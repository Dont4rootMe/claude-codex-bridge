# claude-codex-bridge

Мост между **Claude Code** и приложением **Codex** (macOS). Claude — исследователь: сам
думает, ищет и пишет, а агентов Codex использует как помощников двух видов:
- **консилиум** (`codex-bridge consult`) — независимое второе мнение, критика черновика,
  параллельные долгие исследовательские треки (по умолчанию самая сильная модель);
- **исполнители** (`codex-bridge start`) — код, баги, эксперименты, окружения.

Каждая задача — обычный тред в Codex: его видно в приложении вживую, в нём можно
вмешаться руками. В обратную сторону Codex умеет читать чаты Claude, а агенты Codex через
тот же мост общаются друг с другом.

> Неофициальный инструмент. Опирается на недокументированные механизмы Codex
> (протокол `codex app-server`, `codex queue`, ссылки `codex://threads/<id>`, внутренняя
> переменная `CODEX_APP_SERVER_WS_URL`) и может сломаться после обновления Codex.
> Проверено на Codex.app 26.1103 (CLI 0.167.0-alpha.3.2), macOS 26.

## Что внутри

| Файл | Назначение |
|---|---|
| `codex_bridge.py` | команда `codex-bridge`: ставить задачи, ждать, читать треды Codex и чаты Claude |
| `wsunix.py` | минимальный WebSocket-клиент поверх unix-сокета (для общего режима) |
| `claude/skills/codex-teamlead/` | скилл Claude Code: когда и как делегировать в Codex, выбор модели, приёмка |
| `claude/CLAUDE.md.example` | шаблон глобального правила для `~/.claude/CLAUDE.md` |
| `codex/skills/claude-context/` | скилл Codex: читать чаты Claude и связи «чат → треды» |
| `codex/AGENTS.md.example` | шаблон для `~/.codex/AGENTS.md`: поручения `[Claude → Codex]`, «Отчёт для Claude» |
| `shared-server.sh`, `codex-shared-*`, `codex-app-tools-link`, `gui-connected`, `launchd/` | общий режим (необязательно) |
| `runtime_mcp.py` | MCP-сервер `codex_runtime` с `load_workspace_dependencies` — пути к встроенному набору Codex для документов/таблиц/слайдов/PDF в общем режиме |
| `install.sh` | ставит команды в `~/.local/bin` и скиллы ссылками на репозиторий |

Только стандартная библиотека Python 3 и bash. Состояние моста: `~/.codex-bridge/`
(журнал задач `journal.jsonl`, сокет и логи общего режима).

## Установка

```bash
git clone https://github.com/Dont4rootMe/claude-codex-bridge.git ~/projects/claude-codex-bridge
~/projects/claude-codex-bridge/install.sh
```

Затем добавьте правила из `claude/CLAUDE.md.example` в `~/.claude/CLAUDE.md` и из
`codex/AGENTS.md.example` в `~/.codex/AGENTS.md`. Мост использует CLI из Codex.app;
`codex` из Homebrew может быть устаревшим и для этого не годится.

## Использование

```bash
# новая задача — новый тред в Codex (промпт лучше из файла)
codex-bridge start --title "Починить OOM в загрузчике" --cwd ~/projects/x \
  --model sol --effort xhigh --prompt-file brief.md
# консилиум: N независимых тредов с брифом эксперта (opinion, critique — только чтение; research — полный доступ)
codex-bridge consult --kind opinion --n 2 --title "Почему растёт bias" --prompt-file question.md
codex-bridge send <thread> --prompt "уточнение"        # следующее сообщение в тот же тред
codex-bridge wait <t1> <t2> ...                        # дождаться ответов (параллельно)
codex-bridge read <thread> --last 1 --items            # итог, команды и правки файлов
codex-bridge status | running | list --search "..."    # мои задачи / что идёт сейчас / все треды
codex-bridge claude-sessions --search "..."            # чаты Claude
codex-bridge claude-read <id> --last 30                # переписка чата Claude
codex-bridge links                                     # какой чат Claude (или агент Codex) породил какие треды
codex-bridge mode                                      # режим моста: gui или shared
codex-bridge reload-mcp                                # перечитать MCP на общем сервере (откажется, пока идут ходы)
```

Мостом пользуются и сами агенты Codex: внутри их команд есть `CODEX_THREAD_ID`, и мост
подписывает такие сообщения `[Codex → Codex]` с адресом отправителя (в собственный тред
писать не даёт).

Параметры `start` по умолчанию: `--sandbox danger-full-access --approval never` (как
обычная работа в приложении с полным доступом); для чисто читающих задач —
`--sandbox read-only`. Модели задаются алиасами `terra`, `sol`, `astra` или полным slug.
Astra мост не запустит без `--astra-approved`: это закреплённое правило «Astra только с
явного согласия пользователя» (поменяйте под себя в `codex_bridge.py` и в скилле).

## Два режима

Мост выбирает режим сам (`codex-bridge mode`).

### Обычный (`gui`) — по умолчанию

Приложение Codex работает со своим встроенным сервером.
1. Тред создаётся во временном `codex app-server` (`thread/start`, `thread/name/set`),
   процесс сразу закрывается.
2. Промпт кладётся в очередь треда: `codex queue --thread <id> --message ...`.
3. Тред один раз открывается в приложении в фоне: `open -g codex://threads/<id>`
   (фокус не перехватывается, но окно Codex переключается на этот тред).
4. Дальше приложение само выполняет сообщения из очереди этого треда, даже если в окне
   открыт другой тред. Результат читается из истории (`thread/turns/list`).

Все возможности приложения сохраняются. Цена — переключение окна при создании треда.

### Общий (`shared`) — по желанию

Приложение подключается к долгоживущему `codex app-server` на unix-сокете
`~/.codex-bridge/app-server.sock` (каталог 700, без TCP-порта), к нему же ходит мост.
Новые треды сами появляются в сайдбаре, окно не переключается, ходы переживают
отключение моста.

```bash
codex-shared-on     # LaunchAgent'ы + launchctl setenv CODEX_APP_SERVER_WS_URL=ws+unix://localhost$HOME/.codex-bridge/app-server.sock:/rpc
codex-shared-launch # перезапустить Codex в общем режиме прямо сейчас (откажется, если идут задачи)
codex-shared-off [--stop-server]   # вернуть обычный режим
```

- `com.<user>.codex-shared-server` — сервер (RunAtLoad + KeepAlive), лог `~/.codex-bridge/app-server.log`;
- `com.<user>.codex-shared-helper` — при входе в систему выставляет переменную, при каждом
  запуске Codex (WatchPaths на `/private/tmp/codex-app-tools`) обновляет ссылку на служебный
  канал окна и перезапускает сервер, если Codex обновился (бинарник новее `server.started`);
- автозапуск — через launchd, не через `.zshrc` (тот выполняется лишь при открытии терминала).

**Ограничение и чем оно закрыто.** В общем режиме у агентов Codex нет инструментов
приложения `codex_app`: окно пускает к своему каналу только процессы, которые запустило
само (`dynamic_app_tools_peer_rejected reason=untrusted-process-ancestry` в логе
приложения). Это защита приложения; обходить её не нужно. Что остаётся и чем заменено
(проверено 2026-11-09):

| Возможность | В общем режиме |
|---|---|
| субагенты и переписка с ними | встроенные `collaboration.*` сервера (`spawn_agent`, `send_message`, `wait_agent`, `list_agents`, `followup_task`, `interrupt_agent`) — работают |
| создать отдельный тред, написать в другой тред, дождаться и прочитать ответ | `codex-bridge start/send/wait/read` изнутри Codex — работает, треды сразу видны в сайдбаре |
| найти встроенные библиотеки (`load_workspace_dependencies`) | `runtime_mcp.py` (MCP `codex_runtime`) — работает, скиллы документов и слайдов проходят |
| автоматизации, инструменты worktree, организация сайдбара, панели приложения | нет (worktree можно через обычный `git worktree`) |

Вероятно, недоступны и браузер в приложении и Computer Use (не проверялось).

**Лимит файлов.** launchd даёт процессам мягкий лимит в 256 открытых файлов; серверу с
десятками тредов и MCP-серверов этого мало («Too many open files», падающие MCP и ходы).
`shared-server.sh` поднимает лимит до 65536.

**Осторожно с перезагрузками.** `config/mcpServer/reload` перезапускает MCP во всех
загруженных тредах и обрывает идущие ходы («application network permission was revoked»);
перезапуск сервера — тоже. Поэтому `codex-bridge reload-mcp`, `codex-shared-launch` и
`codex-shared-off` отказываются работать, пока в Codex есть незавершённые ходы. После
перезапуска сервера окно Codex переподключается само (~35 с).

## Что выяснилось по ходу (полезно, если будете чинить)

- Тред, созданный отдельным `app-server`, не появляется в уже открытом приложении без
  перезагрузки; помогает либо ссылка `codex://threads/<id>`, либо общий сервер.
- `app-server`, в котором тред загружен, сам забирает сообщение из очереди — короткоживущий
  процесс моста прервёт ход при выходе. Поэтому мост закрывает свой процесс до `codex queue`.
- Два почти одновременных `open -g codex://threads/...` перебивают друг друга — мост
  сериализует открытия (`~/.codex-bridge/open.lock`) и повторяет их каждые 20 с.
- Незаконченный ход, прочитанный с диска, имеет `status` `interrupted`/`inProgress` и
  `completedAt = null`; так же выглядит ход, оборванный закрытием приложения. `codex-bridge
  running` считает ход идущим, только если в треде была активность за 10 минут или сервер
  выполняет команду.
- Скрытый `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1` в этой сборке недостижим (приложение всегда
  передаёт config overrides), поэтому общий режим сделан через `CODEX_APP_SERVER_WS_URL`;
  встроенная WebSocket-библиотека понимает `ws+unix://localhost<путь>:/rpc`.
- Переменную `CODEX_APP_SERVER_WS_URL` читает только Electron-приложение Codex (и его
  старые копии с тем же bundle id); CLI `codex` её не читает.
- Сервер под launchd читает `~/Documents`, `~/Downloads` и iCloud Drive без запросов TCC
  (проверено чтение).

## Лицензия

Не выбрана.
