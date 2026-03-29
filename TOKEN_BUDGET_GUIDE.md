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
10. [Shorthand Compression (Phase 3 Distillation)](#10-shorthand-compression-phase-3-distillation)
    - [Suitability Analysis by Context](#suitability-analysis-by-context)
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

## 10. Shorthand Compression (Phase 3 Distillation)

After deterministic cleaning (Phase 1) and LLM semantic distillation (Phase 2),
Phase 3 applies **symbolic shorthand compression** — a codebook-driven notation
system that reduces token count while preserving full semantic fidelity.

This phase targets text that is read by LLMs, not humans. Every location where
compressed output is used MUST support a **raw/human toggle** that expands
shorthand back to natural language for debugging and human inspection.

### Design Principles

1. **Token-aware, not just char-aware.** Emojis save characters but often cost
   MORE tokens (✅ = 2-3 tokens, "yes" = 1). Prefer Unicode symbols that
   tokenise efficiently: `→ ≥ ≠ ∧ ∨ ¬ ∈ § ↑ ↓`
2. **Codebook pattern.** Define abbreviations once, use everywhere. The codebook
   lives in the system prompt so the LLM learns it once per session.
3. **Telegraphic grammar.** Drop articles (a/an/the), copulas (is/are/was),
   filler verbs (please/could you), and prepositions where meaning is clear.
4. **No space around symbols.** LLMs parse `cfg→deploy→verify` correctly.
   Symbols act as both semantic markers AND delimiters.
5. **Standard abbreviations only.** Never invent — use what LLMs already know
   from training data (code, docs, papers).
6. **Technical terms stay intact.** Never abbreviate API names, CLI flags,
   file paths, or error messages.

### The Codebook

Injected once in the system prompt. ~300 chars overhead, amortised across
every turn.

```
SHORTHAND: fn=function cfg=config impl=implementation auth=authentication
req=request resp=response dir=directory env=environment dep=dependency
msg=message arg=argument val=value prev=previous cmd=command
→=implies/then ←=from ↔=bidirectional ✓=yes/done ✗=no/failed
⚠=warning/caution §=section_break ∴=therefore ≈=approximately
@=at/located_at #=count/number &=and |=or >=greater <=less
```

### Compression Rules (ordered by reliability)

**TIER 1 — Always apply (HIGH reliability, proven savings):**

| Rule | Before | After | Savings |
|------|--------|-------|---------|
| Drop articles | "the config file" | "config file" | ~15% |
| Drop copulas | "server is running" | "server running" | ~10% |
| Drop filler verbs | "please make sure to" | "" (delete) | ~20% |
| Standard abbrevs | "configuration" | "cfg" | ~60% |
| Arrow notation | "then deploy" | "→deploy" | ~70% |
| Logical symbols | "A and B or C" | "A&B\|C" | ~60% |
| Colon structure | "the output should be JSON" | "output:JSON" | ~50% |
| No-space symbols | "cfg → deploy" | "cfg→deploy" | ~30% |
| Key:value pairs | "the port is 8650" | "port:8650" | ~40% |
| Drop "located at" | "file located at /home/x" | "@/home/x" | ~60% |

**TIER 2 — Apply in structured data (MEDIUM-HIGH reliability):**

| Rule | Before | After | Savings |
|------|--------|-------|---------|
| Bracket grouping | "items: A, B, and C" | "[A,B,C]" | ~40% |
| Range notation | "between 5 and 10" | "5..10" | ~60% |
| Type annotation | "takes a list of strings" | "list[str]" | ~50% |
| Conditional | "if X is true, do Y" | "X→Y" | ~60% |
| Negation | "do not use" | "✗use" | ~50% |

**TIER 3 — Avoid (LOW reliability or negative token savings):**

| Rule | Why avoid |
|------|-----------|
| Vowel dropping | "cmprssd" costs MORE tokens than "compressed" |
| Emoji overload | Most emojis = 2-3 tokens each |
| Made-up acronyms | LLMs guess wrong without codebook |
| Dense logic chains | "A→B∧C∨D" — misinterpreted beyond 3 operators |

### Compression Examples

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

### Where It Applies (Priority Order)

Compression targets ranked by per-turn savings (multiplicative across
all turns in a session):

| Priority | Target | Current Size | Frequency | Est. Savings |
|----------|--------|-------------|-----------|-------------|
| P0 | Tool schema descriptions | 5K-10K chars | Every turn | 30-40% |
| P1 | Memory blocks (MEMORY.md+USER.md) | 2K-3.5K chars | Every turn | 40-50% |
| P2 | System prompt guidance constants | 1.5K chars | Every turn | 40-50% |
| P3 | Context files (AGENTS.md etc) | Up to 20K chars | Every turn | 20-30% |
| P4 | Platform hints | 200-400 chars | Every turn | 30-40% |
| P5 | Context compressor summaries | Variable | Post-compression | 30-40% |
| P6 | Session search results | Variable | Per search | 20-30% |
| P7 | Distilled facts (facts.jsonl) | Variable | At extraction | 40-50% |

**Estimated total savings on a 40-turn session:**

At minimum (P0+P1+P2 only): ~3,000 chars/turn × 40 = **120,000 chars saved**
At full implementation: ~8,000 chars/turn × 40 = **320,000 chars saved**

### The Raw/Human Toggle

Every location that outputs compressed shorthand MUST support expansion
back to natural language. This is non-negotiable for debugging and human
auditing.

**Implementation pattern:**

```python
# tools/shorthand.py

COMPACT_MODE = True  # Global toggle, also per-function override

def compact(text: str, expand: bool = False) -> str:
    """Apply shorthand compression or expand back to natural language."""
    if expand or not COMPACT_MODE:
        return _expand(text)
    return _compress(text)

def _compress(text: str) -> str:
    """Apply codebook + telegraphic rules."""
    # Tier 1 rules applied in order
    ...

def _expand(text: str) -> str:
    """Reverse codebook + restore natural language."""
    # Reverse lookup + grammar restoration
    ...
```

**Toggle locations:**

| Context | Toggle | Default |
|---------|--------|---------|
| Memory injection | `compact_memory` in config.yaml | `true` |
| Tool schemas | `compact_schemas` in config.yaml | `true` |
| System prompt | `compact_system_prompt` in config.yaml | `true` |
| Context files | `compact_context_files` in config.yaml | `false` |
| CLI display | Always expanded for user | `expand` |
| Logging/debug | `HERMES_COMPACT_DEBUG=0` env var | `expand` |
| facts.jsonl | `compact_facts` in config.yaml | `true` |

**CLI command:** `/compact [on|off|status]` — toggle mid-session for debugging.

**Admin UI:** Mission Control → Pipeline page (`#pipeline`). Visual toggle for
every phase, step, and context with live preview. Config stored in
`~/.hermes/pipeline.yaml`.

**Critical rule:** The expansion function must be lossless. If `_expand(_compress(x))`
doesn't preserve meaning, the compression rule is too aggressive and must be
removed.

### Relationship to Existing Distillation

```
Raw Input
    │
    ▼
Phase 1: Deterministic Cleaning (tools/distillation.py)
    │   Whitespace, HTML entities, filler phrases, dedup
    │   Reduction: 20-40%
    ▼
Phase 2: LLM Semantic Distillation (tools/web_tools.py)
    │   Structured summarisation via Gemini Flash
    │   Reduction: 60-80% (on content ≥5K chars)
    ▼
Phase 3: Shorthand Compression (tools/shorthand.py)  ← NEW
    │   Codebook substitution, telegraphic grammar, symbol notation
    │   Reduction: 30-50% additional
    ▼
Compressed Output (with raw/human toggle)
```

Phase 3 is applied AFTER Phases 1-2 on LLM-facing outputs, and independently
on system prompt components, memory entries, and tool schemas where Phases 1-2
don't apply.

### Academic Basis

This approach is validated by:

- **LLMLingua** (Microsoft, 2023): Compressed prompts can outperform verbose
  ones by reducing noise. 3-20× compression maintaining semantics.
- **Rajan Agarwal** (2025): LLMs trained to compress spontaneously invent
  stop-word removal, acronyms, dense punctuation — the same techniques in
  our codebook.
- **MasterPrompting.net**: "Format: Summary → Explanation → Example" (14 tokens)
  replaces "When writing your response, make sure to always start with a brief
  summary, then provide the detailed explanation, and finish with a concrete
  example" (47 tokens). 70% reduction, no quality loss.

### Suitability Analysis by Context

Not every injection point benefits from shorthand compression. This analysis
evaluates each context that enters the LLM conversation, based on content type,
current verbosity, frequency of injection, and risk of semantic degradation.

#### High Value — Recommended

| Context | Size/Turn | Est. Savings | Rationale |
|---------|-----------|-------------|-----------|
| **Tool schema descriptions** | ~9,600 chars (21 tools) | 30–40% (~3–4K chars) | Top 3 tools (execute_code, terminal, delegate_task) = 52% of all description text. Heavy behavioral prose ("Do NOT…", "WHEN TO USE…"). LLM-facing only, never shown to users. Compress once, verify, done. |
| **Tool guidance constants** | ~1,100 chars | 40–50% (~500 chars) | MEMORY_GUIDANCE, SESSION_SEARCH_GUIDANCE, SKILLS_GUIDANCE — pure behavioral prose injected every turn. Already tightly written but still full English grammar. |
| **Platform hints** | 150–350 chars | 30–40% | Short prose instructions per platform. Easy to compress: "TG: ✗markdown, voice→OGG, media→file_id". Per-platform, easy to test individually. |
| **Context compressor summaries** | 2–8K chars | 30–40% | Verbose structured markdown (## Goal, ## Progress, etc). Generated by auxiliary model, consumed by primary. Generation prompt can request shorthand output directly. |
| **Distilled facts** (facts.jsonl) | ~329 bytes avg/fact | 40–50% | "content" field uses full English. Read by LLMs during session_search/query-facts. Compress at extraction time AND injection time. Search/retrieval must work on both forms. |

#### Moderate Value — Lower Priority

| Context | Size/Turn | Notes |
|---------|-----------|-------|
| **Skills index** | ~2,000+ chars | Instruction block (~350 chars) compresses well, but per-skill entries are already near-minimal (30–60 chars each). Moderate ROI. |
| **Agent identity / SOUL.md** | 430 chars (default), up to 20K (custom) | Cached in system prompt — savings only at session start and post-compression rebuilds. Low frequency = low cumulative savings. |

#### Not Suitable — Avoid

| Context | Size/Turn | Why Avoid |
|---------|-----------|-----------|
| **Memory blocks** (MEMORY.md + USER.md) | ~3,575 chars | **Already compressed manually.** Entries like `"CF token@~/.cloudflare_token:list-only(403 on DNS edits)"` and `"Telegram=John. Comms=Alfred↔Penny"` already use §, →, abbreviations. At 91–98% of char budgets. Automated compression gains almost nothing and risks degrading human-curated phrasing. |
| **Context files** (AGENTS.md, .hermes.md) | Up to 20K/file | ~80% technical content: code examples, tables, file paths, exact command syntax. "Technical terms stay intact" rule means most content is untouchable. Multiple projects/users contribute these files. |
| **User/gateway system messages** | Variable | Externally provided — we don't control this content. May contain exact quotes, code, or formatting that must be preserved. |
| **Timestamp + metadata** | 80–150 chars | Already minimal (`"Model: anthropic/claude-opus-4.6"`). Contains exact values that must not be altered. Negligible savings. |

#### Summary

The biggest win is **tool schemas** — 9,600 chars every turn, mostly behavioral
prose, entirely LLM-facing. Combined with tool guidance and compressor summaries,
the low-risk targets yield ~4,500–7,500 chars/turn savings. Memory is already
doing shorthand manually and should not be automated. Context files are too
technical to compress safely.

---

## 12. Known Debt

**Scattered constants.** The magic numbers in [§1](#1-the-magic-numbers) are
hardcoded locally in each tool file rather than imported from a centralised
constants module. A future improvement would consolidate them into a single
`token_constants.py` that all tools import from.

**Skipped test suite.** `test_413_compression.py` (handling context overflow
errors) is currently skipped in the CI/CD environment.
