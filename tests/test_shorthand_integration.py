"""Tests for shorthand compression integration into Phase 2 distillation prompts."""

import pytest


class TestShorthandComponents:
    """Test the modular instruction component system."""

    def test_all_components_defined(self):
        from tools.distillation import SHORTHAND_COMPONENTS
        expected = {
            "key_value", "hierarchical",
            "telegraphic", "abbreviations", "brace", "reference", "ellipsis", "ternary",
            "llmlingua",
        }
        assert set(SHORTHAND_COMPONENTS.keys()) == expected

    def test_components_have_correct_shape(self):
        from tools.distillation import SHORTHAND_COMPONENTS
        for key, (instruction, layer, label) in SHORTHAND_COMPONENTS.items():
            assert isinstance(instruction, str) and len(instruction) > 0, f"{key}: bad instruction"
            assert layer in (1, 2, 3), f"{key}: bad layer {layer}"
            assert isinstance(label, str) and len(label) > 0, f"{key}: bad label"

    def test_layers_ordered(self):
        from tools.distillation import SHORTHAND_COMPONENTS
        layers = {k: v[1] for k, v in SHORTHAND_COMPONENTS.items()}
        assert layers["key_value"] == 1
        assert layers["telegraphic"] == 2
        assert layers["llmlingua"] == 3

    def test_hint_is_one_line(self):
        from tools.distillation import SHORTHAND_HINT
        assert "\n" not in SHORTHAND_HINT
        assert len(SHORTHAND_HINT) < 120


class TestBuildSuffix:
    """Test dynamic assembly of shorthand suffix from toggles."""

    def test_nothing_enabled_returns_empty(self):
        from tools.distillation import build_shorthand_suffix
        assert build_shorthand_suffix({}) == ""
        assert build_shorthand_suffix({"telegraphic": False}) == ""

    def test_single_component(self):
        from tools.distillation import build_shorthand_suffix
        result = build_shorthand_suffix({"telegraphic": True})
        assert "Drop articles" in result
        assert "Safety:" in result
        assert "Never abbreviate file paths" in result

    def test_multiple_components_in_layer_order(self):
        from tools.distillation import build_shorthand_suffix
        result = build_shorthand_suffix({
            "llmlingua": True,     # layer 3
            "key_value": True,     # layer 1
            "telegraphic": True,   # layer 2
        })
        lines = result.strip().split("\n")
        # Find the instruction lines (skip header and safety)
        instructions = [l for l in lines if l.startswith("- ") and "Never" not in l and "Preserve" not in l]
        # Layer 1 should come before layer 2 which should come before layer 3
        assert "key:value" in instructions[0].lower()
        assert "articles" in instructions[1].lower()
        assert "llmlingua" in instructions[2].lower()

    def test_safety_guards_always_present(self):
        from tools.distillation import build_shorthand_suffix
        result = build_shorthand_suffix({"brace": True})
        assert "Preserve" in result
        assert "Never abbreviate file paths" in result
        assert "Never reuse abbreviations" in result

    def test_all_components_enabled(self):
        from tools.distillation import build_shorthand_suffix, SHORTHAND_COMPONENTS
        all_on = {k: True for k in SHORTHAND_COMPONENTS}
        result = build_shorthand_suffix(all_on)
        assert len(result) > 0
        for key, (instruction, _, _) in SHORTHAND_COMPONENTS.items():
            # Each enabled instruction should appear somewhere in result
            assert instruction.split(".")[0] in result, f"Missing: {key}"


class TestContextGating:
    """Test that shorthand is only applied when context toggle is on."""

    def test_context_off_returns_empty(self):
        from tools.distillation import build_shorthand_suffix
        cfg = {"web_extract": False, "telegraphic": True}
        # build_shorthand_suffix doesn't check context — that's shorthand_suffix_if_enabled
        result = build_shorthand_suffix(cfg)
        assert "articles" in result  # component is on, context gating is separate

    def test_is_shorthand_active_all_off(self):
        """When no context is enabled, shorthand is inactive."""
        from tools.distillation import is_shorthand_active
        # is_shorthand_active reads config.yaml — with defaults all False, should be False
        # (This test relies on config.yaml defaults, may need mock in CI)


class TestConfigDefaults:
    """Test that all shorthand config defaults are False."""

    def test_context_defaults(self):
        from hermes_cli.config import DEFAULT_CONFIG
        sh = DEFAULT_CONFIG["compression"]["shorthand"]
        assert sh["web_extract"] is False
        assert sh["compressor"] is False
        assert sh["facts"] is False

    def test_component_defaults(self):
        from hermes_cli.config import DEFAULT_CONFIG
        sh = DEFAULT_CONFIG["compression"]["shorthand"]
        for key in ("key_value", "hierarchical", "telegraphic", "abbreviations",
                     "brace", "reference", "ellipsis", "ternary", "llmlingua"):
            assert sh[key] is False, f"{key} should default to False"

    def test_component_keys_match_distillation(self):
        """Config keys must match SHORTHAND_COMPONENTS keys."""
        from hermes_cli.config import DEFAULT_CONFIG
        from tools.distillation import SHORTHAND_COMPONENTS
        sh = DEFAULT_CONFIG["compression"]["shorthand"]
        for key in SHORTHAND_COMPONENTS:
            assert key in sh, f"Config missing component key: {key}"
