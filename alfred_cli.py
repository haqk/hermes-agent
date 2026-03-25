#!/usr/bin/env python3
"""
AlfredCLI — Wrapper CLI extending Hermes with system status in the status bar.

Overrides _get_status_bar_fragments() to show system metrics alongside
the standard model/context/duration info. One unified bar:

  🧠 M:99% U:89% │ 📚 105 │ 🔬 20/20 │ 📖 17 │ ⏰ 13 │ ⚕ claude-opus │ 149K/1M │ [██░░░░] 15% │ 1h 40m

Uses Hermes extension hooks — no patching.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli import HermesCLI

STATUS_FILE = os.path.expanduser("~/.hermes/system_status.json")
STATUS_REFRESH_SCRIPT = os.path.expanduser("~/.hermes/scripts/update_status.py")
STATUS_CACHE_TTL = 30  # seconds between re-reads


class AlfredCLI(HermesCLI):
    """Extended CLI with system status in the status bar."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._system_status_visible = True
        self._status_cache = {}
        self._status_cache_time = 0
        self._status_refresh_lock = threading.Lock()
        self._bg_refresh_status()

    def _bg_refresh_status(self):
        """Refresh status file in background thread."""
        def _refresh():
            try:
                subprocess.run(
                    [sys.executable, STATUS_REFRESH_SCRIPT],
                    capture_output=True, timeout=15,
                )
            except Exception:
                pass
        threading.Thread(target=_refresh, daemon=True).start()

    def _read_system_status(self):
        """Read system status with TTL cache."""
        now = time.time()
        if now - self._status_cache_time < STATUS_CACHE_TTL and self._status_cache:
            return self._status_cache
        with self._status_refresh_lock:
            if now - self._status_cache_time < STATUS_CACHE_TTL and self._status_cache:
                return self._status_cache
            try:
                with open(STATUS_FILE) as f:
                    self._status_cache = json.load(f)
                self._status_cache_time = time.time()
            except (FileNotFoundError, json.JSONDecodeError):
                self._status_cache = {}
            return self._status_cache

    def _format_time_until(self, iso_time):
        """Format time until next event as compact string."""
        if not iso_time:
            return "—"
        try:
            target = datetime.fromisoformat(iso_time)
            now = datetime.now().astimezone()
            total_secs = int((target - now).total_seconds())
            if total_secs < 0:
                return "now"
            if total_secs < 60:
                return f"{total_secs}s"
            if total_secs < 3600:
                return f"{total_secs // 60}m"
            h = total_secs // 3600
            m = (total_secs % 3600) // 60
            return f"{h}h{m}m" if m else f"{h}h"
        except (ValueError, TypeError):
            return "—"

    def _format_burn_rate(self, tpm):
        """Format tokens-per-minute as compact string."""
        if not tpm or tpm <= 0:
            return "idle"
        if tpm >= 1_000_000:
            return f"{tpm / 1_000_000:.1f}M/m"
        if tpm >= 1_000:
            return f"{tpm / 1_000:.0f}K/m"
        return f"{tpm:.0f}/m"

    def _zone_indicator(self, zone):
        """Return (emoji, style) for a Token OS zone."""
        zone = (zone or "unknown").lower()
        zone_map = {
            "green": ("🟢", "class:status-bar-good"),
            "yellow": ("🟡", "class:status-bar"),
            "red": ("🔴", "class:status-bar-bad"),
            "emergency": ("⚫", "class:status-bar-bad"),
        }
        return zone_map.get(zone, ("⚪", "class:status-bar-dim"))

    def _token_os_fragments(self, width):
        """Build Token OS quota/zone fragments for the status bar.

        Formats:
          Narrow (>=80):  ⚡ 🟢
          Medium (>=100): ⚡ 🟢 324K/m
          Wide (>=130):   ⚡ 🟢 324K/m ↻2h3m
          Forecast:       ⚡ 🟡 324K/m ⏳1h2m   (exhaustion predicted)
        """
        ss = self._read_system_status()
        tos = ss.get("token_os", {})
        if not tos or tos.get("alive") is False:
            return []

        dim = "class:status-bar-dim"
        zone = tos.get("system_zone", "unknown")
        emoji, zone_style = self._zone_indicator(zone)

        frags = []
        frags.append((dim, "⚡ "))
        frags.append((zone_style, emoji))

        # Token quota: remaining% from rate-limit headers or daily tracking
        active_prov = tos.get("providers", {}).get("anthropic", {})
        remaining = active_prov.get("tokens_remaining")
        limit = active_prov.get("tokens_limit")
        remaining_scale = active_prov.get("remaining_scale")
        util_pct = active_prov.get("utilization_pct")
        daily_used = active_prov.get("daily_tokens_used", 0)
        daily_limit = active_prov.get("daily_tokens_limit", 0)

        remaining_pct = None
        if remaining_scale == "percentage" and remaining is not None:
            # OAuth unified headers: tokens_remaining IS already 0-100%
            remaining_pct = max(0, min(100, remaining))
        elif remaining is not None and limit and limit > 0:
            # Console API key: literal token counts
            remaining_pct = max(0, round(remaining / limit * 100))
        elif daily_limit and daily_limit > 0:
            # Daily tracking (cerebras, groq)
            used_pct = util_pct if util_pct is not None else round(daily_used / daily_limit * 100)
            remaining_pct = max(0, 100 - used_pct)

        if remaining_pct is not None:
            quota_style = "class:status-bar-bad" if remaining_pct < 20 else (
                "class:status-bar-warn" if remaining_pct < 40 else dim)
            frags.append((dim, " "))
            frags.append((quota_style, f"{remaining_pct}%"))

        # Active provider burn rate (show the one we're actually using)
        burn = active_prov.get("burn_rate_tpm", 0)

        if width >= 100 and burn > 0:
            frags.append((dim, " "))
            frags.append((zone_style, self._format_burn_rate(burn)))

        # Reset or exhaustion forecast
        if width >= 130:
            exhaust = tos.get("forecast_exhaustion_at")
            reset_at = active_prov.get("reset_at")
            if exhaust and zone in ("yellow", "red", "emergency"):
                frags.append((dim, " "))
                frags.append(("class:status-bar-bad", f"⏳{self._format_time_until(exhaust)}"))
            elif reset_at:
                frags.append((dim, " "))
                frags.append((dim, f"↻{self._format_time_until(reset_at)}"))

        return frags

    def _system_status_fragments(self, width):
        """Build system status fragments for the left side of the bar."""
        ss = self._read_system_status()
        if not ss:
            return []

        frags = []
        dim = "class:status-bar-dim"
        normal = "class:status-bar"
        warn = "class:status-bar-bad"
        good = "class:status-bar-good"

        # Token OS quota zone (highest priority — shows first)
        tos_frags = self._token_os_fragments(width)
        if tos_frags:
            frags.extend(tos_frags)
            frags.append((dim, " │ "))

        # Memory
        mem = ss.get("memory", {})
        mem_pct = mem.get("mem_pct", 0)
        user_pct = mem.get("user_pct", 0)
        ms = warn if mem_pct >= 90 else normal
        us = warn if user_pct >= 90 else normal
        frags.append((dim, "🧠 "))
        frags.append((ms, f"M:{mem_pct}%"))
        frags.append((dim, " "))
        frags.append((us, f"U:{user_pct}%"))

        # Skills
        if width >= 100:
            skills_count = ss.get("skills", {}).get("count", 0)
            frags.append((dim, " │ "))
            frags.append((dim, "📚 "))
            frags.append((normal, f"{skills_count}"))

        # Penny
        penny = ss.get("penny", {})
        alive = penny.get("alive", False)
        score = penny.get("last_score", "—")
        frags.append((dim, " │ "))
        if alive:
            frags.append((dim, "🔬 "))
            frags.append((good, f"{score}"))
        else:
            frags.append((dim, "💀 "))
            frags.append((warn, f"{score}"))

        # Vault
        if width >= 120:
            vault_notes = ss.get("vault", {}).get("notes", 0)
            frags.append((dim, " │ "))
            frags.append((dim, "📖 "))
            frags.append((normal, f"{vault_notes}"))

        # Crons
        if width >= 140:
            crons = ss.get("crons", {})
            active = crons.get("active", 0)
            next_name = crons.get("next_name", "")
            next_time = self._format_time_until(crons.get("next_run"))
            frags.append((dim, " │ "))
            frags.append((dim, "⏰ "))
            frags.append((normal, f"{active}"))
            if next_name:
                short = next_name[:10] + "…" if len(next_name) > 10 else next_name
                frags.append((dim, f" {short} "))
                frags.append((normal, next_time))

        # Peer learning
        if width >= 160:
            pl = ss.get("peer_learning", {})
            total = pl.get("total", 0)
            if total > 0:
                frags.append((dim, " │ "))
                frags.append((dim, "🤝 "))
                frags.append((normal, f"{total}"))

        return frags

    def _get_status_bar_fragments(self):
        """Override: unified bar with system status + model/context/duration."""
        if not self._status_bar_visible:
            return []
        try:
            snapshot = self._get_status_bar_snapshot()
            width = shutil.get_terminal_size((80, 24)).columns
            duration_label = snapshot["duration"]
            percent = snapshot["context_percent"]
            percent_label = f"{percent}%" if percent is not None else "--"

            dim = "class:status-bar-dim"
            normal = "class:status-bar"
            strong = "class:status-bar-strong"

            frags = [("class:status-bar", " ")]

            # Standard hermes status (left side)
            if width < 52:
                frags.append((normal, "⚕ "))
                frags.append((strong, snapshot["model_short"]))
                frags.append((dim, " · "))
                frags.append((dim, duration_label))
            elif width < 76:
                frags.append((normal, "⚕ "))
                frags.append((strong, snapshot["model_short"]))
                frags.append((dim, " · "))
                frags.append((self._status_bar_context_style(percent), percent_label))
                frags.append((dim, " · "))
                frags.append((dim, duration_label))
            else:
                if snapshot["context_length"]:
                    from cli import _format_context_length, format_token_count_compact
                    ctx_total = _format_context_length(snapshot["context_length"])
                    ctx_used = format_token_count_compact(snapshot["context_tokens"])
                    context_label = f"{ctx_used}/{ctx_total}"
                else:
                    context_label = "ctx --"

                bar_style = self._status_bar_context_style(percent)
                frags.append((normal, "⚕ "))
                frags.append((strong, snapshot["model_short"]))
                frags.append((dim, " │ "))
                frags.append((dim, context_label))
                frags.append((dim, " │ "))
                frags.append((bar_style, self._build_context_bar(percent)))
                frags.append((dim, " "))
                frags.append((bar_style, percent_label))
                frags.append((dim, " │ "))
                frags.append((dim, duration_label))

            # System status (right side) — only if visible and wide enough
            if self._system_status_visible and width >= 80:
                sys_frags = self._system_status_fragments(width)
                if sys_frags:
                    frags.append((dim, " │ "))
                    frags.extend(sys_frags)

            frags.append(("class:status-bar", " "))
            return frags
        except Exception:
            return [("class:status-bar", f" {self._build_status_bar_text()} ")]

    def _register_extra_tui_keybindings(self, kb, *, input_area):
        """F3 toggles system status, F4 refreshes it."""
        cli_ref = self

        @kb.add("f3")
        def _toggle(event):
            cli_ref._system_status_visible = not cli_ref._system_status_visible

        @kb.add("f4")
        def _refresh(event):
            cli_ref._bg_refresh_status()
            cli_ref._status_cache_time = 0

    def process_command(self, cmd: str) -> bool:
        """Add /sys and /dashboard commands."""
        stripped = cmd.strip().lower()
        if stripped in ("/sys", "/system-status", "/dashboard"):
            self._bg_refresh_status()
            self._status_cache_time = 0
            self._system_status_visible = True
            print("  System status refreshed. F3 to toggle, F4 to refresh.")
            return True
        return super().process_command(cmd)


def main():
    """Entry point — swaps HermesCLI for AlfredCLI."""
    import cli as cli_module
    cli_module.HermesCLI = AlfredCLI
    from hermes_cli.main import main as hermes_main
    hermes_main()


if __name__ == "__main__":
    main()
