"""Tests for tools/shorthand.py — Phase 3 shorthand compression."""

import pytest
from tools.shorthand import (
    compact,
    expand,
    compact_if_enabled,
    compact_with_metrics,
    configure_from_dict,
    is_enabled,
    is_context_enabled,
    set_enabled,
    get_context_toggles,
    get_codebook_block,
    metrics,
    _compress,
    _expand,
    CODEBOOK,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_shorthand_state():
    """Reset global state between tests."""
    set_enabled(False)
    configure_from_dict({
        "tool_schemas": False,
        "tool_guidance": False,
        "platform_hints": False,
        "compressor": False,
        "facts": False,
    })
    metrics.reset()
    yield
    set_enabled(False)
    configure_from_dict({
        "tool_schemas": False,
        "tool_guidance": False,
        "platform_hints": False,
        "compressor": False,
        "facts": False,
    })


# ---------------------------------------------------------------------------
# Toggle / config tests
# ---------------------------------------------------------------------------

class TestToggles:
    def test_default_disabled(self):
        assert not is_enabled()
        for ctx in ["tool_schemas", "tool_guidance", "platform_hints", "compressor", "facts"]:
            assert not is_context_enabled(ctx)

    def test_configure_enables_global(self):
        configure_from_dict({"tool_schemas": True})
        assert is_enabled()
        assert is_context_enabled("tool_schemas")
        assert not is_context_enabled("compressor")

    def test_configure_all_on(self):
        configure_from_dict({k: True for k in get_context_toggles()})
        assert is_enabled()
        for ctx in get_context_toggles():
            assert is_context_enabled(ctx)

    def test_configure_all_off(self):
        configure_from_dict({k: True for k in get_context_toggles()})
        configure_from_dict({k: False for k in get_context_toggles()})
        # Global stays True because configure_from_dict only sets True
        # when any is True; need set_enabled(False) to turn off
        set_enabled(False)
        assert not is_enabled()

    def test_set_enabled(self):
        set_enabled(True)
        assert is_enabled()
        set_enabled(False)
        assert not is_enabled()

    def test_unknown_context(self):
        set_enabled(True)
        assert not is_context_enabled("nonexistent")

    def test_codebook_only_when_enabled(self):
        assert get_codebook_block() is None
        set_enabled(True)
        assert get_codebook_block() == CODEBOOK


# ---------------------------------------------------------------------------
# Compression tests — Tier 1 rules
# ---------------------------------------------------------------------------

class TestCompression:
    def test_empty_passthrough(self):
        assert compact("", force=True) == ""
        assert compact("  ", force=True) == "  "
        assert compact(None, force=True) is None

    def test_article_removal(self):
        result = _compress("the config file is ready")
        assert "the " not in result.lower().split("@")[0]  # Don't match inside protected
        assert "cfg" in result

    def test_copula_removal(self):
        result = _compress("server is running")
        assert "is " not in result

    def test_filler_phrase_removal(self):
        result = _compress("Please make sure to save the file")
        assert "please" not in result.lower()
        assert "make sure" not in result.lower()

    def test_standard_abbreviations(self):
        result = _compress("the configuration and implementation")
        assert "cfg" in result
        assert "impl" in result

    def test_abbreviation_function(self):
        result = _compress("define a function for authentication")
        assert "fn" in result
        assert "auth" in result

    def test_abbreviation_environment(self):
        result = _compress("check the environment variable")
        assert "env" in result

    def test_arrow_notation(self):
        result = _compress("build then deploy")
        assert "→" in result

    def test_colon_structure(self):
        result = _compress("output should be JSON")
        assert "output:JSON" in result

    def test_space_collapse(self):
        result = _compress("too   many   spaces")
        assert "   " not in result

    def test_symbol_space_removal(self):
        result = _compress("A then B")
        assert "→" in result
        assert "B" in result

    def test_combined_compression(self):
        text = "Please make sure to check the configuration file and then deploy"
        result = _compress(text)
        assert len(result) < len(text)
        assert "cfg" in result
        assert "→" in result

    def test_multiple_abbreviations(self):
        text = "the command takes an argument and returns a response"
        result = _compress(text)
        assert "cmd" in result
        assert "arg" in result
        assert "resp" in result


# ---------------------------------------------------------------------------
# Protected region tests
# ---------------------------------------------------------------------------

class TestProtectedRegions:
    def test_file_paths_preserved(self):
        result = _compress("check the file at ~/.hermes/config.yaml")
        assert "~/.hermes/config.yaml" in result

    def test_cli_flags_preserved(self):
        result = _compress("use the --verbose flag")
        assert "--verbose" in result

    def test_urls_preserved(self):
        result = _compress("visit https://example.com/path")
        assert "https://example.com/path" in result

    def test_constants_preserved(self):
        result = _compress("set HERMES_HOME to the directory")
        assert "HERMES_HOME" in result

    def test_backtick_code_preserved(self):
        result = _compress("run `npm install` in the directory")
        assert "`npm install`" in result

    def test_quoted_strings_preserved(self):
        result = _compress('set the value to "hello world"')
        assert '"hello world"' in result

    def test_version_numbers_preserved(self):
        result = _compress("requires Python 3.11.15")
        assert "3.11.15" in result


# ---------------------------------------------------------------------------
# Expansion tests
# ---------------------------------------------------------------------------

class TestExpansion:
    def test_abbreviation_expansion(self):
        result = _expand("check cfg and env")
        assert "config" in result or "configuration" in result
        assert "environment" in result

    def test_arrow_expansion(self):
        result = _expand("build→deploy→verify")
        assert "then" in result

    def test_symbol_expansion(self):
        result = _expand("∴ we must check")
        assert "therefore" in result

    def test_round_trip_preserves_meaning(self):
        """Compression then expansion should preserve semantic content."""
        original = "check the configuration file"
        compressed = _compress(original)
        expanded = _expand(compressed)
        # Key semantic words should survive round-trip
        assert "config" in expanded or "configuration" in expanded
        assert "file" in expanded


# ---------------------------------------------------------------------------
# Context-aware compression tests
# ---------------------------------------------------------------------------

class TestContextCompression:
    def test_compact_disabled_returns_original(self):
        text = "the configuration is ready"
        assert compact(text, context="tool_schemas") == text

    def test_compact_enabled_compresses(self):
        configure_from_dict({"tool_schemas": True})
        text = "the configuration is ready"
        result = compact(text, context="tool_schemas")
        assert result != text
        assert "cfg" in result

    def test_compact_wrong_context_returns_original(self):
        configure_from_dict({"tool_schemas": True})
        text = "the configuration is ready"
        result = compact(text, context="compressor")
        assert result == text

    def test_compact_force_overrides_toggles(self):
        text = "the configuration is ready"
        result = compact(text, force=True)
        assert "cfg" in result

    def test_compact_if_enabled_convenience(self):
        text = "the configuration is ready"
        assert compact_if_enabled(text, "tool_schemas") == text

        configure_from_dict({"tool_schemas": True})
        result = compact_if_enabled(text, "tool_schemas")
        assert "cfg" in result


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_metrics_tracking(self):
        configure_from_dict({"tool_schemas": True})
        text = "the configuration and implementation details"
        compact_with_metrics(text, context="tool_schemas")
        assert metrics.compressions == 1
        assert metrics.total_input_chars > 0
        assert metrics.total_output_chars > 0
        assert metrics.avg_reduction_pct > 0

    def test_metrics_no_change_no_record(self):
        # Very short text that doesn't change
        compact_with_metrics("hi", context="tool_schemas")
        assert metrics.compressions == 0

    def test_metrics_summary(self):
        assert "inactive" in metrics.summary()
        configure_from_dict({"tool_schemas": True})
        compact_with_metrics("the configuration details", context="tool_schemas")
        summary = metrics.summary()
        assert "compression" in summary
        assert "reduction" in summary

    def test_metrics_reset(self):
        configure_from_dict({"tool_schemas": True})
        compact_with_metrics("the configuration details", context="tool_schemas")
        metrics.reset()
        assert metrics.compressions == 0


# ---------------------------------------------------------------------------
# Real-world examples from TOKEN_BUDGET_GUIDE.md §10
# ---------------------------------------------------------------------------

class TestRealWorldExamples:
    def test_memory_entry_compression(self):
        text = (
            "The user prefers that we always commit and push after making code changes. "
            "The user is located in Australia and uses Telegram as the primary "
            "communication channel."
        )
        result = _compress(text)
        assert len(result) < len(text)
        # Should achieve meaningful reduction (articles, copulas, that, and)
        reduction = (1 - len(result) / len(text)) * 100
        assert reduction > 10, f"Only {reduction:.0f}% reduction"

    def test_guidance_compression(self):
        text = (
            "When you discover something new about the environment, such as the operating "
            "system, installed tools, or project structure, save it to memory. When the "
            "user corrects you, save that correction immediately so you don't repeat the "
            "mistake."
        )
        result = _compress(text)
        assert len(result) < len(text)
        reduction = (1 - len(result) / len(text)) * 100
        assert reduction > 10, f"Only {reduction:.0f}% reduction"

    def test_fact_compression(self):
        text = (
            "Cloudflare token at ~/.cloudflare_token has list-only permissions and "
            "returns 403 on DNS edit operations."
        )
        result = _compress(text)
        assert len(result) < len(text)
        # Path must be preserved
        assert "~/.cloudflare_token" in result

    def test_tool_schema_compression(self):
        """Test with an actual tool schema description snippet."""
        text = (
            "Execute shell commands on a Linux environment. Filesystem persists between calls. "
            "Do NOT use cat/head/tail to read files — use read_file instead. "
            "Do NOT use grep/rg/find to search — use search_files instead. "
            "Foreground (default): Commands return INSTANTLY when done. "
            "Background: ONLY for long-running servers, watchers, or processes that never exit."
        )
        result = _compress(text)
        assert len(result) < len(text)
        # Technical content is heavily protected, so lower threshold
        reduction = (1 - len(result) / len(text)) * 100
        assert reduction > 5, f"Only {reduction:.0f}% reduction on tool schema"
