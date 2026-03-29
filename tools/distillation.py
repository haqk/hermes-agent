"""Shared distillation utilities — DRY module for pre-cleaning and LLM fallback.

Used by: web_tools, browser_tool, session_search_tool, context_compressor,
and the autosave pipeline (process_session.py).

SSOT for:
  - FALLBACK_MODEL constant
  - Haiku fallback call pattern
  - Text pre-cleaning primitives (dedup, whitespace, filler, JSON collapse)
  - Shorthand codebook and compression instructions (for Phase 2 LLM prompts)
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

DISTILLATION_FALLBACK_MODEL = "anthropic/claude-3.5-haiku"
DISTILLATION_FALLBACK_PROVIDER = "anthropic"


# ─── Fallback call ────────────────────────────────────────────────────────────

def call_with_haiku_fallback(
    call_fn,
    messages: list,
    task: str,
    *,
    model: str = None,
    max_tokens: int = 4000,
    temperature: float = 0.1,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_delay_base: int = 2,
    retry_delay_cap: int = 8,
) -> Optional[str]:
    """Call an LLM with retries and Haiku fallback. Returns content string or None.

    Args:
        call_fn: The LLM call function (call_llm or async_call_llm).
                 Must accept (task, messages, temperature, max_tokens, model, provider, timeout).
        messages: Chat messages list.
        task: Auxiliary task name.
        model: Primary model override (None = let auxiliary_client resolve).
        max_tokens: Max output tokens.
        temperature: Sampling temperature.
        timeout: Request timeout.
        max_retries: Retry attempts on the primary model.
        retry_delay_base: Base delay for exponential backoff.
        retry_delay_cap: Max delay between retries.

    Returns:
        Response content string, or None if all attempts fail.
    """
    import time as _time

    last_error = None

    for attempt in range(max_retries):
        try:
            call_kwargs = {
                "task": task,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
            if model:
                call_kwargs["model"] = model
            response = call_fn(**call_kwargs)
            content = response.choices[0].message.content
            if not isinstance(content, str):
                content = str(content) if content else ""
            return content.strip()
        except RuntimeError:
            logger.warning("%s: no auxiliary provider available", task)
            return None
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = min(retry_delay_base ** attempt, retry_delay_cap)
                logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %ds...",
                    task, attempt + 1, max_retries, str(e)[:100], delay,
                )
                _time.sleep(delay)
            else:
                # Primary exhausted — try Haiku once
                if model != DISTILLATION_FALLBACK_MODEL:
                    logger.warning(
                        "%s failed after %d retries. Falling back to %s",
                        task, max_retries, DISTILLATION_FALLBACK_MODEL,
                    )
                    try:
                        fallback_kwargs = {
                            "task": task,
                            "messages": messages,
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                            "timeout": timeout,
                            "model": DISTILLATION_FALLBACK_MODEL,
                            "provider": DISTILLATION_FALLBACK_PROVIDER,
                        }
                        response = call_fn(**fallback_kwargs)
                        content = response.choices[0].message.content
                        if not isinstance(content, str):
                            content = str(content) if content else ""
                        logger.info("%s: Haiku fallback succeeded", task)
                        return content.strip()
                    except Exception as fallback_error:
                        logger.warning(
                            "%s: Haiku fallback also failed: %s",
                            task, str(fallback_error)[:100],
                        )

    return None


# ─── Async variant ────────────────────────────────────────────────────────────

async def async_call_with_haiku_fallback(
    call_fn,
    messages: list,
    task: str,
    *,
    model: str = None,
    max_tokens: int = 4000,
    temperature: float = 0.1,
    timeout: float = 30.0,
    max_retries: int = 3,
    retry_delay_base: int = 2,
    retry_delay_cap: int = 60,
) -> Optional[str]:
    """Async version of call_with_haiku_fallback."""
    import asyncio

    last_error = None

    for attempt in range(max_retries):
        try:
            call_kwargs = {
                "task": task,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if model:
                call_kwargs["model"] = model
            response = await call_fn(**call_kwargs)
            return response.choices[0].message.content.strip()
        except RuntimeError:
            logger.warning("%s: no auxiliary provider available", task)
            return None
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = min(retry_delay_base ** attempt, retry_delay_cap)
                logger.warning(
                    "%s failed (attempt %d/%d): %s. Retrying in %ds...",
                    task, attempt + 1, max_retries, str(e)[:100], delay,
                )
                await asyncio.sleep(delay)
            else:
                if model != DISTILLATION_FALLBACK_MODEL:
                    logger.warning(
                        "%s failed after %d retries. Falling back to %s",
                        task, max_retries, DISTILLATION_FALLBACK_MODEL,
                    )
                    try:
                        fallback_kwargs = {
                            "task": task,
                            "messages": messages,
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                            "model": DISTILLATION_FALLBACK_MODEL,
                            "provider": DISTILLATION_FALLBACK_PROVIDER,
                        }
                        response = await call_fn(**fallback_kwargs)
                        logger.info("%s: Haiku fallback succeeded", task)
                        return response.choices[0].message.content.strip()
                    except Exception as fallback_error:
                        logger.warning(
                            "%s: Haiku fallback also failed: %s",
                            task, str(fallback_error)[:100],
                        )

    return None


# ─── Text cleaning primitives ─────────────────────────────────────────────────

def collapse_whitespace(text: str) -> str:
    """Collapse 3+ blank lines to 2, strip trailing whitespace per line."""
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return text


def dedup_lines(text: str, min_len: int = 10, max_len: int = 200) -> str:
    """Remove duplicate lines (exact match) keeping first occurrence.

    Only deduplicates lines between min_len and max_len characters
    to avoid removing intentionally repeated short content.
    """
    seen = set()
    result = []
    for line in text.split('\n'):
        stripped = line.strip()
        if stripped and min_len < len(stripped) < max_len:
            if stripped in seen:
                continue
            seen.add(stripped)
        result.append(line)
    return '\n'.join(result)


def collapse_json_blocks(text: str, min_size: int = 500) -> str:
    """Collapse large JSON-like blocks to first+last 3 lines."""
    def _collapse(m):
        block = m.group(0)
        lines = block.split('\n')
        if len(lines) <= 6:
            return block
        return '\n'.join(
            lines[:3]
            + [f'  ... [{len(lines) - 6} lines collapsed] ...']
            + lines[-3:]
        )
    return re.sub(rf'\{{[^{{}}]{{{min_size},}}\}}', _collapse, text, flags=re.DOTALL)


def strip_filler_lines(text: str, patterns: list[str]) -> str:
    """Remove lines matching any of the given regex patterns."""
    for pattern in patterns:
        text = re.sub(pattern, '', text, flags=re.MULTILINE)
    return text


# Common filler patterns for web content
WEB_FILLER_PATTERNS = [
    r'(?i)^.*click here to (?:learn|read|find out) more.*$',
    r'(?i)^.*read (?:our )?full (?:terms|privacy|cookie).*$',
    r'(?i)^.*we use cookies.*$',
    r'(?i)^.*accept (?:all|cookies).*$',
    r'(?i)^.*share (?:on|this|via) (?:twitter|facebook|linkedin|x|email|whatsapp).*$',
    r'(?i)^.*(?:follow us|connect with us) (?:on|@).*$',
    r'(?i)^.*subscribe to (?:our|the) newsletter.*$',
    r'(?i)^.*©\s*\d{4}.*all rights reserved.*$',
    r'(?i)^.*skip to (?:main |)content.*$',
    r'(?i)^.*back to top.*$',
    r'(?i)^.*table of contents.*$',
    r'(?i)^.*toggle (?:navigation|menu|sidebar).*$',
]

# Common filler patterns for assistant transcripts
TRANSCRIPT_FILLER_PATTERNS = [
    r'(?i)^\[ASSISTANT\]:\s*(?:Let me |I\'ll |I will |Sure, |OK, )(?:check|look|do|fix|find|read|search|run|try).*$',
    r'(?i)^\[ASSISTANT\]:\s*(?:Now |Next, |First, )(?:let me|I\'ll).*$',
]


# ─── Shorthand Compression ───────────────────────────────────────────────────
# Two pieces, two audiences. See TOKEN_BUDGET_GUIDE.md §10.
#
# SHORTHAND_PROMPT_SUFFIX — appended to Phase 2 LLM prompts (compressor side).
#   Tells the auxiliary model to produce telegraphic output.
#
# SHORTHAND_HINT — injected into the primary model's system prompt (reader side).
#   One-line hint so the model interprets shorthand rather than literal-reading.

SHORTHAND_PROMPT_SUFFIX = (
    "\n\nRespond in dense LLM-readable shorthand (LLMLingua/telegraphic style). "
    "Drop articles, copulas, filler verbs. Use standard abbreviations & symbols. "
    "Keep only high-perplexity tokens — omit what the reader would predict. "
    "Never abbreviate file paths, CLI flags, or error messages. "
    "Never reuse abbreviations ambiguously. Preserve values, numbers, proper nouns."
)

SHORTHAND_HINT = "Decompress & interpret: shorthand used in some context below."


def shorthand_suffix_if_enabled(context_key: str) -> str:
    """Return SHORTHAND_PROMPT_SUFFIX if the context toggle is on, else ''.

    Reads compression.shorthand.<context_key> from config.yaml.
    Safe to call from anywhere — returns '' on any error.
    """
    try:
        import yaml as _yaml
        from pathlib import Path
        cfg_path = Path.home() / ".hermes" / "config.yaml"
        if not cfg_path.exists():
            return ""
        with open(cfg_path) as f:
            cfg = _yaml.safe_load(f) or {}
        if cfg.get("compression", {}).get("shorthand", {}).get(context_key, False):
            return SHORTHAND_PROMPT_SUFFIX
    except Exception:
        pass
    return ""


def is_shorthand_active() -> bool:
    """Return True if ANY shorthand context is enabled."""
    return any(shorthand_suffix_if_enabled(k) for k in ("web_extract", "compressor", "facts"))
