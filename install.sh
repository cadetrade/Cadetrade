#!/usr/bin/env bash
# Installa CadeTrade su un server Ubuntu 24.04 e lo fa girare h24 con Docker.
# Uso (sul server, da root):  bash install.sh <url-del-repo-git>
set -euo pipefail
REPO="${1:?Uso: bash install.sh <url-del-repo-git>}"
DIR=/opt/cadetrade

echo "== 1/5 Docker e fuso orario"
timedatectl set-timezone Europe/Rome || true
if ! command -v docker >/dev/null; then
  apt-get update -y && apt-get install -y ca-certificates curl git
  curl -fsSL https://get.docker.com | sh
fi

echo "== 2/5 Codice"
mkdir -p ~/.ssh && ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone "$REPO" "$DIR"; fi
mkdir -p "$DIR/state" "$DIR/logs"

echo "== 3/5 Build e test"
docker build -t cadetrade "$DIR"
docker run --rm cadetrade python -m pytest -q

echo "== 4/5 Chiavi (non vengono mostrate a schermo)"
if [ ! -f "$DIR/secrets.toml" ]; then
  ask() { local v; read -r -s -p "$1: " v; echo >&2; printf '%s' "$v"; }
  KEY=$(ask "Alpaca Key ID (inizia con PK)")
  SEC=$(ask "Alpaca Secret Key")
  TGT=$(ask "Token del bot Telegram")
  read -r -p "Chat id Telegram (invio = lo trovo io, dopo aver scritto 'ciao' al bot): " TGC
  if [ -z "$TGC" ]; then
    TGC=$(curl -fsS "https://api.telegram.org/bot${TGT}/getUpdates" | grep -o '"chat":{"id":-\?[0-9]*' | head -1 | grep -o -- '-\?[0-9]*$' || true)
    [ -n "$TGC" ] && echo "Chat id trovato: $TGC" || { echo "Chat id non trovato: scrivi 'ciao' al bot e rilancia lo script"; exit 1; }
  fi
  echo "Ora generiamo il token dell'abbonamento Claude: apri il link che compare, accedi e incolla il codice."
  docker run -it --rm cadetrade claude setup-token || true
  CCT=$(ask "Incolla il token che inizia con sk-ant-oat (invio se usi la API)")
  ANT=$(ask "Chiave API Anthropic (invio se usi il token Claude Code)")
  umask 077
  cat > "$DIR/secrets.toml" <<TOML
ALPACA_KEY_ID = "$KEY"
ALPACA_SECRET_KEY = "$SEC"
TELEGRAM_BOT_TOKEN = "$TGT"
TELEGRAM_CHAT_ID = "$TGC"
CLAUDE_CODE_OAUTH_TOKEN = "$CCT"
ANTHROPIC_API_KEY = "$ANT"
TOML
fi
BRAIN=claude-code
grep -q 'CLAUDE_CODE_OAUTH_TOKEN = ""' "$DIR/secrets.toml" && BRAIN=api

echo "== 5/5 Avvio h24 (si riavvia da solo dopo crash o reboot)"
docker rm -f cadetrade 2>/dev/null || true
docker run -d --name cadetrade --restart unless-stopped \
  -v "$DIR/state:/app/state" -v "$DIR/logs:/app/logs" -v "$DIR/secrets.toml:/app/secrets.toml:ro" \
  cadetrade python -m cadetrade schedule --brain "$BRAIN"
sleep 5
docker exec cadetrade python -m cadetrade status
echo
echo "Fatto. Log in diretta:  docker logs -f cadetrade"
echo "Prova una sveglia ora:   docker exec cadetrade python -m cadetrade once --slot 1540 --force --brain $BRAIN"
