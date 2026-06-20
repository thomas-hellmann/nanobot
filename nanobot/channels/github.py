"""GitHub channel — receives webhooks, posts comments to issues/PRs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import jwt

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config_base import Base


class GithubConfig(Base):
    """GitHub webhook channel configuration."""

    enabled: bool = False
    webhook_secret: str = ""
    github_token: str = ""
    app_id: str = ""
    private_key: str = ""
    installation_id: str = ""
    host: str = "0.0.0.0"
    port: int = 8080


_STATUS_TEXTS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    500: "Internal Server Error",
}


class GithubChannel(BaseChannel):
    """GitHub channel using webhooks."""

    name = "github"
    display_name = "GitHub"
    send_progress = False
    send_tool_hints = False
    show_reasoning = False

    def is_allowed(self, sender_id: str) -> bool:
        return True

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return GithubConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = GithubConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: GithubConfig = config
        self._server: asyncio.AbstractServer | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._installation_token: str | None = None
        self._token_expires_at: float = 0.0

    async def start(self) -> None:
        has_pat = bool(self.config.github_token)
        has_app = bool(self.config.app_id and self.config.private_key and self.config.installation_id)
        if not has_pat and not has_app:
            self.logger.warning("GitHub channel: no token or app credentials configured, skipping")
            return
        self._http_client = httpx.AsyncClient(
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "nanobot",
            },
            timeout=30,
        )
        self._server = await asyncio.start_server(
            self._handle_connection, self.config.host, self.config.port,
        )
        self.logger.info(
            "GitHub webhook server listening on http://{}:{}",
            self.config.host, self.config.port,
        )
        asyncio.create_task(self._serve_forever())

    async def _serve_forever(self) -> None:
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._http_client:
            await self._http_client.aclose()

    async def _ensure_token(self) -> str | None:
        if self.config.github_token:
            return self.config.github_token
        if self._installation_token and time.time() < self._token_expires_at - 60:
            return self._installation_token
        return await self._refresh_installation_token()

    async def _refresh_installation_token(self) -> str | None:
        if not self._http_client:
            return None
        app_id = self.config.app_id
        private_key = self.config.private_key
        install_id = self.config.installation_id
        if not app_id or not private_key or not install_id:
            return None

        now = int(time.time())
        jwt_token = jwt.encode(
            {"iat": now - 60, "exp": now + 600, "iss": app_id},
            private_key,
            algorithm="RS256",
        )

        try:
            resp = await self._http_client.post(
                f"https://api.github.com/app/installations/{install_id}/access_tokens",
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
            if resp.is_error:
                self.logger.error(
                    "GitHub App token error: {} {}", resp.status_code, resp.text[:300],
                )
                return None
            data = resp.json()
            self._installation_token = data["token"]
            self._token_expires_at = time.time() + data.get("expires_in", 3600)
            return self._installation_token
        except Exception as e:
            self.logger.error("GitHub App token refresh error: {}", e)
            return None

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=30)
        except (asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        try:
            await self._process_webhook(data, writer)
        except Exception:
            self.logger.exception("Error processing webhook")
            await self._send_response(writer, 500, "Internal Server Error")

    async def _process_webhook(self, data: bytes, writer: asyncio.StreamWriter) -> None:
        header_end = data.find(b"\r\n\r\n")
        if header_end == -1:
            await self._send_response(writer, 400, "Bad Request")
            return

        request_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        parts = request_line.split(" ")
        if len(parts) < 2 or parts[0] != "POST":
            await self._send_response(writer, 405, "Method Not Allowed")
            return

        header_section = data[:header_end].decode("utf-8", errors="replace")
        headers = {}
        for line in header_section.split("\r\n")[1:]:
            if ":" in line:
                key, val = line.split(":", 1)
                headers[key.strip().lower()] = val.strip()

        signature = headers.get("x-hub-signature-256", "")
        event_type = headers.get("x-github-event", "")
        body = data[header_end + 4:]

        self.logger.debug(
            "Webhook: event={} sig={} body_len={} secret_set={}",
            event_type, signature[:20] if signature else "(none)",
            len(body), bool(self.config.webhook_secret),
        )

        # Use Content-Length to trim body to exact bytes (avoids trailing
        # data that would break HMAC verification).
        content_length = headers.get("content-length")
        if content_length:
            try:
                body = body[:int(content_length)]
            except (ValueError, IndexError):
                pass

        if self.config.webhook_secret:
            expected = "sha256=" + hmac.new(
                self.config.webhook_secret.encode(), body, hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                self.logger.warning("Invalid HMAC signature")
                await self._send_response(writer, 403, "Forbidden")
                return

        if event_type != "issue_comment":
            await self._send_response(writer, 200, "OK (ignored)")
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            self.logger.error(
                "GitHub JSON error: {} body[:300]={!r}", e, body[:300],
            )
            await self._send_response(writer, 400, f"Invalid JSON: {e}")
            return

        if payload.get("action") != "created":
            await self._send_response(writer, 200, "OK (ignored)")
            return

        comment = payload.get("comment", {}) or {}
        issue = payload.get("issue", {}) or {}
        repository = payload.get("repository", {}) or {}
        sender = payload.get("sender", {}) or {}

        comment_body = (comment.get("body") or "").strip()
        if not comment_body.startswith("/nanobot"):
            await self._send_response(writer, 200, "OK (ignored)")
            return

        repo = repository.get("full_name", "")
        issue_number = issue.get("number")
        sender_login = sender.get("login", "unknown")
        if not repo or not issue_number:
            await self._send_response(writer, 400, "Missing repo/issue info")
            return

        chat_id = f"{repo}#{issue_number}"
        self.logger.info(
            "GitHub: /nanobot from {} on {}#{}", sender_login, repo, issue_number,
        )

        await self._handle_message(
            sender_id=sender_login,
            chat_id=chat_id,
            content=comment_body,
            session_key=f"github:{chat_id}",
        )
        await self._send_response(writer, 200, "OK")

    async def send(self, msg: OutboundMessage) -> None:
        content = msg.content
        if not content or not self._http_client:
            return

        # Skip progress, reasoning, and tool-hint messages – only post final
        # responses to GitHub comments.
        meta = msg.metadata or {}
        if meta.get("_progress") or meta.get("_reasoning_delta") or meta.get("_tool_hint"):
            return

        token = await self._ensure_token()
        if not token:
            self.logger.error("No GitHub auth token available, cannot send")
            return

        chat_id = msg.chat_id
        if "#" not in chat_id:
            self.logger.warning("Invalid GitHub chat_id: {}", chat_id)
            return
        repo, issue_str = chat_id.rsplit("#", 1)
        try:
            issue_number = int(issue_str)
        except ValueError:
            self.logger.warning("Invalid issue number in chat_id: {}", chat_id)
            return

        url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
        body = content[:65000]

        try:
            resp = await self._http_client.post(
                url, json={"body": body},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.is_error:
                self.logger.error(
                    "GitHub send failed: {} {}", resp.status_code, resp.text[:300],
                )
        except Exception as e:
            self.logger.error("GitHub send error: {}", e)

    @staticmethod
    async def _send_response(writer: asyncio.StreamWriter, status: int, body: str) -> None:
        reason = _STATUS_TEXTS.get(status, "Unknown")
        resp = (
            f"HTTP/1.0 {status} {reason}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n{body}"
        )
        writer.write(resp.encode())
        await writer.drain()
        writer.close()
