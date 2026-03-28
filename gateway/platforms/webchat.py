"""
WebChat platform adapter.

Runs a lightweight WebSocket server that Mission Control (or any WebSocket
client) connects to for relaying chat messages through the Hermes agent
pipeline.

Protocol (JSON over WebSocket):
  Client -> Server:
    {"type": "chat", "message": "...", "client_id": "...", "user_name": "..."}
  
  Server -> Client:
    {"type": "response", "message": "...", "client_id": "..."}
    {"type": "typing", "client_id": "..."}
    {"type": "error", "message": "...", "client_id": "..."}

Requires:
- websockets (pip install websockets)
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

try:
    import websockets
    from websockets.server import serve as ws_serve
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def check_webchat_requirements() -> bool:
    """Check if WebChat dependencies are available."""
    return WEBSOCKETS_AVAILABLE


class WebChatAdapter(BasePlatformAdapter):
    """
    WebSocket-based chat adapter for Mission Control integration.

    Starts a WebSocket server on a configurable port (default 8765).
    Mission Control connects as a client and relays browser chat messages.
    Responses flow back through the same WebSocket connection.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBCHAT)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("WEBCHAT_HOST", DEFAULT_HOST))
        self._port: int = int(extra.get("port", os.getenv("WEBCHAT_PORT", str(DEFAULT_PORT))))
        self._server = None
        # client_id -> websocket connection
        self._connections: Dict[str, Any] = {}
        # reverse: websocket -> client_id
        self._ws_to_client: Dict[Any, str] = {}
        # Track which MC relay connection owns which browser client_ids
        # ws_connection -> set of browser client_ids
        self._relay_clients: Dict[Any, set] = {}

    async def connect(self) -> bool:
        """Start the WebSocket server."""
        if not WEBSOCKETS_AVAILABLE:
            logger.error("WebChat: 'websockets' package not installed. Run: pip install websockets")
            self._set_fatal_error("missing_dep", "websockets package not installed", retryable=False)
            return False

        try:
            self._server = await ws_serve(
                self._handle_ws_connection,
                self._host,
                self._port,
                ping_interval=30,
                ping_timeout=10,
            )
            self._mark_connected()
            logger.info("WebChat adapter listening on ws://%s:%d", self._host, self._port)
            return True
        except OSError as e:
            logger.error("WebChat: Failed to bind to %s:%d — %s", self._host, self._port, e)
            self._set_fatal_error("bind_failed", f"Cannot bind to port {self._port}: {e}", retryable=True)
            return False

    async def disconnect(self) -> None:
        """Stop the WebSocket server and close all connections."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Close all active connections
        for ws in list(self._ws_to_client.keys()):
            try:
                await ws.close()
            except Exception:
                pass

        self._connections.clear()
        self._ws_to_client.clear()
        self._relay_clients.clear()
        self._mark_disconnected()
        logger.info("WebChat adapter stopped")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a response message back through WebSocket to the client."""
        ws = self._connections.get(chat_id)
        if not ws:
            # chat_id might be a browser client_id relayed through MC.
            # Search through relay clients.
            for relay_ws, client_ids in self._relay_clients.items():
                if chat_id in client_ids:
                    ws = relay_ws
                    break

        if not ws:
            return SendResult(success=False, error=f"No connection for client {chat_id}")

        message_id = str(uuid.uuid4())[:8]
        payload = {
            "type": "response",
            "message": content,
            "client_id": chat_id,
            "message_id": message_id,
        }

        try:
            await ws.send(json.dumps(payload))
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("WebChat: Failed to send to %s: %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send a typing indicator to the client."""
        ws = self._connections.get(chat_id)
        if not ws:
            for relay_ws, client_ids in self._relay_clients.items():
                if chat_id in client_ids:
                    ws = relay_ws
                    break

        if ws:
            try:
                await ws.send(json.dumps({
                    "type": "typing",
                    "client_id": chat_id,
                }))
            except Exception:
                pass

    async def _handle_ws_connection(self, ws) -> None:
        """Handle a new WebSocket connection (from Mission Control or direct client)."""
        conn_id = str(uuid.uuid4())[:8]
        self._connections[conn_id] = ws
        self._ws_to_client[ws] = conn_id
        self._relay_clients[ws] = set()

        logger.info("WebChat: New connection %s from %s", conn_id, ws.remote_address)

        # Send welcome
        try:
            await ws.send(json.dumps({
                "type": "connected",
                "connection_id": conn_id,
                "message": "WebChat adapter connected",
            }))
        except Exception:
            return

        try:
            async for raw_message in ws:
                await self._process_message(ws, conn_id, raw_message)
        except Exception as e:
            logger.debug("WebChat: Connection %s closed: %s", conn_id, e)
        finally:
            # Clean up
            self._connections.pop(conn_id, None)
            self._ws_to_client.pop(ws, None)
            # Clean up relay client mappings
            relay_client_ids = self._relay_clients.pop(ws, set())
            for cid in relay_client_ids:
                self._connections.pop(cid, None)
            logger.info("WebChat: Connection %s disconnected", conn_id)

    async def _process_message(self, ws, conn_id: str, raw: str) -> None:
        """Process an incoming WebSocket message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(json.dumps({
                "type": "error",
                "message": "Invalid JSON",
            }))
            return

        msg_type = data.get("type", "")

        if msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            return

        if msg_type == "chat":
            # A chat message to relay through the agent.
            # client_id: identifies the browser user (may differ from conn_id
            # if MC is relaying on behalf of a browser client)
            client_id = data.get("client_id", conn_id)
            user_name = data.get("user_name", "webchat_user")
            message_text = data.get("message", "").strip()

            if not message_text:
                await ws.send(json.dumps({
                    "type": "error",
                    "message": "Empty message",
                    "client_id": client_id,
                }))
                return

            # Register client_id -> this ws connection for response routing
            self._connections[client_id] = ws
            self._relay_clients.setdefault(ws, set()).add(client_id)

            # Build MessageEvent and dispatch to the gateway handler
            source = SessionSource(
                platform=Platform.WEBCHAT,
                chat_id=client_id,
                chat_type="dm",
                user_id=data.get("user_id", client_id),
                user_name=user_name,
            )

            event = MessageEvent(
                text=message_text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=data.get("message_id", str(uuid.uuid4())[:8]),
            )

            # handle_message spawns a background task and returns immediately
            await self.handle_message(event)

        elif msg_type == "command":
            # Slash commands (e.g., /new, /reset)
            client_id = data.get("client_id", conn_id)
            command = data.get("command", "").strip()

            if command:
                self._connections[client_id] = ws
                self._relay_clients.setdefault(ws, set()).add(client_id)

                source = SessionSource(
                    platform=Platform.WEBCHAT,
                    chat_id=client_id,
                    chat_type="dm",
                    user_id=data.get("user_id", client_id),
                    user_name=data.get("user_name", "webchat_user"),
                )

                event = MessageEvent(
                    text=f"/{command}" if not command.startswith("/") else command,
                    message_type=MessageType.COMMAND,
                    source=source,
                )

                await self.handle_message(event)

        else:
            await ws.send(json.dumps({
                "type": "error",
                "message": f"Unknown message type: {msg_type}",
            }))
