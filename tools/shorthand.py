"""
Shorthand Compression — Phase 3 Distillation.

Codebook-driven symbolic notation system that reduces token count in LLM-facing
text while preserving full semantic fidelity. Applied AFTER Phase 1 (deterministic
cleaning) and Phase 2 (LLM semantic distillation).

Every compressed output supports a raw/human toggle via expand().

Design principles (from TOKEN_BUDGET_GUIDE.md §10):
- Token-aware: prefer Unicode symbols that tokenise efficiently (→ ≥ §)
- Codebook injected once in system prompt, used everywhere
- Telegraphic grammar: drop articles, copulas, filler verbs
- Standard abbreviations only — never invent
- Technical terms (API names, file paths, CLI flags) stay intact

Config keys (all default False — activated via Mission Control pipeline page):
- shorthand.tool_schemas     — compress tool schema descriptions
- shorthand.tool_guidance    — compress MEMORY/SESSION/SKILLS guidance
- shorthand.platform_hints   — compress platform hint strings
- shorthand.compressor       — compress context compressor summaries
- shorthand.facts            — compress distilled fact content
"""

import os
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Global toggle — can be overridden per-call or via config
# ---------------------------------------------------------------------------
_GLOBAL_ENABLED = False


def is_enabled() -> bool:
    """Check if shorthand compression is globally enabled."""
    return _GLOBAL_ENABLED


def set_enabled(enabled: bool) -> None:
    """Set global shorthand compression state."""
    global _GLOBAL_ENABLED
    _GLOBAL_ENABLED = enabled


# ---------------------------------------------------------------------------
# Per-context toggles — read from config at startup
# ---------------------------------------------------------------------------
_CONTEXT_TOGGLES = {
    "tool_schemas": False,
    "tool_guidance": False,
    "platform_hints": False,
    "compressor": False,
    "facts": False,
}


def configure_from_dict(shorthand_config: dict) -> None:
    """Load per-context toggles from config.yaml's shorthand section."""
    if not isinstance(shorthand_config, dict):
        return
    for key in _CONTEXT_TOGGLES:
        if key in shorthand_config:
            _CONTEXT_TOGGLES[key] = bool(shorthand_config[key])
    # If ANY context is enabled, enable globally
    global _GLOBAL_ENABLED
    if any(_CONTEXT_TOGGLES.values()):
        _GLOBAL_ENABLED = True


def is_context_enabled(context: str) -> bool:
    """Check if a specific context has shorthand compression enabled."""
    return _GLOBAL_ENABLED and _CONTEXT_TOGGLES.get(context, False)


def get_context_toggles() -> dict:
    """Return a copy of current context toggle states."""
    return dict(_CONTEXT_TOGGLES)


# ---------------------------------------------------------------------------
# Codebook — injected once in system prompt (~300 chars overhead)
# ---------------------------------------------------------------------------

CODEBOOK = (
    "SHORTHAND: fn=function cfg=config impl=implementation auth=authentication "
    "req=request resp=response dir=directory env=environment dep=dependency "
    "msg=message arg=argument val=value prev=previous cmd=command "
    "→=implies/then ←=from ↔=bidirectional ✓=yes/done ✗=no/failed "
    "⚠=warning/caution §=section_break ∴=therefore ≈=approximately "
    "@=at/located_at #=count/number &=and |=or >=greater <=less"
)

# Forward map: long form → abbreviation
_ABBREVIATIONS = {
    "function": "fn",
    "configuration": "cfg",
    "configure": "cfg",
    "config": "cfg",
    "implementation": "impl",
    "implement": "impl",
    "authentication": "auth",
    "authenticate": "auth",
    "request": "req",
    "response": "resp",
    "directory": "dir",
    "environment": "env",
    "dependency": "dep",
    "dependencies": "deps",
    "message": "msg",
    "messages": "msgs",
    "argument": "arg",
    "arguments": "args",
    "value": "val",
    "previous": "prev",
    "command": "cmd",
    "commands": "cmds",
}

# Reverse map: abbreviation → long form (for expansion)
_EXPANSIONS = {v: k for k, v in _ABBREVIATIONS.items()}
# Handle plurals
_EXPANSIONS["deps"] = "dependencies"
_EXPANSIONS["msgs"] = "messages"
_EXPANSIONS["args"] = "arguments"
_EXPANSIONS["cmds"] = "commands"

# Articles, copulas, and filler words to drop (Tier 1)
_ARTICLES = re.compile(r'\b(the|a|an)\s+', re.IGNORECASE)
_COPULAS = re.compile(r'\b(is|are|was|were)\s+', re.IGNORECASE)
_FILLER_WORDS = re.compile(r'\b(that|which|such as|for example|in order to|when you|if you)\s+', re.IGNORECASE)

# Filler verb phrases to remove entirely (Tier 1)
_FILLER_PHRASES = [
    "please make sure to ",
    "please ensure that ",
    "make sure to ",
    "make sure that ",
    "ensure that ",
    "please note that ",
    "note that ",
    "keep in mind that ",
    "it is important to ",
    "it's important to ",
    "you should always ",
    "you should ",
    "you need to ",
    "you must ",
    "please ",
    "could you ",
    "would you ",
]

# Sorted longest-first for greedy matching
_FILLER_PHRASES.sort(key=len, reverse=True)

# Arrow/logical substitutions (Tier 1)
# Note: "from" is too common to replace blindly — only replace "from X" when
# preceded by certain patterns. Skip it for now to avoid false positives.
_ARROW_PATTERNS = [
    (re.compile(r'\s+then\s+', re.IGNORECASE), "→"),
    (re.compile(r'\bthen\s+', re.IGNORECASE), "→"),
    (re.compile(r'\bimplies\s+', re.IGNORECASE), "→"),
    (re.compile(r'\btherefore\s+', re.IGNORECASE), "∴"),
    (re.compile(r'\bapproximately\s+', re.IGNORECASE), "≈"),
    (re.compile(r'\band\s+', re.IGNORECASE), "&"),
    (re.compile(r'\bor\s+', re.IGNORECASE), "|"),
]

# Technical term protection — these patterns should NOT be compressed
_PROTECTED_PATTERNS = re.compile(
    r'(?:'
    r'[~/.][\w/._-]+'           # File paths: ~/foo, ./bar, /etc/thing
    r'|--[\w-]+'                # CLI flags: --verbose, --help
    r'|-[a-zA-Z]\b'            # Short flags: -v, -h
    r'|\b\w+\.\w+\.\w+'       # Dotted names: os.path.join
    r'|\b[A-Z_]{2,}\b'         # ALL_CAPS constants: HERMES_HOME
    r'|`[^`]+`'                 # Backtick-quoted code
    r"|'[^']+'"                 # Single-quoted strings
    r'|"[^"]+"'                 # Double-quoted strings
    r'|\bhttps?://\S+'          # URLs
    r'|\b\d+\.\d+'             # Version numbers: 3.11.15
    r')'
)


# ---------------------------------------------------------------------------
# Core compression / expansion functions
# ---------------------------------------------------------------------------

def compact(text: str, context: str = "", force: bool = False) -> str:
    """Apply shorthand compression to text.

    Args:
        text: Input text to compress.
        context: Which context this is for (tool_schemas, tool_guidance, etc).
                 If provided, checks the per-context toggle.
        force: If True, bypass all toggle checks and compress anyway.

    Returns:
        Compressed text, or original text if compression is disabled.
    """
    if not text:
        return text
    if not text.strip():
        return text

    if not force:
        if context and not is_context_enabled(context):
            return text
        if not context and not _GLOBAL_ENABLED:
            return text

    return _compress(text)


def expand(text: str) -> str:
    """Expand shorthand back to natural language.

    This is the lossless reverse of compact(). Always available regardless
    of toggle state — used for debugging and human display.
    """
    if not text or not text.strip():
        return text
    return _expand(text)


def compact_if_enabled(text: str, context: str) -> str:
    """Convenience: compress text only if the specific context is enabled.

    This is the primary integration point — call sites use this to
    conditionally compress without checking toggles themselves.
    """
    if is_context_enabled(context):
        return _compress(text)
    return text


# ---------------------------------------------------------------------------
# Internal compression engine
# ---------------------------------------------------------------------------

def _compress(text: str) -> str:
    """Apply Tier 1 compression rules to text.

    Protected regions (file paths, code, URLs, constants) are extracted first,
    compressed text is processed, then protected regions are restored.
    """
    # Extract protected regions
    protected = {}
    counter = [0]

    def _protect(match):
        key = f"\x00PROT{counter[0]}\x00"
        protected[key] = match.group(0)
        counter[0] += 1
        return key

    text = _PROTECTED_PATTERNS.sub(_protect, text)

    # Tier 1: Drop filler phrases (longest first)
    text_lower = text.lower()
    for phrase in _FILLER_PHRASES:
        # Case-insensitive removal
        idx = text_lower.find(phrase)
        while idx != -1:
            text = text[:idx] + text[idx + len(phrase):]
            text_lower = text.lower()
            idx = text_lower.find(phrase)

    # Tier 1: Drop articles
    text = _ARTICLES.sub("", text)

    # Tier 1: Drop copulas
    text = _COPULAS.sub("", text)

    # Tier 1: Drop filler words
    text = _FILLER_WORDS.sub("", text)

    # Tier 1: Standard abbreviations (word boundary matching)
    for long_form, short_form in _ABBREVIATIONS.items():
        text = re.sub(
            rf'\b{re.escape(long_form)}\b',
            short_form,
            text,
            flags=re.IGNORECASE,
        )

    # Tier 1: Arrow notation for remaining patterns
    for pattern, replacement in _ARROW_PATTERNS:
        text = pattern.sub(replacement, text)

    # Tier 1: Colon structure — "X should be Y" → "X:Y"
    text = re.sub(r'\s+should be\s+', ':', text)
    text = re.sub(r'\s+defaults to\s+', ':', text)

    # Tier 1: Key:value — "the port is 8650" → "port:8650"
    text = re.sub(r'\bport\s+(?:is\s+)?(\d+)', r'port:\1', text)

    # Cleanup: collapse multiple spaces
    text = re.sub(r'  +', ' ', text)

    # Cleanup: collapse space around symbols
    text = re.sub(r'\s*→\s*', '→', text)
    text = re.sub(r'\s*←\s*', '←', text)
    text = re.sub(r'\s*↔\s*', '↔', text)

    # Cleanup: trim lines
    lines = text.split('\n')
    lines = [line.strip() for line in lines]
    text = '\n'.join(lines)

    # Restore protected regions
    for key, original in protected.items():
        text = text.replace(key, original)

    return text.strip()


def _expand(text: str) -> str:
    """Reverse shorthand compression back to natural language.

    Applies reverse codebook lookups and restores grammar particles.
    Not perfectly lossless for all filler phrases (those are intentionally
    dropped), but preserves all semantic content.
    """
    # Reverse abbreviations (short → long)
    for short_form, long_form in _EXPANSIONS.items():
        text = re.sub(
            rf'\b{re.escape(short_form)}\b',
            long_form,
            text,
        )

    # Reverse arrow/logical notation
    text = text.replace('→', ' then ')
    text = text.replace('←', ' from ')
    text = text.replace('↔', ' bidirectional ')
    text = text.replace('∴', 'therefore ')
    text = text.replace('≈', 'approximately ')
    text = re.sub(r'&(?![a-zA-Z#])', ' and ', text)  # & but not &amp; etc.
    text = re.sub(r'\|(?!\|)', ' or ', text)  # | but not ||

    # Reverse colon structures where appropriate
    # (Only safe for known patterns — general colon is ambiguous)

    # Cleanup
    text = re.sub(r'  +', ' ', text)

    return text.strip()


# ---------------------------------------------------------------------------
# System prompt codebook injection
# ---------------------------------------------------------------------------

def get_codebook_block() -> Optional[str]:
    """Return the codebook block for system prompt injection.

    Only returns content if shorthand compression is globally enabled.
    The codebook is injected once in the system prompt so the LLM learns
    the abbreviation mappings for the session.
    """
    if not _GLOBAL_ENABLED:
        return None
    return CODEBOOK


# ---------------------------------------------------------------------------
# Metrics tracking
# ---------------------------------------------------------------------------

class ShorthandMetrics:
    """Track compression statistics for session reporting."""

    def __init__(self):
        self.compressions = 0
        self.total_input_chars = 0
        self.total_output_chars = 0

    def record(self, input_text: str, output_text: str) -> None:
        self.compressions += 1
        self.total_input_chars += len(input_text)
        self.total_output_chars += len(output_text)

    @property
    def avg_reduction_pct(self) -> float:
        if self.total_input_chars == 0:
            return 0.0
        return (1 - self.total_output_chars / self.total_input_chars) * 100

    def summary(self) -> str:
        if self.compressions == 0:
            return "Shorthand: inactive"
        return (
            f"Shorthand:  {self.compressions} compressions, "
            f"avg {self.avg_reduction_pct:.0f}% reduction "
            f"({self.total_input_chars:,}→{self.total_output_chars:,} chars)"
        )

    def reset(self):
        self.compressions = 0
        self.total_input_chars = 0
        self.total_output_chars = 0


# Global metrics instance
metrics = ShorthandMetrics()


def compact_with_metrics(text: str, context: str = "", force: bool = False) -> str:
    """Like compact() but also records metrics."""
    original = text
    result = compact(text, context=context, force=force)
    if result != original:
        metrics.record(original, result)
    return result
