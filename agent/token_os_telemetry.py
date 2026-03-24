"""
Token OS telemetry — fire-and-forget call reporting.

After each LLM call, Hermes POSTs usage data to Token OS at localhost:8650.
This is Stage 1 (passive observation). Token OS tracks quota, computes zones,
logs history, and sends notifications — without being in the request path.

If Token OS is down, the POST silently fails. Zero impact on Hermes.
"""

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_OS_URL = os.getenv("TOKEN_OS_URL", "http://127.0.0.1:8650")
_TRACK_ENDPOINT = f"{TOKEN_OS_URL}/v1/track"
_REGISTER_ENDPOINT = f"{TOKEN_OS_URL}/register"
_TIMEOUT = 2  # seconds — fail fast, never block the agent


def _post_async(url: str, payload: dict) -> None:
    """POST JSON to Token OS in a background thread. Fire and forget."""
    def _do_post():
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=_TIMEOUT)
        except (urllib.error.URLError, OSError, Exception):
            pass  # Token OS down or unreachable — silently ignore

    t = threading.Thread(target=_do_post, daemon=True)
    t.start()


def track_call(
    provider: str,
    model: str,
    total_tokens: int,
    priority: str = "P2",
    agent_id: str = "unknown",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    platform: str = "",
) -> None:
    """Report an LLM call to Token OS. Non-blocking, fire-and-forget.

    Call this after every successful LLM API response.
    """
    payload = {
        "provider": provider or "unknown",
        "model": model or "unknown",
        "tokens_used": total_tokens,
        "priority": priority,
        "agent_id": agent_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "platform": platform,
    }
    _post_async(_TRACK_ENDPOINT, payload)


def register_agent(
    agent_id: str,
    agent_type: str = "gateway",
    default_priority: str = "P0",
) -> None:
    """Register this agent with Token OS. Non-blocking."""
    payload = {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "default_priority": default_priority,
    }
    _post_async(_REGISTER_ENDPOINT, payload)
