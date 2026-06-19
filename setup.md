# nanobot Deployment Setup

Server-Dokumentation basierend auf Konfiguration mit Docker, OpenRouter, Slack (Socket Mode), GitHub CLI, WebUI (lokal).

## Verzeichnisstruktur

```
Server (dein-ssh-user: thomas)
├── /opt/nanobot/              # nanobot Repo (git clone)
│   ├── Dockerfile             # angepasst mit gh CLI
│   ├── docker-compose.yml     # angepasst: Ports 127.0.0.1 + environment
│   └── setup.md               # diese Datei
└── /home/thomas/.nanobot/
    ├── config.json             # nanobot Konfiguration
    └── (workspace, sessions, memory – werden automatisch angelegt)
```

## 1. Voraussetzungen

- Docker installiert auf dem Server
- Slack App mit Bot Token (`xoxb-...`) und App-Level Token (`xapp-...`) für Socket Mode
- OpenRouter API Key (`sk-or-v1-...`)
- GitHub Personal Access Token (`ghp_...`) mit Repo-Zugriff (lesen + schreiben)
- WebUI-Passwort (frei wählbar)

## 2. `Dockerfile` – gh CLI hinzufügen

**Datei:** `/opt/nanobot/Dockerfile`

Ergänze zwei Zeilen für GitHub CLI Key + Repo und füge `gh` zur Installation hinzu:

```dockerfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git bubblewrap openssh-client && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs gh && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*
```

## 3. `docker-compose.yml`

**Datei:** `/opt/nanobot/docker-compose.yml`

Änderungen gegenüber dem Original:
- Ports auf `127.0.0.1` gebunden (kein externer Zugriff)
- `nanobot-api` Service entfernt (nicht benötigt)
- `GITHUB_TOKEN` + `GH_TOKEN` als `environment` hinzugefügt
- `env_file` weggelassen – Secrets stehen direkt in `config.json` und `environment`

```yaml
x-common-config: &common-config
  build:
    context: .
    dockerfile: Dockerfile
  volumes:
    - ~/.nanobot:/home/nanobot/.nanobot
  environment:
    - GITHUB_TOKEN=ghp_...
    - GH_TOKEN=ghp_...
  cap_drop:
    - ALL
  cap_add:
    - SYS_ADMIN
  security_opt:
    - apparmor=unconfined
    - seccomp=unconfined

services:
  nanobot-gateway:
    container_name: nanobot-gateway
    <<: *common-config
    command: ["gateway"]
    restart: unless-stopped
    ports:
      - "127.0.0.1:18790:18790"
      - "127.0.0.1:8765:8765"
    deploy:
      resources:
        limits:
          cpus: "1"
          memory: 1G
```

## 4. `config.json`

**Datei:** `/home/thomas/.nanobot/config.json`

```json
{
  "agents": {
    "defaults": {
      "provider": "openrouter",
      "model": "qwen/qwen3.5-flash-02-23"
    }
  },
  "gateway": {
    "host": "127.0.0.1"
  },
  "channels": {
    "websocket": {
      "enabled": true,
      "host": "127.0.0.1",
      "port": 8765,
      "tokenIssueSecret": "dein-webui-passwort"
    },
    "slack": {
      "enabled": true,
      "mode": "socket",
      "botToken": "xoxb-...",
      "appToken": "xapp-...",
      "replyInThread": true
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-..."
    }
  },
  "tools": {
    "exec": {
      "sandbox": "bwrap",
      "allowedEnvKeys": ["GH_TOKEN", "GITHUB_TOKEN"]
    },
    "web": {
      "enabled": true
    }
  }
}
```

Die Config akzeptiert sowohl `camelCase` als auch `snake_case` (z. B. `apiKey` oder `api_key`, `botToken` oder `bot_token`).

Keine `${...}`-Referenzen – alle Secrets direkt in der Datei. Das ist auf einem Einzel-Server vertretbar mit `chmod 600 ~/.nanobot/config.json`.

## 4a. bwrap-Sandbox: Env-Vars durchlassen

**Problem:** bwrap isoliert die Umgebung komplett – Umgebungsvariablen aus dem Container (wie `GH_TOKEN`) sind in der Sandbox nicht sichtbar.

**Fix in `nanobot/agent/tools/sandbox.py`:**

```diff
+import os
 import shlex
 from pathlib import Path

-def _bwrap(command: str, workspace: str, cwd: str) -> str:
+def _bwrap(command: str, workspace: str, cwd: str,
+           allowed_env_keys: list[str] | None = None) -> str:
     args = ["bwrap", "--new-session", "--die-with-parent", "--setenv", "HOME", str(ws)]
+    for key in (allowed_env_keys or []):
+        val = os.environ.get(key)
+        if val is not None:
+            args += ["--setenv", key, val]
     ...
```

**Fix in `nanobot/agent/tools/shell.py`:**

```diff
-command = wrap_command(self.sandbox, command, workspace, cwd)
+command = wrap_command(self.sandbox, command, workspace, cwd, self.allowed_env_keys)
```

**Config-Eintrag (siehe Abschnitt 4):** `"allowedEnvKeys": ["GH_TOKEN", "GITHUB_TOKEN"]` unter `tools.exec`.

Danach Container neubauen: `docker compose build nanobot-gateway`.

## 5. Setup-Befehle (einmalig)

```bash
# Verzeichnis anlegen
mkdir -p ~/.nanobot
sudo chown 1000:1000 ~/.nanobot

# nanobot clonen (falls noch nicht geschehen)
git clone https://github.com/HKUDS/nanobot.git /opt/nanobot
cd /opt/nanobot

# Dockerfile anpassen (gh CLI – siehe Abschnitt 2)
vim Dockerfile

# config.json anlegen
vim ~/.nanobot/config.json

# docker-compose.yml anpassen (Ports + GITHUB_TOKEN)
vim docker-compose.yml

# Bauen und starten
docker compose build nanobot-gateway
docker compose up -d nanobot-gateway

# Logs prüfen
docker compose logs -f nanobot-gateway
```

## 6. Slack testen

Im Slack-Channel `@nanobot Hello!` senden. Der Bot antwortet im Thread.

## 7. GitHub-Zugriff mit gh CLI

```bash
# gh im Container testen
docker compose exec nanobot-gateway gh --version

# Repo clonen (GH_TOKEN wird automatisch von gh erkannt)
docker compose exec nanobot-gateway gh repo clone dein-org/dein-repo /home/nanobot/workspace/
```

Der Container hat `GH_TOKEN` und `GITHUB_TOKEN` als Umgebungsvariablen – `gh` und Git mit HTTPS lesen diese automatisch.

**Wichtig bei bwrap-Sandbox:** Zusätzlich muss in der `config.json` unter `tools.exec` der Eintrag `"allowedEnvKeys": ["GH_TOKEN", "GITHUB_TOKEN"]` gesetzt sein (siehe Abschnitt 4a). Sonst sieht nanobot die Variablen in der Sandbox nicht.

## 8. WebUI per SSH-Tunnel

```bash
# Vom lokalen Rechner:
ssh -L 8765:localhost:8765 dein-server

# Browser öffnen:
open http://localhost:8765
# oder: http://localhost:8765
```

Login mit dem `tokenIssueSecret` aus der `config.json`.

## 9. Container-Neustart nach Änderungen

```bash
# Nach config-Änderungen: nur restart
docker compose restart nanobot-gateway

# Nach Dockerfile-Änderungen: rebuild + restart
docker compose build nanobot-gateway
docker compose up -d nanobot-gateway

# Komplett neu
docker compose down
docker compose up -d nanobot-gateway
```

## 10. Sicherheits-Übersicht

| Aspekt | Status |
|---|---|
| Keine Ports nach außen | ✅ `127.0.0.1`-Binding in `docker-compose.yml` |
| Slack (nur ausgehend) | ✅ Socket Mode – kein Webhook-Endpunkt |
| LLM-API (ausgehend) | ✅ HTTPS zu OpenRouter |
| GitHub (lesen + schreiben) | ✅ gh CLI + GH_TOKEN per `allowedEnvKeys` in bwrap-Sandbox |
| WebUI lokal per SSH-Tunnel | ✅ Port 8765 nur auf `127.0.0.1` |
| Container-Härtung | ✅ `cap_drop: ALL`, bwrap-Sandbox |
| Secrets in Config | ⚠️ unverschlüsselt – Dateirechte per `chmod 600` schützen |
| Env-Vars in bwrap-Sandbox | ✅ `allowedEnvKeys` steuert, welche Variablen durchgelassen werden |

## 11. Nützliche Befehle

```bash
# Logs live verfolgen
docker compose logs -f nanobot-gateway

# Container-Shell
docker compose exec nanobot-gateway sh

# nanobot Status im Container
docker compose exec nanobot-gateway nanobot status

# Agent-Kommando (einmalig)
docker compose exec nanobot-gateway nanobot agent -m "Sag etwas"
```