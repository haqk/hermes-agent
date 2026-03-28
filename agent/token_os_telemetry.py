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

TOKEN_OS_URL = os.environ.get("TOKEN_OS_URL", "http://127.0.0.1:8650")
_TRACK_ENDPOINT = f"{TOKEN_OS_URL}/v1/track"
_TRACK_HEADERS_ENDPOINT = f"{TOKEN_OS_URL}/v1/track-headers"
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


def _post_response_headers(response) -> None:
    """httpx event_hook callback — capture rate limit headers from LLM responses.

    Called by the OpenAI SDK's underlying httpx client on every HTTP response.
    Extracts rate limit headers and POSTs them to Token OS /v1/track-headers.
    Fire-and-forget, never blocks.

    This is the belt-and-suspenders path (v3): even if Token OS is bypassed
    via circuit breaker, headers still flow through this hook.
    """
    try:
        # Extract rate limit headers
        rl_headers = {}
        for key, value in response.headers.items():
            key_lower = key.lower()
            if 'ratelimit' in key_lower or 'x-ratelimit' in key_lower:
                rl_headers[key] = value

        if not rl_headers:
            return  # No rate limit headers in this response

        # Determine provider from the request URL or response headers
        url = str(response.request.url) if hasattr(response, 'request') else ""
        provider = "unknown"
        # Check Token OS response header first (when proxied)
        token_provider = response.headers.get("x-token-provider", "")
        if token_provider:
            provider = token_provider
        elif "anthropic.com" in url:
            provider = "anthropic"
        elif "openrouter.ai" in url:
            provider = "openrouter"
        elif "googleapis.com" in url or "generativelanguage" in url:
            provider = "gemini"
        elif "cerebras.ai" in url:
            provider = "cerebras"
        elif "groq.com" in url:
            provider = "groq"
        elif "127.0.0.1:8650" in url or "localhost:8650" in url:
            # Proxied through Token OS — detect from header names
            if any("anthropic" in k.lower() for k in rl_headers):
                provider = "anthropic"

        payload = {
            "provider": provider,
            "headers": rl_headers,
            "agent_id": os.environ.get("HERMES_AGENT_ID", "unknown"),
        }
        _post_async(_TRACK_HEADERS_ENDPOINT, payload)
    except Exception:
        pass  # never, ever block the LLM response path
