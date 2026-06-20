"""GitHub channel — receives webhooks, posts comments to issues/PRs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import httpx

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config_base import Base


class GithubConfig(Base):
    """GitHub webhook channel configuration."""

    enabled: bool = False
    webhook_secret: str = ""
    github_token: str = ""
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

    async def start(self) -> None:
        if not self.config.github_token:
            self.logger.warning("GitHub token not configured, channel disabled")
            return
        self._http_client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.config.github_token}",
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
        except json.JSONDecodeError:
            await self._send_response(writer, 400, "Invalid JSON")
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
            session_key_override=f"github:{chat_id}",
        )
        await self._send_response(writer, 200, "OK")

    async def send(self, msg: OutboundMessage) -> None:
        content = msg.content
        if not content or not self._http_client:
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
            resp = await self._http_client.post(url, json={"body": body})
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
