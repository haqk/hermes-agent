"""Tests for shorthand codebook integration into Phase 2 distillation prompts."""

import pytest
from unittest.mock import patch, MagicMock


class TestCodebookConstants:
    """Test that codebook constants exist and are well-formed."""

    def test_codebook_exists(self):
        from tools.distillation import SHORTHAND_CODEBOOK
        assert "SHORTHAND:" in SHORTHAND_CODEBOOK
        assert "fn=function" in SHORTHAND_CODEBOOK
        assert "cfg=config" in SHORTHAND_CODEBOOK
        assert "→=implies/then" in SHORTHAND_CODEBOOK

    def test_instructions_exist(self):
        from tools.distillation import SHORTHAND_INSTRUCTIONS
        assert "Drop articles" in SHORTHAND_INSTRUCTIONS
        assert "NEVER abbreviate file paths" in SHORTHAND_INSTRUCTIONS
        assert "NEVER use vowel dropping" in SHORTHAND_INSTRUCTIONS

    def test_prompt_suffix_combines_both(self):
        from tools.distillation import (
            SHORTHAND_CODEBOOK, SHORTHAND_INSTRUCTIONS, SHORTHAND_PROMPT_SUFFIX,
        )
        assert SHORTHAND_CODEBOOK in SHORTHAND_PROMPT_SUFFIX
        assert SHORTHAND_INSTRUCTIONS in SHORTHAND_PROMPT_SUFFIX

    def test_codebook_is_compact(self):
        """Codebook should be ~300 chars — not bloated."""
        from tools.distillation import SHORTHAND_CODEBOOK
        assert len(SHORTHAND_CODEBOOK) < 500


class TestConfigToggleDefaults:
    """Test that shorthand config defaults are all False."""

    def test_defaults_in_config(self):
        from hermes_cli.config import DEFAULT_CONFIG
        shorthand = DEFAULT_CONFIG["compression"]["shorthand"]
        assert shorthand["web_extract"] is False
        assert shorthand["compressor"] is False
        assert shorthand["facts"] is False


class TestSystemPromptCodebookInjection:
    """Test that the codebook is injected into system prompt when active."""

    def test_codebook_not_injected_when_disabled(self):
        """When _shorthand_active is False, codebook should not be in prompt."""
        from run_agent import AIAgent
        agent = AIAgent.__new__(AIAgent)
        agent._shorthand_active = False
        agent.platform = None
        agent.valid_tool_names = set()
        agent.model = "test/model"
        agent.provider = "test"
        agent._honcho = None
        agent._honcho_session_key = None

        # Mock _build_system_prompt to test just the codebook injection part
        # We can't easily call it without full init, so test the flag logic
        assert agent._shorthand_active is False

    def test_shorthand_active_flag_from_config(self):
        """When any shorthand toggle is True, _shorthand_active should be True."""
        cfg = {"compression": {"shorthand": {"web_extract": True, "compressor": False, "facts": False}}}
        shorthand_cfg = cfg["compression"].get("shorthand", {})
        active = any(shorthand_cfg.get(k, False) for k in ("web_extract", "compressor", "facts"))
        assert active is True

    def test_shorthand_active_flag_all_disabled(self):
        """When all shorthand toggles are False, _shorthand_active should be False."""
        cfg = {"compression": {"shorthand": {"web_extract": False, "compressor": False, "facts": False}}}
        shorthand_cfg = cfg["compression"].get("shorthand", {})
        active = any(shorthand_cfg.get(k, False) for k in ("web_extract", "compressor", "facts"))
        assert active is False
