#!/usr/bin/env python3
"""
Skill Metabolism — synaptic-style strength decay for skill auto-pruning.

Every skill has a strength score:
  - New skills start at 1.0
  - All skills decay each session: strength *= DECAY_RATE
  - skill_view() boosts: strength += VIEW_BOOST (reading)
  - skill_manage(patch/edit) boosts: strength += DO_BOOST (doing)
  - When the enabled pool is full and a new skill arrives, the weakest
    non-protected skills are evicted to make room.

Strength data lives in ~/.hermes/skills/.metabolism.json

Boost tiers (doing > reading):
  - VIEW_BOOST  = 0.5  — looked at it
  - DO_BOOST    = 2.0  — patched/edited from experience (mastery)

Properties:
  - A skill read every session stabilises around ~10.0
  - A skill actively maintained stabilises around ~40.0
  - A skill used once fades to ~0.1 after 45 sessions
  - No threshold to tune — decay is the tuning
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
METABOLISM_FILE = HERMES_HOME / "skills" / ".metabolism.json"

DECAY_RATE = 0.95
VIEW_BOOST = 0.5    # reading — looked at the skill
DO_BOOST = 2.0      # doing — patched/edited from experience (mastery)
DEFAULT_STRENGTH = 1.0
POOL_CAP = 45  # max enabled skills in system prompt

# Skills that are never evicted regardless of strength
PROTECTED_SKILLS = frozenset({
    # Core ops
    "system-health", "memory-dashboard", "promote-fact", "query-facts",
    # Vault
    "vault-orient", "vault-capture", "vault-reflect", "vault-session-close",
    # DevOps
    "penny-canary", "hermes-update-with-patches", "safe-update",
    # Dev workflow essentials
    "plan", "systematic-debugging", "code-review",
})


def _load_strengths() -> dict[str, float]:
    """Load strength scores from disk."""
    if not METABOLISM_FILE.exists():
        return {}
    try:
        with open(METABOLISM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: float(v) for k, v in data.items()}
    except Exception as e:
        logger.debug("Could not load metabolism data: %s", e)
        return {}


def _save_strengths(strengths: dict[str, float]) -> None:
    """Persist strength scores to disk."""
    try:
        METABOLISM_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(METABOLISM_FILE, "w", encoding="utf-8") as f:
            json.dump(strengths, f, indent=2, sort_keys=True)
    except Exception as e:
        logger.debug("Could not save metabolism data: %s", e)


def get_strengths() -> dict[str, float]:
    """Get current strength scores for all tracked skills."""
    return _load_strengths()


def decay_all() -> dict[str, float]:
    """Apply one round of decay to all tracked skills. Call once per session."""
    strengths = _load_strengths()
    for skill in strengths:
        strengths[skill] *= DECAY_RATE
    _save_strengths(strengths)
    return strengths


def boost(skill_name: str, amount: float = None) -> float:
    """Boost a skill's strength. Returns new strength.

    Args:
        skill_name: Name of the skill.
        amount: Override boost amount. Defaults to VIEW_BOOST.
            Use DO_BOOST for patch/edit actions (mastery).
    """
    if amount is None:
        amount = VIEW_BOOST
    strengths = _load_strengths()
    current = strengths.get(skill_name, DEFAULT_STRENGTH)
    strengths[skill_name] = current + amount
    _save_strengths(strengths)
    return strengths[skill_name]


def ensure_tracked(skill_names: set[str]) -> None:
    """Ensure all given skill names have a strength entry. New ones get DEFAULT_STRENGTH."""
    strengths = _load_strengths()
    changed = False
    for name in skill_names:
        if name not in strengths:
            strengths[name] = DEFAULT_STRENGTH
            changed = True
    if changed:
        _save_strengths(strengths)


def get_eviction_candidates(enabled_skills: set[str], n: int) -> list[str]:
    """Return the N weakest non-protected enabled skills, sorted weakest first."""
    strengths = _load_strengths()
    candidates = []
    for skill in enabled_skills:
        if skill in PROTECTED_SKILLS:
            continue
        candidates.append((skill, strengths.get(skill, DEFAULT_STRENGTH)))

    # Sort by strength ascending, then by name for deterministic tiebreaker
    candidates.sort(key=lambda x: (x[1], x[0]))
    return [name for name, _ in candidates[:n]]


def admit_new_skills(
    new_skill_names: set[str],
    all_enabled: set[str],
    all_disabled: set[str],
) -> tuple[set[str], set[str]]:
    """
    Admit new skills into the enabled pool, evicting weakest if needed.

    Args:
        new_skill_names: Skills that just arrived (created/installed)
        all_enabled: Currently enabled skill names
        all_disabled: Currently disabled skill names

    Returns:
        (to_enable, to_disable) — sets of skill names to change
    """
    # Only consider genuinely novel skills (not already known)
    novel = new_skill_names - all_enabled - all_disabled

    if not novel:
        return set(), set()

    # How much room do we have?
    room = POOL_CAP - len(all_enabled)
    to_enable = set()
    to_disable = set()

    if len(novel) <= room:
        # Plenty of room — just add them all
        to_enable = novel
    else:
        # Need to evict. Figure out shortfall.
        shortfall = len(novel) - room
        evict = get_eviction_candidates(all_enabled, shortfall)

        if len(evict) < shortfall:
            # Can't evict enough — admit what we can
            can_admit = room + len(evict)
            # Admit the novel skills (arbitrary order is fine)
            to_enable = set(list(novel)[:can_admit])
            to_disable = set(evict)
        else:
            to_enable = novel
            to_disable = set(evict)

    # Initialise strength for new arrivals
    strengths = _load_strengths()
    for name in to_enable:
        strengths[name] = DEFAULT_STRENGTH
    # Zero out evicted (so they start fresh if re-enabled later)
    for name in to_disable:
        strengths.pop(name, None)
    _save_strengths(strengths)

    return to_enable, to_disable
