"""
Token OS Circuit Breaker — routes LLM traffic through Token OS with fallback.

When Token OS is healthy:
  - Anthropic calls → localhost:8650/v1/messages (Token OS proxies to Anthropic)
  - OpenAI calls → localhost:8650/v1/chat/completions (Token OS proxies to provider)
  - X-Priority and X-Agent-ID headers added to every request

When Token OS is down:
  - Calls go directly to provider (current behavior, no Token OS)
  - Health checked every 30 seconds to restore routing

Thread-safe. Never blocks. Never crashes the agent.
"""

import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

TOKEN_OS_URL = os.environ.get("TOKEN_OS_URL", "http://127.0.0.1:8650")
_HEALTH_ENDPOINT = f"{TOKEN_OS_URL}/health"
_TOKEN_OS_BASE = f"{TOKEN_OS_URL}/v1"

# Circuit breaker state
_lock = threading.Lock()
_healthy = True  # assume healthy on start
_last_check = 0.0
_CHECK_INTERVAL = 30.0  # seconds between health checks when unhealthy
_HEALTH_TIMEOUT = 2.0  # seconds for health check request


def _check_health() -> bool:
    """Synchronous health check. Returns True if Token OS is responding."""
    try:
        req = urllib.request.Request(_HEALTH_ENDPOINT, method="GET")
        resp = urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT)
        return resp.status == 200
    except Exception:
        return False


def is_healthy() -> bool:
    """Check if Token OS is available. Caches result."""
    global _healthy, _last_check
    should_check = False
    with _lock:
        if _healthy:
            return True
        now = time.time()
        if now - _last_check >= _CHECK_INTERVAL:
            _last_check = now
            should_check = True
        else:
            return False

    if should_check:
        # Health check done OUTSIDE the lock to avoid blocking other threads
        h = _check_health()
        if h:
            with _lock:
                _healthy = True
            logger.info("Token OS circuit breaker: CLOSED (restored)")
        return h
    return False


def mark_unhealthy():
    """Mark Token OS as unreachable. Triggers fallback to direct provider."""
    global _healthy, _last_check
    with _lock:
        if _healthy:
            _healthy = False
            _last_check = time.time()
            logger.warning("Token OS circuit breaker: OPEN (falling back to direct)")


def get_priority(platform: str = "", is_cron: bool = False) -> str:
    """Determine request priority from context."""
    if is_cron:
        return "P3"
    if platform in ("telegram", "cli", "discord", "whatsapp", "signal", "slack"):
        return "P0"
    if platform == "cron":
        return "P3"
    return "P2"


def get_token_os_base_url() -> str:
    """Get Token OS base URL if healthy, empty string if not."""
    if is_healthy():
        return _TOKEN_OS_BASE
    return ""


def get_extra_headers(priority: str = "P2", agent_id: str = "") -> dict:
    """Get Token OS routing headers to add to requests."""
    return {
        "X-Priority": priority,
        "X-Agent-ID": agent_id or os.environ.get("HERMES_AGENT_ID", "unknown"),
    }


# ── Integration helpers ────────────────────────────────────────────────────

def wrap_anthropic_base_url(original_base_url: str = None) -> str:
    """Return Token OS URL for Anthropic calls, or original if circuit open.

    Token OS has /v1/messages endpoint that proxies to Anthropic.
    The Anthropic SDK sends to {base_url}/v1/messages, so we set
    base_url to TOKEN_OS_URL (without /v1 suffix — SDK adds it).
    """
    token_os = get_token_os_base_url()
    if token_os:
        # Anthropic SDK appends /v1/messages itself, so give it the root
        return TOKEN_OS_URL
    return original_base_url


def wrap_openai_base_url(original_base_url: str = None) -> str:
    """Return Token OS URL for OpenAI-compatible calls, or original if circuit open.

    OpenAI SDK sends to {base_url}/chat/completions, so we set
    base_url to TOKEN_OS_URL/v1 (SDK appends /chat/completions).
    """
    token_os = get_token_os_base_url()
    if token_os:
        return token_os
    return original_base_url
