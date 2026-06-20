This file provides guidance to AI coding agents working with this repository.

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

## Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, OpenAI Responses API, Azure, Bedrock, GitHub Copilot, OpenAI Codex, etc.) built on a common base (`base.py`). Includes image generation (`image_generation.py`) and audio transcription (`transcription.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WeCom, DingTalk, Email, GitHub, MoChat, MS Teams, WebSocket). `manager.py` discovers and coordinates them. Channels are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution (with sandbox backends), web search/fetch, MCP servers, cron, notebook editing, subagent spawning, long-running tasks / sustained goals (`long_task.py`), image generation, and self-modification. Tools are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/`): Per-session history, context compaction, TTL-based auto-compaction (`manager.py`), and sustained goal state tracking (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **Bridge** (`bridge/`): TypeScript services (e.g. WhatsApp bridge) bundled into the wheel via `pyproject.toml` `force-include`.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.
- **API Server** (`nanobot/api/server.py`): OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- **Command Router** (`nanobot/command/`): Slash command routing and built-in command handlers.
- **Heartbeat** (`nanobot/templates/HEARTBEAT.md`): Periodic task list checked via `cron` jobs (legacy dedicated service removed).
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent pairing codes per channel.
- **Skills** (`nanobot/skills/`): Built-in skill definitions (long-goal, cron, github, image-generation, etc.) loaded into agent context.
- **Security** (`nanobot/security/`): PTH file guard and other security measures activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Fork-Specific Changes (thomas-hellmann/nanobot)

### GitHub Channel (`nanobot/channels/github.py`)

A new channel that receives GitHub webhooks and posts comments to issues/PRs.

**Config (`~/.nanobot/config.json`):**
```json
"channels": {
  "github": {
    "enabled": true,
    "webhookSecret": "${GH_WEBHOOK_SECRET}",
    "appId": "${GITHUB_APP_ID}",
    "privateKey": "${GITHUB_APP_PRIVATE_KEY}",
    "installationId": "${GITHUB_INSTALLATION_ID}",
    "port": 8080
  }
}
```

- Listens on `POST /webhook` for `issue_comment` events
- Responds only to comments starting with `/nanobot`
- Uses GitHub App installation token (falls back to `githubToken` PAT)
- Session per issue/PR (`github:owner/repo#123`)

### GitHub App Auth

- `githubToken` (PAT) → comments appear as token owner
- `appId` + `privateKey` + `installationId` → comments appear as `nanobot[bot]`
- App token is refreshed every hour (cached, renewed 60s before expiry)
- Private key in `.env` uses `\n` literal newlines (normalized in code)

### bwrap Sandbox: Env Variable Forwarding

**`nanobot/agent/tools/sandbox.py`** and **`nanobot/agent/tools/shell.py`** modified to pass container env vars into the bwrap sandbox.

**Config:**
```json
"tools": {
  "exec": {
    "sandbox": "bwrap",
    "allowedEnvKeys": ["GH_TOKEN", "GITHUB_TOKEN"]
  }
}
```

- `_bwrap()` now adds `--setenv KEY VALUE` for each key in `allowed_env_keys`
- `_prepare_command()` passes `self.allowed_env_keys` to `wrap_command()`

### Optional Dependency Group

**`pyproject.toml`** has a new `[github]` extra:
```toml
github = [
    "PyJWT>=2.0,<3.0",
    "cryptography>=41.0",
]
```

Install with: `pip install "nanobot-ai[github]"` or in Dockerfile: `uv pip install --system ".[github]"`.

### Docker Compose (nginx-proxy)

```yaml
environment:
  - VIRTUAL_HOST=nanobot.th-dev.eu
  - VIRTUAL_PORT=8080
  - LETSENCRYPT_HOST=nanobot.th-dev.eu
networks:
  - default
  - proxy-net
```

### Secrets Strategy

All secrets in `~/.nanobot/.env` file, referenced in `config.json` via `${VAR_NAME}`:
- `OPENAI_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `GH_TOKEN`, `GH_WEBHOOK_SECRET`, `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`, `NANOBOT_WEBUI_SECRET`
- `.env` is in `.gitignore` and `.dockerignore`

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Contribution Flow

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for contribution flow and PR guidelines.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.
