# Shorthand Compression — Implementation Plan

> Companion to [TOKEN_BUDGET_GUIDE.md §10](./TOKEN_BUDGET_GUIDE.md#10-shorthand-compression).
> This document defines the current state, the target state, and every change
> required to get there. Enterprise-grade: no gaps, no stubs, no disconnects.

---

## Table of Contents

1. [Current State (What Exists)](#1-current-state)
2. [Target State (What We Want)](#2-target-state)
3. [Gap Analysis](#3-gap-analysis)
4. [Changes Required](#4-changes-required)
   - [Layer 1: Agent — Remove tools/shorthand.py](#41-agent--remove-toolsshorthandpy)
   - [Layer 2: Agent — Enhance Phase 2 Prompts](#42-agent--enhance-phase-2-prompts)
   - [Layer 3: Agent — Hand-Compress Static Content](#43-agent--hand-compress-static-content)
   - [Layer 4: Agent — Codebook Injection](#44-agent--codebook-injection)
   - [Layer 5: Agent — Config Cleanup](#45-agent--config-cleanup)
   - [Layer 6: Mission Control — Pipeline Page](#46-mission-control--pipeline-page)
   - [Layer 7: Mission Control — Preview](#47-mission-control--preview)
   - [Layer 8: Tests](#48-tests)
   - [Layer 9: Documentation](#49-documentation)
5. [Verification Criteria](#5-verification-criteria)
6. [Rollout](#6-rollout)

---

## 1. Current State

### What exists today (broken)

| Component | Location | Status |
|-----------|----------|--------|
| `tools/shorthand.py` | hermes-agent | Regex-based compression engine. Achieves 3-20% reduction. Wrong approach — §10 requires LLM-powered compression. |
| `tests/test_shorthand.py` | hermes-agent | 44 tests for the regex engine. All pass, but test the wrong thing. |
| Config: `shorthand.*` | hermes-agent `config.py` | 5 per-context toggles (tool_schemas, tool_guidance, platform_hints, compressor, facts). All default False. |
| Config version | hermes-agent `config.py` | Bumped to 11 for shorthand section. |
| Hook: model_tools.py | hermes-agent | Calls `compact_with_metrics()` on tool schema descriptions. Does almost nothing (~5% reduction). |
| Hook: run_agent.py | hermes-agent | Calls `compact_with_metrics()` on guidance + platform hints. Injects codebook into system prompt. Loads config. |
| Hook: context_compressor.py | hermes-agent | Calls `compact_with_metrics()` on compressor summaries. |
| Hook: process_session.py | autosave | Calls `compact_with_metrics()` on distilled facts. |
| `/compact` command | hermes-agent CLI | Registered in commands.py, handler in cli.py. Toggles shorthand on/off/status. |
| Pipeline page: P3 card | mission-control | Shows Phase 3 with toggles for codebook_injection, tier1_telegraphic, tier2_structured. |
| Pipeline page: P3 preview | mission-control | Calls `tools.shorthand._compress()` for preview. Shows real (bad) reduction %. |
| pipeline.yaml: phase3 | ~/.hermes | Keys: enabled, codebook_injection, tier1_telegraphic, tier2_structured. |

### What's wrong

1. **Wrong abstraction.** `tools/shorthand.py` is a regex engine where the
   design requires LLM-powered compression. The codebook and tier rules are
   instructions for an auxiliary LLM, not regex patterns.

2. **Disconnected systems.** Pipeline page writes to `pipeline.yaml` with
   tier-based keys. Agent reads from `config.yaml` with context-based keys.
   Different files, different key names, different conceptual axis. Toggling
   switches on the pipeline page does nothing to the agent.

3. **Wrong architecture.** Runtime per-turn regex compression on static text
   that we authored ourselves. Should be: hand-compress once, cache, serve.

4. **Misleading metrics.** 5% actual reduction vs 30-50% claimed in the
   original §10. The examples in §10 require semantic restructuring that
   only an LLM can do.

---

## 2. Target State

### Static content (tool schemas, guidance, platform hints)

Hand-compressed versions of these strings exist in source code alongside the
originals. The compressed versions are served to the LLM every turn. No runtime
compression engine, no per-turn LLM calls. Compression done once at development
time, verified, committed.

### Dynamic content (web extraction, compressor summaries, distilled facts)

The existing Phase 2 LLM prompts include the codebook and tier rules as
additional instructions. The auxiliary model (Gemini Flash) applies shorthand
conventions during distillation. No separate compression step. One LLM call
per content chunk, same as today — just with better instructions.

### Codebook in system prompt

When shorthand is active, the ~300 char codebook is appended to the system
prompt so the primary model can read compressed content in its context.

### Mission Control pipeline page

The Phase 3 card is replaced with shorthand toggles integrated into each
applicable context. The "phase3" concept is removed from the UI — shorthand
is a property of Phase 2, not a separate phase. pipeline.yaml and config.yaml
agree on key names and values via a single source of truth.

### No runtime compression engine

`tools/shorthand.py` is deleted. No regex engine, no per-turn compression
hooks, no `compact_with_metrics()` calls scattered across the codebase.

---

## 3. Gap Analysis

| # | Gap | Severity | Current | Target |
|---|-----|----------|---------|--------|
| 1 | `tools/shorthand.py` exists as a regex engine | CRITICAL | 418 lines of wrong abstraction | Deleted |
| 2 | 5 hooks in agent code call regex engine per-turn | CRITICAL | model_tools, run_agent (×2), context_compressor, process_session | All removed |
| 3 | Phase 2 prompts lack codebook/tier instructions | CRITICAL | Plain summarisation prompt | Enhanced with codebook + tier rules |
| 4 | Static content not hand-compressed | HIGH | Full verbose English | Compressed versions alongside originals |
| 5 | Pipeline page disconnected from agent | HIGH | Different files + keys + axis | Single source of truth |
| 6 | Pipeline page shows "Phase 3" as separate phase | MEDIUM | Separate card with tier toggles | Shorthand toggle per context in Phase 2 |
| 7 | `shorthand.*` config section | MEDIUM | 5 toggles, all False | Removed — replaced with per-context phase2 shorthand flag |
| 8 | `/compact` CLI command | LOW | Exists, toggles regex engine | Removed — no runtime engine to toggle |
| 9 | `tests/test_shorthand.py` | LOW | 44 tests for regex engine | Deleted — replaced with Phase 2 integration tests |
| 10 | Config version bumped to 11 | LOW | For shorthand section | Migration removes shorthand section |

---

## 4. Changes Required

### 4.1 Agent — Remove tools/shorthand.py

**Delete:**
- `tools/shorthand.py`
- `tests/test_shorthand.py`

**Remove hooks (revert to original code):**

| File | What to revert |
|------|----------------|
| `model_tools.py` | Remove Phase 3 compression block after line 347 |
| `run_agent.py` | Remove shorthand import + `configure_from_dict()` call (~L904-910) |
| `run_agent.py` | Revert guidance text compression block (~L2281-2291) to plain `prompt_parts.append(" ".join(tool_guidance))` |
| `run_agent.py` | Revert platform hints block (~L2402-2425) to plain `prompt_parts.append(PLATFORM_HINTS[platform_key])` + remove codebook injection |
| `agent/context_compressor.py` | Remove shorthand compression block after summary generation (~L346-352) |
| `~/.hermes/autosave/process_session.py` | Remove shorthand compression block before facts writing (~L530-544) |

**Remove CLI command:**

| File | What to remove |
|------|----------------|
| `hermes_cli/commands.py` | Delete the `CommandDef("compact", ...)` entry |
| `cli.py` | Delete the `elif canonical == "compact"` dispatch line |
| `cli.py` | Delete the `_handle_compact_command()` method |

**Remove config:**

| File | What to change |
|------|----------------|
| `hermes_cli/config.py` | Delete the `"shorthand": {...}` section from `DEFAULT_CONFIG` |
| `hermes_cli/config.py` | Keep `_config_version` at 11 — add migration to remove `shorthand` from existing configs |

### 4.2 Agent — Enhance Phase 2 Prompts

Add the codebook and tier rules to the LLM prompts used by each distillation
consumer. The codebook is defined once in `tools/distillation.py` as a constant
and imported where needed.

**New constant in `tools/distillation.py`:**

```python
SHORTHAND_CODEBOOK = """
SHORTHAND: fn=function cfg=config impl=implementation auth=authentication
req=request resp=response dir=directory env=environment dep=dependency
msg=message arg=argument val=value prev=previous cmd=command
→=implies/then ←=from ↔=bidirectional ✓=yes/done ✗=no/failed
⚠=warning/caution §=section_break ∴=therefore ≈=approximately
@=at/located_at #=count/number &=and |=or >=greater <=less
"""

SHORTHAND_INSTRUCTIONS = """
Apply shorthand conventions to your output:
- Drop articles (the/a/an), copulas (is/are/was), filler verbs (please/ensure)
- Use codebook abbreviations: fn, cfg, impl, auth, req, resp, dir, env, dep, msg, arg, val, cmd
- Use domain abbreviations the LLM already knows (CF=Cloudflare, TG=Telegram, AU=Australia, etc.)
- Arrow notation: then→, from←, bidirectional↔
- Logical: and→&, or→|, not→✗, done→✓
- Key:value pairs: "the port is 8650" → "port:8650"
- @ for location: "at ~/.config" → "@~/.config"
- Brackets for lists: "A, B, and C" → "[A,B,C]"
- Parenthetical qualifiers: "uses X as the primary Y" → "X(primary)"
- NEVER abbreviate file paths, CLI flags, API names, or error messages
- NEVER use vowel dropping or made-up acronyms
"""
```

**Files to modify:**

| File | Function | Change |
|------|----------|--------|
| `tools/web_tools.py` | `_call_summarizer_llm()` | Append codebook + instructions to summarisation prompt when shorthand is enabled for web context |
| `agent/context_compressor.py` | `_generate_summary()` | Append codebook + instructions to compaction prompt when shorthand is enabled for compressor context |
| `autosave/process_session.py` | `distil_via_llm()` | Append codebook + instructions to fact extraction prompt when shorthand is enabled for facts context |

Each integration is gated by a config toggle (see §4.5) so shorthand can be
enabled per-context independently.

### 4.3 Agent — Codebook Injection

When any shorthand-compressed content is present in the context (static or
dynamic), the codebook must be in the system prompt so the primary model can
read it.

**File:** `run_agent.py` → `_build_system_prompt()`

**Logic:**
```python
# At end of _build_system_prompt(), before return:
if self._shorthand_active:
    from tools.distillation import SHORTHAND_CODEBOOK
    prompt_parts.append(SHORTHAND_CODEBOOK)
```

`_shorthand_active` is set during `__init__` based on config. If ANY shorthand
context is enabled, the codebook is injected.

### 4.4 Agent — Config Cleanup

**Remove** the `shorthand.*` section from `DEFAULT_CONFIG`.

**Add** per-context shorthand toggles to the `compression` section (which
already exists and is the natural home):

```python
"compression": {
    "enabled": True,
    "threshold": 0.50,
    ...
    "shorthand": {
        "web_extract": False,      # Add codebook to web summarisation prompt
        "compressor": False,       # Add codebook to compaction prompt
        "facts": False,            # Add codebook to fact extraction prompt
},
},
```

All default False. Activated via Mission Control pipeline page.

**Config version** stays at 11. Add migration logic: if `shorthand` key exists
at top level, delete it and merge into `compression.shorthand`.

### 4.5 Mission Control — Pipeline Page

**Remove** the Phase 3 card entirely. Shorthand is not a phase.

**Add** a shorthand toggle to each applicable context's detail view:

| Context | Toggle | Maps to config |
|---------|--------|----------------|
| Web Extraction | "Apply shorthand conventions" | `compression.shorthand.web_extract` |
| Context Compression | "Apply shorthand conventions" | `compression.shorthand.compressor` |
| Fact Distillation | "Apply shorthand conventions" | `compression.shorthand.facts` |

**pipeline.yaml changes:**
- Remove `phase3` section entirely
- Add `shorthand` flags under each context's config:
  ```yaml
  contexts:
    web_extraction:
      shorthand: false
    context_compression:
      shorthand: false
    fact_distillation:
      shorthand: false
  ```

**Sync:** The PATCH endpoint writes to both pipeline.yaml AND config.yaml
when shorthand keys change, so the agent picks up changes immediately.

### 4.6 Mission Control — Preview

**Remove** the Phase 3 preview that calls `_compress()`.

**Replace** with a note on Phase 2: "With shorthand: codebook conventions will
be applied during LLM distillation. Preview shows Phase 1 cleaning only —
Phase 2 requires live LLM call."

The preview cannot demonstrate shorthand compression without making an actual
LLM call, which would cost money on every preview. This is an acceptable
limitation. The examples in TOKEN_BUDGET_GUIDE.md §10 serve as reference.

### 4.7 Tests

**Delete:** `tests/test_shorthand.py` (44 tests for deleted regex engine)

**Add:** Integration tests for the enhanced Phase 2 prompts:

| Test | What it verifies |
|------|-----------------|
| `test_web_summariser_includes_codebook` | When `compression.shorthand.web_extract` is True, the summarisation prompt includes SHORTHAND_CODEBOOK |
| `test_compressor_includes_codebook` | When `compression.shorthand.compressor` is True, the compaction prompt includes SHORTHAND_CODEBOOK |
| `test_fact_extractor_includes_codebook` | When `compression.shorthand.facts` is True, the extraction prompt includes SHORTHAND_CODEBOOK |
| `test_codebook_not_injected_when_disabled` | When all shorthand toggles are False, no codebook in any prompt |
| `test_system_prompt_codebook_injection` | When `_shorthand_active` is True, SHORTHAND_CODEBOOK appears in system prompt |
| `test_config_migration_removes_shorthand` | Configs with top-level `shorthand` key get migrated to `compression.shorthand` |

### 4.8 Documentation

**Already done:** TOKEN_BUDGET_GUIDE.md §10 rewritten (this session).

**Still needed:**
- Update AGENTS.md if it references tools/shorthand.py or /compact command
- Update pipeline.yaml schema docs if they exist
- Session summary in vault

---

## 5. Verification Criteria

The implementation is complete when ALL of the following are true:

- [ ] `tools/shorthand.py` does not exist
- [ ] `tests/test_shorthand.py` does not exist
- [ ] No file in hermes-agent imports from `tools.shorthand`
- [ ] No `shorthand` key at top level of DEFAULT_CONFIG
- [ ] `compression.shorthand.*` toggles exist, all default False
- [ ] `/compact` command does not exist
- [ ] `SHORTHAND_CODEBOOK` and `SHORTHAND_INSTRUCTIONS` constants exist in `tools/distillation.py`
- [ ] Web summariser prompt includes codebook when enabled
- [ ] Compressor prompt includes codebook when enabled
- [ ] Fact extraction prompt includes codebook when enabled
- [ ] System prompt includes codebook when any shorthand is active
- [ ] Pipeline page has no Phase 3 card
- [ ] Pipeline page has per-context shorthand toggles
- [ ] pipeline.yaml and config.yaml agree (sync mechanism works)
- [ ] All new tests pass
- [ ] Full test suite passes with no regressions
- [ ] TOKEN_BUDGET_GUIDE.md §10 matches implementation exactly

---

## 6. Rollout

**Order of operations** (each step is independently shippable):

1. **Remove the regex engine.** Delete shorthand.py, revert all hooks, remove
   /compact command, remove config section. Ship. This makes reality honest —
   nothing is worse than dead code that pretends to work.

2. **Add codebook constants.** Define SHORTHAND_CODEBOOK and
   SHORTHAND_INSTRUCTIONS in distillation.py. Add config toggles. Ship.
   Nothing uses them yet — this is just laying the foundation.

3. **Enhance Phase 2 prompts.** Wire codebook into web summariser, compressor,
   and fact extraction prompts behind toggles. Add tests. Ship. Now dynamic
   content can be compressed when toggled on.

4. **Fix Mission Control.** Remove Phase 3 card. Add per-context shorthand
   toggles. Fix pipeline.yaml ↔ config.yaml sync. Ship.

5. **Activate.** Toggle on one context at a time via MC. Monitor for quality
   regressions. Roll back per-context if needed.

Each step has a clean rollback: revert the commit. No step depends on
a later step. Enterprise excellence means every intermediate state is
production-safe.

---

## Progress Tracker

| Step | Description | Status | Commit |
|------|-------------|--------|--------|
| 1 | Remove regex engine | ✅ Done | `76831022` |
| 2 | Add codebook constants | ✅ Done | `1fb0f89f` |
| 3 | Enhance Phase 2 prompts | ✅ Done | `c52c8df1` |
| 4 | Fix Mission Control | ✅ Done | `2688358` (MC) + `2bc52699` (agent) |
| 5 | Activate + verify | ⬜ Ready | — |
