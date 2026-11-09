#!/bin/bash
# Установка claude-codex-bridge (macOS, приложение Codex, Claude Code).
# Ставит команды в ~/.local/bin и скиллы для Claude Code и Codex символическими ссылками
# на этот репозиторий. Глобальные правила не трогает — шаблоны лежат в claude/ и codex/.
# Общий режим включается отдельно: codex-shared-on (см. README).
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN" "$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.codex-bridge"
chmod 700 "$HOME/.codex-bridge"

link() {  # link SRC DST — существующие файлы, которые не являются ссылками, не затирает
  local src="$1" dst="$2"
  if [ -L "$dst" ] || [ ! -e "$dst" ]; then
    ln -sfn "$src" "$dst"
    echo "  $dst -> $src"
  else
    echo "  пропущено: $dst уже существует и не является ссылкой" >&2
  fi
}

echo "команды:"
link "$REPO/codex_bridge.py" "$BIN/codex-bridge"
for c in codex-shared-on codex-shared-off codex-shared-launch; do
  link "$REPO/$c" "$BIN/$c"
done
echo "скиллы:"
link "$REPO/claude/skills/codex-teamlead" "$HOME/.claude/skills/codex-teamlead"
link "$REPO/codex/skills/claude-context" "$HOME/.codex/skills/claude-context"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "добавьте в ~/.zshrc: export PATH=\"$BIN:\$PATH\"" ;;
esac
CODEX_CLI=/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex
if [ ! -x "$CODEX_CLI" ]; then
  echo "внимание: не найдено приложение Codex (/Applications/Codex.app)" >&2
else
  echo "MCP для Codex:"
  if "$CODEX_CLI" mcp get codex_runtime >/dev/null 2>&1; then
    echo "  codex_runtime уже зарегистрирован"
  else
    "$CODEX_CLI" mcp add codex_runtime -- "$REPO/runtime_mcp.py" && echo "  codex_runtime -> $REPO/runtime_mcp.py"
  fi
fi

cat <<EOF

Дальше:
  1) добавьте правило из $REPO/claude/CLAUDE.md.example в ~/.claude/CLAUDE.md
  2) добавьте правило из $REPO/codex/AGENTS.md.example в ~/.codex/AGENTS.md
  3) проверьте: codex-bridge mode
  4) по желанию: codex-shared-on — общий режим (у него есть ограничения, см. README)
EOF
