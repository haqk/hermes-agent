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

        # Token quota: utilization from rate-limit headers or daily tracking
        active_prov = tos.get("providers", {}).get("anthropic", {})
        remaining = active_prov.get("tokens_remaining")
        limit = active_prov.get("tokens_limit")
        remaining_scale = active_prov.get("remaining_scale")
        util_pct = active_prov.get("utilization_pct")
        daily_used = active_prov.get("daily_tokens_used", 0)
        daily_limit = active_prov.get("daily_tokens_limit", 0)

        used_pct = None
        if remaining_scale == "percentage" and remaining is not None:
            # OAuth unified headers: tokens_remaining IS already 0-100%
            used_pct = max(0, min(100, 100 - remaining))
        elif remaining is not None and limit and limit > 0:
            # Console API key: literal token counts
            used_pct = max(0, min(100, round((1 - remaining / limit) * 100)))
        elif daily_limit and daily_limit > 0:
            # Daily tracking (cerebras, groq)
            used_pct = util_pct if util_pct is not None else round(daily_used / daily_limit * 100)
            used_pct = max(0, min(100, used_pct))

        # Active provider burn rate (show the one we're actually using)
        burn = active_prov.get("burn_rate_tpm", 0)

        if width >= 100 and burn > 0:
            frags.append((dim, " "))
            frags.append((zone_style, self._format_burn_rate(burn)))

        if used_pct is not None:
            # [█░░░░░░░░░] 9%  — 10 chars, filled proportionally
            bar_len = 10
            filled = round(used_pct / 100 * bar_len)
            bar = "█" * filled + "░" * (bar_len - filled)
            frags.append((dim, " ["))
            frags.append((zone_style, bar))
            frags.append((dim, "] "))
            frags.append((zone_style, f"{used_pct}%"))

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

        # Skills count (≥100 cols)
        if width >= 100:
            skills = ss.get("skills", {})
            skill_count = skills.get("count", 0)
            if skill_count:
                frags.append((dim, " │ "))
                frags.append((dim, f"📚 {skill_count}"))

        # Vault note count (≥120 cols)
        if width >= 120:
            vault = ss.get("vault", {})
            note_count = vault.get("notes", 0)
            if note_count:
                frags.append((dim, " │ "))
                frags.append((dim, f"📖 {note_count}"))

        # Crons (≥140 cols)
        if width >= 140:
            crons = ss.get("crons", {})
            active = crons.get("active", 0)
            if active:
                frags.append((dim, " │ "))
                frags.append((dim, f"⏰ {active}"))
                next_name = crons.get("next_name")
                if next_name and width >= 170:
                    frags.append((dim, f" {next_name}"))

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

    def _show_status_help(self):
        """Print annotated status bar with live values explaining every segment.

        Renders the live status bar as plain text, then prints a bracket
        annotation line underneath pointing at each section — sitting
        directly above the real status bar so it visually annotates it.
        """
        from cli import _cprint, format_token_count_compact, _format_context_length
        import unicodedata

        snapshot = self._get_status_bar_snapshot()
        ss = self._read_system_status()
        tos = ss.get("token_os", {}) if ss else {}
        mem = ss.get("memory", {}) if ss else {}
        penny = ss.get("penny", {}) if ss else {}
        active_prov = tos.get("providers", {}).get("anthropic", {})
        width = shutil.get_terminal_size((80, 24)).columns

        # -- Build the plain-text status line from live fragments --
        # We reconstruct it the same way _get_status_bar_fragments does,
        # but track section boundaries for annotation.

        def _display_width(s):
            """Calculate display width accounting for wide chars (CJK, emoji)."""
            w = 0
            for ch in s:
                cat = unicodedata.east_asian_width(ch)
                w += 2 if cat in ("W", "F") else 1
            return w

        model = snapshot.get("model_short", "?")
        ctx_tokens = snapshot.get("context_tokens", 0)
        ctx_length = snapshot.get("context_length") or 0
        ctx_pct = snapshot.get("context_percent")
        duration = snapshot.get("duration", "?")
        compressions = snapshot.get("compressions", 0)

        # Build hermes core section text
        if ctx_length:
            ctx_total = _format_context_length(ctx_length)
            ctx_used = format_token_count_compact(ctx_tokens)
            context_label = f"{ctx_used}/{ctx_total}"
        else:
            context_label = "ctx --"

        pct_label = f"{ctx_pct}%" if ctx_pct is not None else "--"
        bar_len = 10
        if ctx_pct is not None:
            filled = round(ctx_pct / 100 * bar_len)
            ctx_bar = "█" * filled + "░" * (bar_len - filled)
        else:
            ctx_bar = "░" * bar_len

        hermes_section = f" ⚕ {model} │ {context_label} │ [{ctx_bar}] {pct_label} │ {duration}"

        # Build token OS section text
        tos_alive = bool(tos) and tos.get("alive") is not False
        token_os_section = ""
        if tos_alive and width >= 80:
            zone = tos.get("system_zone", "unknown")
            emoji, _ = self._zone_indicator(zone)
            burn = active_prov.get("burn_rate_tpm", 0)
            remaining = active_prov.get("tokens_remaining")
            remaining_scale = active_prov.get("remaining_scale")
            reset_at = active_prov.get("reset_at")

            parts = [f"⚡ {emoji}"]

            if width >= 100 and burn > 0:
                parts.append(self._format_burn_rate(burn))

            used_pct = None
            if remaining_scale == "percentage" and remaining is not None:
                used_pct = max(0, min(100, 100 - remaining))
            elif remaining is not None and active_prov.get("tokens_limit"):
                lim = active_prov["tokens_limit"]
                if lim > 0:
                    used_pct = max(0, min(100, round((1 - remaining / lim) * 100)))

            if used_pct is not None:
                filled = round(used_pct / 100 * bar_len)
                qbar = "█" * filled + "░" * (bar_len - filled)
                parts.append(f"[{qbar}] {used_pct}%")

            if width >= 130 and reset_at:
                exhaust = tos.get("forecast_exhaustion_at")
                if exhaust and zone in ("yellow", "red", "emergency"):
                    parts.append(f"⏳{self._format_time_until(exhaust)}")
                else:
                    parts.append(f"↻{self._format_time_until(reset_at)}")

            token_os_section = " ".join(parts)

        # Build system status section text (mirrors _system_status_fragments)
        sys_section = ""
        if ss and width >= 80:
            mem_pct = mem.get("mem_pct", 0)
            user_pct = mem.get("user_pct", 0)
            sys_parts = [f"🧠 M:{mem_pct}% U:{user_pct}%"]

            if width >= 100:
                skill_count = ss.get("skills", {}).get("count", 0)
                if skill_count:
                    sys_parts.append(f"📚 {skill_count}")
            if width >= 120:
                note_count = ss.get("vault", {}).get("notes", 0)
                if note_count:
                    sys_parts.append(f"📖 {note_count}")
            if width >= 140:
                cron_active = ss.get("crons", {}).get("active", 0)
                if cron_active:
                    cron_str = f"⏰ {cron_active}"
                    next_name = ss.get("crons", {}).get("next_name")
                    if next_name and width >= 170:
                        cron_str += f" {next_name}"
                    sys_parts.append(cron_str)

            sys_section = " │ ".join(sys_parts)

        # Assemble full status line
        sections = [hermes_section]
        if token_os_section:
            sections.append(token_os_section)
        if sys_section:
            sections.append(sys_section)
        status_line = " │ ".join(sections) + " "

        # -- Build annotation line --
        # Measure each section's display width and create ╭─ label ─╮ brackets
        section_labels = ["hermes core (cli.py)"]
        section_texts = [hermes_section]
        if token_os_section:
            section_labels.append("token_os (alfred_cli.py)")
            section_texts.append(token_os_section)
        if sys_section:
            section_labels.append("system_status")
            section_texts.append(sys_section)

        # Calculate positions
        annotation = []
        pos = 0
        for i, (text, label) in enumerate(zip(section_texts, section_labels)):
            sec_width = _display_width(text)
            # Account for " │ " separator (3 chars) between sections
            if i > 0:
                annotation.append("   ")  # space for " │ "
                pos += 3

            # Build ╭─ label ─╮ centered in the section width
            inner = f" {label} "
            inner_len = len(inner)
            if sec_width <= inner_len + 2:
                # Section too narrow — just use the label
                bracket = f"╭{inner}╮"
            else:
                left_dash = (sec_width - inner_len - 2) // 2
                right_dash = sec_width - inner_len - 2 - left_dash
                bracket = f"╭{'─' * left_dash}{inner}{'─' * right_dash}╮"

            # Pad or trim to match section width
            if len(bracket) < sec_width:
                bracket = bracket + " " * (sec_width - len(bracket))
            elif len(bracket) > sec_width:
                bracket = bracket[:sec_width]

            annotation.append(bracket)
            pos += sec_width

        annotation_line = "".join(annotation)

        # -- Print the help content --
        _cprint("")
        _cprint("  \033[1mStatus Bar — Live Annotated View\033[0m")
        _cprint(f"  \033[2mTerminal width: {width} cols\033[0m")
        _cprint("")

        # Section details with live values
        def fmt_tokens(n):
            if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
            if n >= 1_000: return f"{n/1_000:.1f}K"
            return str(n)

        _cprint("  \033[1m── Hermes Core ──\033[0m")
        _cprint(f"    ⚕ Model:        \033[1m{model}\033[0m")
        ctx_str = f"{fmt_tokens(ctx_tokens)}/{fmt_tokens(ctx_length) if ctx_length else '?'}"
        _cprint(f"    Context:        {ctx_str} tokens")
        _cprint(f"    Context bar:    [{ctx_bar}] {pct_label}")
        _cprint(f"    Duration:       {duration}")
        if compressions:
            _cprint(f"    Compressions:   {compressions}x")
        _cprint("")

        if tos_alive:
            zone = tos.get("system_zone", "unknown")
            emoji, _ = self._zone_indicator(zone)
            burn = active_prov.get("burn_rate_tpm", 0)
            _cprint("  \033[1m── Token OS Quota ──\033[0m")
            _cprint(f"    ⚡ Zone:         {emoji} {zone}")
            _cprint(f"    Burn rate:      {self._format_burn_rate(burn) if burn else 'idle'}")
            reset_at = active_prov.get("reset_at")
            if reset_at:
                _cprint(f"    Reset:          ↻{self._format_time_until(reset_at)}")
            exhaust = tos.get("forecast_exhaustion_at")
            if exhaust and zone in ("yellow", "red", "emergency"):
                _cprint(f"    ⏳ Exhaustion:   {self._format_time_until(exhaust)}")
        else:
            _cprint("  \033[1m── Token OS Quota ──\033[0m")
            _cprint("    \033[2mNot connected\033[0m")
        _cprint("")

        if ss:
            mem_pct = mem.get("mem_pct", 0)
            user_pct = mem.get("user_pct", 0)
            _cprint("  \033[1m── System Status ──\033[0m")
            m_warn = " ⚠" if mem_pct >= 90 else ""
            u_warn = " ⚠" if user_pct >= 90 else ""
            _cprint(f"    🧠 Memory:       M:{mem_pct}%{m_warn}  U:{user_pct}%{u_warn}")
            skill_count = ss.get("skills", {}).get("count", 0)
            vis = "" if width >= 100 else "  \033[2m(hidden: need ≥100 cols)\033[0m"
            _cprint(f"    📚 Skills:       {skill_count}{vis}")
            note_count = ss.get("vault", {}).get("notes", 0)
            vis = "" if width >= 120 else "  \033[2m(hidden: need ≥120 cols)\033[0m"
            _cprint(f"    📖 Vault:        {note_count} notes{vis}")
            crons = ss.get("crons", {})
            cron_active = crons.get("active", 0)
            next_name = crons.get("next_name", "—")
            vis = "" if width >= 140 else "  \033[2m(hidden: need ≥140 cols)\033[0m"
            _cprint(f"    ⏰ Crons:        {cron_active} active, next: {next_name}{vis}")
        _cprint("")

        _cprint("  \033[1m── Keybindings ──\033[0m")
        _cprint("    F3              Toggle system status visibility")
        _cprint("    F4              Force refresh status data")
        _cprint("    /sys            Refresh + show system status")
        _cprint("")

        _cprint("  \033[1m── Zone Legend ──\033[0m")
        _cprint("    🟢 Green   >50% remaining   all tiers (P0-P4)")
        _cprint("    🟡 Yellow  20-50%           P0-P3 only")
        _cprint("    🔴 Red     5-20%            P0-P2 only")
        _cprint("    ⚫ Emergency <5%            P0 only")
        _cprint("")

        # Distillation metrics (if any web extracts happened this session)
        try:
            from tools.web_tools import distillation_metrics
            dm = distillation_metrics.snapshot()
            if dm["pre_clean_calls"] > 0 or dm["distiller_calls"] > 0:
                _cprint("  \033[1m── Distillation (this session) ──\033[0m")
                if dm["pre_clean_calls"] > 0:
                    _cprint(f"    Pre-clean:      {dm['pre_clean_calls']} pages, avg {dm['avg_pre_clean_reduction_pct']}% reduction")
                if dm["distiller_calls"] > 0:
                    _cprint(f"    Distiller:      {dm['distiller_calls']} pages, avg {dm['avg_distiller_output_chars']} chars output")
                if dm["distiller_over_10k"] > 0:
                    _cprint(f"    ⚠ Over 10K:    {dm['distiller_over_10k']} pages (large output)")
                if dm["distiller_failures"] > 0:
                    _cprint(f"    ⚠ Failures:    {dm['distiller_failures']} (raw content passed through)")
                if dm["distiller_skipped"] > 0:
                    _cprint(f"    Skipped:        {dm['distiller_skipped']} (content too short)")
                _cprint("")
        except Exception:
            pass

        # -- Annotation pointing at the live status bar below --
        _cprint(f"  \033[2m{annotation_line}\033[0m")

    def process_command(self, cmd: str) -> bool:
        """Add /sys, /dashboard, and /? commands."""
        stripped = cmd.strip().lower()
        if stripped in ("/sys", "/system-status", "/dashboard"):
            self._bg_refresh_status()
            self._status_cache_time = 0
            self._system_status_visible = True
            print("  System status refreshed. F3 to toggle, F4 to refresh.")
            return True
        if stripped == "/?":
            self._bg_refresh_status()
            self._status_cache_time = 0
            self._show_status_help()
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
