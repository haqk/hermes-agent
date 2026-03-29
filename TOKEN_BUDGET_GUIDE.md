# Token Budget Guide

> **Single Source of Truth** for token usage optimisation in Hermes Agent.
> Companion to [AGENTS.md](./AGENTS.md) — read that first for project structure.

Every byte the LLM sees costs money and context window. This guide documents
how Hermes minimises both through hard caps, pre-processing pipelines,
caching, and intelligent routing.

---

## Table of Contents

1. [The Magic Numbers](#1-the-magic-numbers)
2. [Prompt Caching](#2-prompt-caching)
3. [Pre-Processing Pipelines](#3-pre-processing-pipelines)
4. [Context Compression](#4-context-compression)
5. [Distillation Pipeline](#5-distillation-pipeline)
6. [Auxiliary Model Routing](#6-auxiliary-model-routing)
7. [Token OS — Multi-Provider Orchestration](#7-token-os--multi-provider-orchestration)
8. [Cross-Session Awareness](#8-cross-session-awareness)
9. [Cost Reduction — Lessons Learned](#9-cost-reduction--lessons-learned)
10. [Shorthand Compression](#10-shorthand-compression)
11. [Known Debt](#12-known-debt)

---

## 1. The Magic Numbers

All hard caps in one place. These are currently scattered across individual
files (see [Known Debt](#10-known-debt)) but should be treated as canonical.

| Limit | What | Where | Truncation Strategy |
|------:|------|-------|---------------------|
| 50,000 chars | Terminal/stdout max output | `tools/terminal_tool.py` | 40% head / 60% tail |
| 20,000 chars | Context file injection max | `agent/prompt_builder.py` | 70% head / 20% tail / 10% gap |
| 20,000 tokens | Tail protection budget | `agent/context_compressor.py` | Newest messages preserved |
| 8,000 chars | Browser snapshot threshold | `tools/browser_tool.py` | LLM-summarised or truncated |
| 5,000 chars | Web extraction final cap | `tools/web_tools.py` | LLM-summarised above threshold |
| 3,000 chars | Message truncation during compression | `agent/context_compressor.py` | Hard truncation |
| 2,000 lines | File read max | `tools/file_tools.py` | Paginated |
| 200 KB | Process output rolling buffer | `tools/process_registry.py` | Old output evicted |
| 80 chars | Cheap pruning threshold | `agent/context_compressor.py` | Replaced with stub |

---

## 2. Prompt Caching

**File:** `agent/prompt_caching.py`

Strategy: **"system_and_3"** — places 4 cache breakpoints at the system prompt
plus the last 3 non-system messages. Anthropic caches these server-side with a
5-minute TTL.

**The critical rule:** the system prompt and early conversation prefix MUST
remain static throughout a session. Breaking cache forces dramatically higher
costs.

**What must never happen mid-conversation:**
- Altering past context
- Changing toolsets
- Reloading memories or rebuilding the system prompt

The only exception is context compression, which is handled carefully to
preserve cache validity.

---

## 3. Pre-Processing Pipelines

Every tool output is compressed before it enters the conversation. No raw
scrape, terminal dump, or snapshot reaches the LLM unprocessed.

### 3a. Web Content (`tools/web_tools.py`)

| Stage | What | Details |
|-------|------|---------|
| 1 | Backend normalisation | Markdown conversion from raw HTML |
| 2 | Base64 image stripping | Regex: `data:image/[^;]+;base64,...` → `[BASE64_IMAGE_REMOVED]` |
| 3 | Pre-distillation cleaning | 9-step deterministic pipeline (see [§5](#5-distillation-pipeline)) |
| 4 | LLM summarisation | Triggered at ≥ 5,000 chars; uses auxiliary model |
| 5 | Chunking for massive pages | 500K–2M → 100K chunks → parallel summarise → synthesise; >2M refused |
| 6 | Hard cap | 5,000 chars per page in final output |

### 3b. Terminal Output (`tools/terminal_tool.py`)

Hard cap at 50,000 characters. Truncation uses a 40/60 head/tail split.
Rationale: errors usually appear at the top, recent output at the bottom.
Content under the cap passes through untouched.

### 3c. Browser Snapshots (`tools/browser_tool.py`)

Accessibility tree snapshots exceeding 8,000 chars are LLM-summarised or
truncated.

### 3d. Process Registry (`tools/process_registry.py`)

Rolling output buffer capped at 200KB. Old output is evicted as new output
arrives.

### 3e. Context File Injection (`agent/prompt_builder.py`)

Files like `AGENTS.md`, `.hermes.md`, and project context files are injected at
session start:

- **Max size:** `CONTEXT_FILE_MAX_CHARS` = 20,000 chars
- **If exceeded:** head/tail truncation (70% head, 20% tail, 10% gap)
- **Security:** `_CONTEXT_THREAT_PATTERNS` blocks prompt injection strings

---

## 4. Context Compression

**File:** `agent/context_compressor.py`

Triggers when context usage hits **50% of the model's context window**.
Uses an auxiliary model (Gemini Flash) to iteratively summarise conversation
history without losing critical information.

### 5-Step Pipeline

1. **Cheap Pruning** — Tool results >80 chars outside the protected zone are
   replaced with `[Old tool output cleared to save context space]`.

2. **Head Protection** — First N messages preserved verbatim (default: 3).

3. **Tail Protection** — Most recent messages preserved.
   Budget: `_DEFAULT_TAIL_TOKEN_BUDGET` = 20,000 tokens.
   Minimum: `protect_last_n` = 4 messages.

4. **Summarisation** — Middle turns are serialised (each truncated to 3,000
   chars) and sent to the auxiliary model with a structured template:
   Goal → Progress → Decisions → Files → Next Steps.

5. **Iterative Update** — If a previous summary exists, new turns are folded
   into it (not re-summarised from scratch).

**Target summary size:** `_SUMMARY_RATIO` = 0.20 (20% of compressed content).

**Critical fix-up:** `_sanitize_tool_pairs` inserts stub results for orphaned
tool calls after middle-turn removal, preventing API errors from malformed
message sequences.

---

## 5. Distillation Pipeline

There are two distinct contexts where distillation happens:

- **Web content** — clean scraped pages before the agent sees them
- **Session transcripts** — extract durable facts from conversations

Both share the same primitives from **`tools/distillation.py`** — the DRY
module consumed by five systems: `web_tools`, `browser_tool`,
`session_search`, `context_compressor`, and the autosave pipeline
(`process_session.py`).

### Phase 1: Deterministic Cleaning (No LLM)

**File:** `tools/web_tools.py` → `clean_content_pre_distillation()`
**Primitives:** `tools/distillation.py`

A 9-step regex/heuristic pipeline that strips noise before any LLM involvement.
Every transformation is provably lossless — only content that cannot affect
meaning is removed. Typical reduction: **20–40%** on scraped web content.

| Step | What | Details |
|------|------|---------|
| 1 | Whitespace normalisation | Collapse 3+ blank lines → 2; inline space runs → single space |
| 2 | HTML entity cleanup | `&nbsp;` `&amp;` `&lt;` `&gt;` `&quot;` `&#39;`; zero-width spaces; BOM chars |
| 3 | Empty markdown removal | Empty headings, links `[]()`, bold/italic markers |
| 4 | URL tracking params | `utm_*`, `fbclid`, `gclid`, `msclkid`, `mc_*`, `yclid`, `_ga`, `_gl`, etc. |
| 5 | Decorative image refs | Pattern-matched: `![image]()`, `![banner]()`, `![logo]()`, `![icon]()` |
| 6 | Filler phrases | 14 patterns: cookie banners, social share buttons, nav boilerplate |
| 7 | Line deduplication | Exact duplicate lines removed (10–200 char window; short lines preserved) |
| 8 | Reference compression | URLs appearing 3+ times → `[ref1]`, `[ref2]`, etc. with reference table |
| 9 | Final whitespace cleanup | Collapse any multi-blank-lines created by prior steps |

### Phase 2: LLM-Powered Distillation (Semantic)

**File:** `tools/web_tools.py` → `_call_summarizer_llm()`
**Fallback:** `tools/distillation.py` → `call_with_haiku_fallback()`

Only runs after Phase 1, and only if cleaned content is still ≥ 5,000 chars.

- **Primary model:** Gemini Flash (via auxiliary client)
- **Retry:** 3 attempts with exponential backoff (2s, 4s, 8s cap)
- **Fallback:** Claude 3.5 Haiku
- **If both fail:** raw (cleaned) content passed through
- **Large docs:** 100K chunks → parallel distil → synthesis
- **Truncated JSON recovery:** partial responses salvaged by closing the array

### Session Transcript Distillation (Autosave)

**File:** `autosave/process_session.py`

The key insight (from Sanity/Miriad's CTO, documented in vault):

> *"Summarization preserves narrative. Agents don't need narrative —
> they need operational intelligence."*

We don't summarise. We **distil into structured semantic atoms**.

#### 4-Phase Pipeline

1. **Message Extraction** — Pull user/assistant messages from session JSON.
   Skip tool messages. Skip messages <10 chars. Truncate messages >2,000 chars.

2. **Transcript Cleaning** (deterministic) — `collapse_whitespace()`,
   `collapse_json_blocks()`, `strip_filler_lines(TRANSCRIPT_FILLER_PATTERNS)`,
   `dedup_lines()`. Strips assistant narration like "Let me check that for you."
   Typical reduction: 20–40%.

3. **Chunk-and-Merge Distillation** — Transcripts >10K chars split at message
   boundaries. Each chunk distilled independently with `[Chunk N/M]` markers.
   Facts merged then deduplicated by first 100 chars.

4. **Fact Persistence** — Atomic facts written to `facts.jsonl`. Sessions
   logged in `processed.jsonl` (idempotency ledger). Cron sessions skipped.
   Minimum 4 messages required.

#### Fact Schema

The LLM extracts **typed atomic facts** with durability classifications:

**7 fact types** (priority ordered):
1. `RULES` — standing instructions ("always do X", "never do Y")
2. `IDENTITY` — who the user is, their role, location
3. `LESSONS` — things learned from errors or surprises
4. `PREFERENCES` — user corrections, style, workflow choices
5. `DECISIONS` — choices made WITH reasoning (the why, not the what)
6. `FACTS` — discoveries about our systems/infrastructure
7. `ACTIONS` — significant milestones (not mechanical build steps)

**4 durability classes:**
- `permanent` — standing rules, identity, critical config (never expires)
- `durable` — lessons, preferences, architecture decisions (slow decay)
- `temporal` — current state/counts that will change (fast decay)
- `ephemeral` — build logs, transient actions (fastest decay)

Each fact includes a `reasoning` field — we capture **why** something matters,
not just what happened.

**Hard negative examples** (explicitly skipped):
- "Ran npm install" (mechanical)
- "Searched for X on web" (tool noise)
- "Built Tool #42: Compress Image" (ephemeral build log)

### Shared Primitives (`tools/distillation.py`)

| Function | What |
|----------|------|
| `collapse_whitespace()` | 3+ blank lines → 2 |
| `dedup_lines()` | Exact match dedup (10–200 char window) |
| `collapse_json_blocks()` | Large JSON → first 3 + last 3 lines |
| `strip_filler_lines()` | Regex pattern removal |
| `WEB_FILLER_PATTERNS` | 14 web boilerplate patterns |
| `TRANSCRIPT_FILLER_PATTERNS` | Assistant narration noise patterns |
| `call_with_haiku_fallback()` | Retry + Haiku fallback pattern |
| `async_call_with_haiku_fallback()` | Async variant |

Fallback model: `anthropic/claude-3.5-haiku` (hardcoded constant).

### Distillation Metrics

The CLI displays stats at session end (via `_DistillationMetrics` in `web_tools.py`):

```
Distiller:      12 pages, avg 3200 chars output
⚠ Over 10K:    1 pages (large output)
⚠ Failures:    0 (raw content passed through)
Skipped:        5 (content too short)
```

### Why This Matters

This pipeline exists so the agent doesn't re-read raw transcripts. Instead of
injecting 30K chars of conversation history, the system produces ~10–15 atomic
facts totalling ~2K chars — a **15× compression ratio**. The compressed form is
better for retrieval because it uses high-value vocabulary rather than
conversational filler.

---

## 6. Auxiliary Model Routing

**File:** `agent/auxiliary_client.py`

Low-priority tasks are routed to cheaper models instead of the primary (Opus):

| Task | Model |
|------|-------|
| Vision analysis | Gemini Flash |
| Web summarisation | Gemini Flash |
| Context compression | Gemini Flash |
| Cron/background jobs | Cerebras (free) or Gemini Flash |
| Memory compaction (6h cycle) | Gemini Flash |
| Fact extraction | Gemini Flash |

**Fallback chain:**
1. Default auxiliary: `gemini-3-flash-preview`
2. Anthropic fallback: `claude-haiku-4-5-20251001`
3. Codex fallback: `gpt-5.2-codex`
4. If all fail: hard truncation or drop middle turns (lossy but functional)

---

## 7. Token OS — Multi-Provider Orchestration

A dedicated service (port 8650) manages LLM quota across all providers.

### Priority Tiers

| Tier | Purpose | Routing |
|------|---------|---------|
| P0 | Interactive (user-facing) | 30% capacity reserved for Anthropic Opus/Sonnet |
| P1–P3 | Standard tasks | Normal routing |
| P4 | Background tasks | Hardcoded to Cerebras (free) or Gemini Flash |

### Throttle Zones

```
Green → Yellow → Red → Emergency
```

Burn rate is monitored continuously. Requests are throttled as quota depletes
through each zone.

---

## 8. Cross-Session Awareness

Instead of re-reading full transcripts (expensive), the autosave pipeline
extracts pre-processed artefacts:

| Artefact | What | Injection |
|----------|------|-----------|
| `recent_context.md` | What happened in other sessions | Read once at session start |
| `facts.jsonl` | Atomic facts (cold storage) | Searchable via `query-facts` |
| Memory hot cache | Compact durable facts | Auto-injected every turn |

Agents start sessions with cross-session awareness without burning tokens on
raw history reprocessing.

---

## 9. Cost Reduction — Lessons Learned

### Peer-Learning System → Agent Diary
Was costing 240+ Opus calls/day. Replaced with a daily "Agent Diary" (1 Opus
call) for similar reflective value at ~1/100th the cost.

### Cron Job Model Routing
Agent cron jobs were defaulting to Opus. Updated to use Haiku/Cerebras for
non-strategic tasks.

### Claude Code OAuth
Both agents use the subscription-included OAuth token instead of per-token
Anthropic API key where possible. Token refresh automated via cron +
`refresh_claude_token.py`.

### Free Models on OpenRouter
Identified 27 free models with up to 1M context. Best picks: Qwen3 Coder,
Nemotron 3 Super, GPT-OSS-120B. Rate-limited (20 req/min, 200/day) so not
suitable for production.

### Failover Bug Fix (March 25)
Anthropic's "too many tokens" rate-limit message was being caught by the
context-length detection logic — causing the system to shrink context windows
instead of failing over to another provider. Fix: prioritise `is_rate_limited`
flag over keyword matching. Commit: `64fc2537`.

---

## 10. Shorthand Compression

Shorthand compression instructs the Phase 2 auxiliary LLM to produce dense,
token-efficient output using compression techniques that LLMs natively
understand from training data. This is **not a separate pipeline phase** — it
is a set of instructions appended to existing Phase 2 prompts.

1. **Dynamic content** (web pages, compressor summaries, distilled facts) —
   shorthand instructions appended to the Phase 2 LLM prompt. The auxiliary
   model applies enabled compression techniques during distillation. One LLM
   call, not two.

2. **Static content** (tool schemas, guidance constants, platform hints) —
   hand-compressed once, committed to source. No runtime cost.

### Compression Components

Nine individually-toggleable techniques, applied in layer order. Each is a
one-line instruction to the auxiliary LLM — no libraries, no codebooks, no
regex. LLMs know all of these from training data.

**File:** `tools/distillation.py` → `SHORTHAND_COMPONENTS`
**Config:** `compression.shorthand.<component>` in config.yaml
**UI:** Mission Control → Pipeline → Phase 2 card → Shorthand section

**Layer 1 — Structure** (reshape output format):

| Component | Instruction | Example |
|-----------|-------------|---------|
| `key_value` | Key:value & structured notation over prose | "port is 8650" → "port:8650" |
| `hierarchical` | Hierarchical indentation over connective prose | Nested structure instead of "A contains B which has C" |

**Layer 2 — Token Reduction** (remove/shorten words within structure):

| Component | Instruction | Example |
|-----------|-------------|---------|
| `telegraphic` | Drop articles, copulas, filler verbs | "the config file is ready" → "config file ready" |
| `abbreviations` | Standard abbreviations & symbols (→ & \| @ ✓ ✗) | "and then" → "&→" |
| `brace` | Brace expansion for repeated patterns | "config.yaml, config.json" → "config.{yaml,json}" |
| `reference` | First mention full, subsequent abbreviated | "Mission Control (MC)... MC restart" |
| `ellipsis` | Omit predictable parts of repeated patterns | "read_file→content, write_file→status" |
| `ternary` | Ternary notation for conditionals | "if X then Y else Z" → "X ? Y : Z" |

**Layer 3 — Density** (final squeeze):

| Component | Instruction | Example |
|-----------|-------------|---------|
| `llmlingua` | Keep only high-perplexity tokens, omit predictable | LLMLingua-style: tokens the reader would predict are dropped |

**Safety Guards** (always on when any component is active):
- Preserve all specific values, numbers, and proper nouns
- Never abbreviate file paths, CLI flags, or error messages
- Never reuse abbreviations ambiguously

### How It Works

The function `build_shorthand_suffix()` in `tools/distillation.py` reads which
components are enabled from config, assembles them in layer order, appends
safety guards, and returns a prompt suffix. Each Phase 2 consumer appends this
suffix to its LLM prompt via `shorthand_suffix_if_enabled(context_key)`.

The primary model receives a one-line hint in its system prompt —
`"Decompress & interpret: shorthand used in some context below."` — so it
knows to interpret compressed content naturally. No codebook needed; LLMs
read shorthand natively from training data.

### Compression Examples

These show what the auxiliary LLM produces when given shorthand instructions.

**Memory entry (before — 187 chars):**
```
The user prefers that we always commit and push after making code changes.
The user is located in Australia and uses Telegram as the primary
communication channel.
```

**Memory entry (after — 89 chars, 52% reduction):**
```
✓always commit&push after code changes
user:AU comms:TG(primary)
```

**System prompt guidance (before — 280 chars):**
```
When you discover something new about the environment, such as the operating
system, installed tools, or project structure, save it to memory. When the
user corrects you, save that correction immediately so you don't repeat the
mistake.
```

**System prompt guidance (after — 118 chars, 58% reduction):**
```
env discovery(OS,tools,project structure)→save to memory
user correction→save immediately(✗repeat mistakes)
```

**Fact entry (before — 120 chars):**
```
Cloudflare token at ~/.cloudflare_token has list-only permissions and
returns 403 on DNS edit operations.
```

**Fact entry (after — 62 chars, 48% reduction):**
```
CF token@~/.cloudflare_token:list-only(403 on DNS edits)
```

### Where It Applies

| Target | Method | Frequency |
|--------|--------|-----------|
| **Tool schema descriptions** (~9.6K chars) | Hand-compressed once, cached | At release / when schemas change |
| **Guidance constants** (~1.1K chars) | Hand-compressed once, cached | At release / when guidance changes |
| **Platform hints** (~250 chars each) | Hand-compressed once, cached | At release / when hints change |
| **Web extraction** (variable) | Codebook added to Phase 2 prompt | Per extraction (≥5K chars) |
| **Context compressor summaries** (2–8K) | Codebook added to compressor prompt | On context compression trigger |
| **Distilled facts** (variable) | Codebook added to fact extraction prompt | Per session distillation |

**Not suitable:** Memory blocks (already hand-compressed), context files (too
technical), user/gateway messages (not ours), timestamps (too small).

### Integration Architecture

```
Static content (tool schemas, guidance, hints)
    │
    └─ Compressed once (hand edit or one-off LLM pass)
       └─ Cached in source code / config
          └─ Served every turn with no runtime cost

Dynamic content
    │
    ▼
Phase 1: Deterministic Cleaning (tools/distillation.py)
    │   Whitespace, HTML entities, filler phrases, dedup
    │   Reduction: 20-40%
    ▼
Phase 2: LLM Distillation + Shorthand (auxiliary model)
    │   Structured summarisation + enabled shorthand components
    │   Prompt suffix assembled from toggled components in layer order
    │   Reduction: 60-80% (includes shorthand gains)
    ▼
Compressed Output
```

There is no separate Phase 3 runtime step. Shorthand is a convention applied
BY Phase 2, not AFTER Phase 2.

### Admin UI

Mission Control → Pipeline page (`#pipeline`) → Phase 2 card:
- Per-context master toggles: web extraction, compressor, facts
- Expandable component section: 9 techniques grouped by layer
- Components are shared across all contexts (toggle once, applies everywhere)
- Config stored in `~/.hermes/pipeline.yaml`, synced to `~/.hermes/config.yaml`
  (`compression.shorthand.*`)

### Academic Basis

- **LLMLingua** (Microsoft, 2023): Compressed prompts can outperform verbose
  ones by reducing noise. 3-20× compression maintaining semantics.
- **Rajan Agarwal** (2025): LLMs trained to compress spontaneously invent
  stop-word removal, acronyms, dense punctuation — the same techniques in
  our codebook.
- **MasterPrompting.net**: "Format: Summary → Explanation → Example" (14 tokens)
  replaces "When writing your response, make sure to always start with a brief
  summary, then provide the detailed explanation, and finish with a concrete
  example" (47 tokens). 70% reduction, no quality loss.

---

## 12. Known Debt

**Scattered constants.** The magic numbers in [§1](#1-the-magic-numbers) are
hardcoded locally in each tool file rather than imported from a centralised
constants module. A future improvement would consolidate them into a single
`token_constants.py` that all tools import from.

**Skipped test suite.** `test_413_compression.py` (handling context overflow
errors) is currently skipped in the CI/CD environment.
