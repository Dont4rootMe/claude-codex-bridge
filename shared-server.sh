#!/bin/bash
# Общий app-server для приложения Codex и codex-bridge.
# Слушает unix-сокет в ~/.codex-bridge (каталог 700): доступен только текущему пользователю,
# без TCP-порта. Приложение Codex подключается к нему, если запущено с
#   CODEX_APP_SERVER_WS_URL=ws+unix://localhost$HOME/.codex-bridge/app-server.sock:/rpc
# Переменные окружения и флаги повторяют то, с чем приложение запускает свой сервер.
set -euo pipefail
DIR="$HOME/.codex-bridge"
SOCK="$DIR/app-server.sock"
BIN=/Applications/Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex
mkdir -p "$DIR"
chmod 700 "$DIR"
rm -f "$SOCK"
# Инструменты приложения (codex_app: создать/форкнуть/передать тред, автоматизации) ходят
# в служебный канал окна Codex. Окно создаёт его заново при каждом запуске в
# /tmp/codex-app-tools/<id>.sock, поэтому серверу даётся постоянный путь-ссылка,
# которую обновляет codex-app-tools-link (вызывается из codex-shared-launch).
"$(dirname "$(readlink -f "$0")")/codex-app-tools-link" >/dev/null 2>&1 || true
export CODEX_APP_TOOLS_PIPE_PATH="$DIR/app-tools.sock"
# launchd даёт процессам мягкий лимит в 256 открытых файлов, а серверу с десятками тредов и
# MCP-серверов нужно намного больше (иначе «Too many open files» при запуске MCP).
ulimit -n 65536 2>/dev/null || ulimit -n 10240 2>/dev/null || true
# Время старта: codex-shared-helper перезапустит сервер, если Codex обновился (бинарник новее).
date +%s > "$DIR/server.started"
export LOG_FORMAT=json RUST_LOG=warn CODEX_INTERNAL_ORIGINATOR_OVERRIDE=Codex CODEX_SHELL=1 \
  CODEX_SAGE_BACKFILL_TRACKER_TAB_REUSE=1 \
  CODEX_TECTONIC_PATH=/Applications/Codex.app/Contents/Resources/tectonic/tectonic \
  CODEX_APP_RUNTIME_CLI_PATH="$BIN" \
  CODEX_MCP_NODE_PATH=/Applications/Codex.app/Contents/Resources/cua_node/bin/node
exec "$BIN" -c features.code_mode_host=true app-server --analytics-default-enabled \
  -c plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true \
  --listen "unix://$SOCK"
