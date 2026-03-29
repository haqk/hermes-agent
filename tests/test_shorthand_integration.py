"""Tests for shorthand compression integration into Phase 2 distillation prompts."""

import pytest


class TestShorthandConstants:
    """Test that the shorthand constants exist and are minimal."""

    def test_suffix_exists_and_compact(self):
        from tools.distillation import SHORTHAND_PROMPT_SUFFIX
        assert "shorthand" in SHORTHAND_PROMPT_SUFFIX.lower()
        assert len(SHORTHAND_PROMPT_SUFFIX) < 300

    def test_suffix_protects_technical_terms(self):
        from tools.distillation import SHORTHAND_PROMPT_SUFFIX
        assert "file paths" in SHORTHAND_PROMPT_SUFFIX.lower()

    def test_hint_exists_and_is_one_line(self):
        from tools.distillation import SHORTHAND_HINT
        assert "\n" not in SHORTHAND_HINT
        assert len(SHORTHAND_HINT) < 120

    def test_no_codebook_constant(self):
        """LLMs don't need a lookup table."""
        import tools.distillation as d
        assert not hasattr(d, "SHORTHAND_CODEBOOK")

    def test_no_instructions_constant(self):
        """Replaced by simple suffix."""
        import tools.distillation as d
        assert not hasattr(d, "SHORTHAND_INSTRUCTIONS")


class TestConfigDefaults:
    """Test that shorthand config defaults are all False."""

    def test_defaults_all_false(self):
        from hermes_cli.config import DEFAULT_CONFIG
        shorthand = DEFAULT_CONFIG["compression"]["shorthand"]
        assert shorthand["web_extract"] is False
        assert shorthand["compressor"] is False
        assert shorthand["facts"] is False
        assert len(shorthand) == 3

    def test_shorthand_active_flag(self):
        """Any toggle True → active. All False → inactive."""
        def is_active(cfg):
            return any(cfg.get(k, False) for k in ("web_extract", "compressor", "facts"))

        assert is_active({"web_extract": True, "compressor": False, "facts": False})
        assert is_active({"web_extract": False, "compressor": False, "facts": True})
        assert not is_active({"web_extract": False, "compressor": False, "facts": False})
        assert not is_active({})
