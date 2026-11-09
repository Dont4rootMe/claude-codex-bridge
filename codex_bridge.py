#!/usr/bin/env python3
"""codex-bridge: общий мост Claude <-> Codex.

Claude (тимлид) ставит задачи Codex в обычных тредах, которые пользователь видит
в приложении Codex вживую; Codex и Claude могут читать переписку друг друга.

Два режима (выбираются автоматически, см. `codex-bridge mode`):

shared — приложение Codex подключено к общему app-server на unix-сокете
  (~/.codex-bridge/app-server.sock, см. shared-server.sh). Мост говорит с тем же
  сервером: ходы запускаются напрямую (turn/start), пользователь видит их вживую,
  окно Codex никуда не переключается.

gui — приложение работает со своим встроенным сервером (проверено 2026-11-09 на
  Codex.app 26.1103, CLI 0.167):
  * тред создаётся во временном `codex app-server` (thread/start + name/set);
  * сообщения кладутся в очередь треда через `codex queue`;
  * исполняет их приложение: тред один раз открывается в фоне через
    `open -g codex://threads/<id>`, после чего приложение само берёт сообщения
    из очереди, даже если в окне открыт другой тред.

Результат в обоих режимах читается из истории треда (thread/turns/list).
Только стандартная библиотека Python 3.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from wsunix import WSUnix  # noqa: E402

CODEX_BIN = os.environ.get(
    "CODEX_BRIDGE_CODEX", "/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex")
CODEX_APP = "/Applications/Codex.app"
STATE_DIR = os.path.expanduser(os.environ.get("CODEX_BRIDGE_HOME", "~/.codex-bridge"))
JOURNAL = os.path.join(STATE_DIR, "journal.jsonl")
SHARED_SOCK = os.path.join(STATE_DIR, "app-server.sock")
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
HEADER = "[Claude → Codex]"
CODEX_HEADER = "[Codex → Codex]"
FINAL = {"completed", "failed", "interrupted"}

# Политика моделей пользователя (2026-11-09), одна для исследований и исполнения:
#   минимально достаточный уровень, а не самый сильный (лимиты Codex быстро кончаются)
#   простое и механическое   -> gpt-6.1-terra  medium (high/xhigh — если нужна аккуратность)
#   обычное и сложное        -> gpt-6.1-sol    high | xhigh | max | ultra
#   сверх этого              -> gpt-6-astra    ТОЛЬКО после явного «да» пользователя
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_EFFORT = "xhigh"
MODEL_ALIASES = {"terra": "gpt-6.1-terra", "sol": "gpt-6.1-sol", "astra": "gpt-6-astra",
                 "luna": "gpt-6-luna"}
EFFORT_ALIASES = {"med": "medium", "hard": "high", "extra": "xhigh", "x-high": "xhigh"}


def die(msg: str, code: int = 1):
    print(f"codex-bridge: {msg}", file=sys.stderr)
    sys.exit(code)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- app-server

class AppServer:
    """Минимальный JSON-RPC клиент к `codex app-server` (stdio)."""

    def __init__(self):
        if not os.path.exists(CODEX_BIN):
            die(f"не найден Codex CLI: {CODEX_BIN}")
        self.p = subprocess.Popen([CODEX_BIN, "app-server"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "claude-codex-bridge", "version": "1.0.0"}})
        self._send({"method": "initialized", "params": {}})

    def _send(self, obj):
        with self._lock:
            self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.p.stdin.flush()

    def _reader(self):
        for line in self.p.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                q = self._pending.pop(msg["id"], None)
                if q:
                    q.put(msg)
            elif "id" in msg and "method" in msg:
                # Мост сам ходы не выполняет; на запросы сервера отвечаем отказом.
                self._send({"id": msg["id"], "error": {"code": -32601,
                            "message": "codex-bridge does not handle server requests"}})

    def call(self, method, params, timeout=120):
        i = next(self._ids)
        q: queue.Queue = queue.Queue()
        self._pending[i] = q
        self._send({"id": i, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"{method}: нет ответа за {timeout} с")
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg["result"]

    def close(self):
        try:
            self.p.terminate()
            self.p.wait(timeout=5)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SharedServer(AppServer):
    """Клиент общего app-server (WebSocket поверх unix-сокета), к которому подключено и
    приложение Codex. На запросы сервера (подтверждения) не отвечает: их решает
    пользователь в приложении."""

    def __init__(self, timeout: float = 5):
        self.ws = WSUnix(SHARED_SOCK, timeout=timeout)
        self._ids = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._ws_reader, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "claude-codex-bridge", "version": "1.1.0"}})
        self._send({"method": "initialized", "params": {}})

    def _send(self, obj):
        self.ws.send_text(json.dumps(obj, ensure_ascii=False))

    def _ws_reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv_text())
                if "id" in msg and ("result" in msg or "error" in msg):
                    q = self._pending.pop(msg["id"], None)
                    if q:
                        q.put(msg)
        except Exception:
            for q in list(self._pending.values()):
                q.put({"error": {"message": "соединение с общим сервером закрыто"}})

    def close(self):
        self.ws.close()


def _processes():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def gui_on_own_server() -> bool:
    """Приложение Codex запущено со своим встроенным app-server (дочерний процесс без --listen)."""
    rows = _processes()
    gui = {pid for pid, _, cmd in rows if cmd.startswith(f"{CODEX_APP}/Contents/MacOS/")}
    return any(ppid in gui and " app-server" in cmd and "--listen" not in cmd
               for _, ppid, cmd in rows)


def shared_alive() -> bool:
    if not os.path.exists(SHARED_SOCK):
        return False
    try:
        WSUnix(SHARED_SOCK, timeout=2).close()
        return True
    except Exception:
        return False


def bridge_mode() -> str:
    forced = os.environ.get("CODEX_BRIDGE_MODE", "").lower()
    if forced in ("shared", "gui"):
        return forced
    if gui_on_own_server():
        return "gui"
    return "shared" if shared_alive() else "gui"


def connect(mode: str | None = None) -> AppServer:
    return SharedServer() if (mode or bridge_mode()) == "shared" else AppServer()


def latest_turn(s: AppServer, tid: str, full=True):
    r = s.call("thread/turns/list", {"threadId": tid, "limit": 1, "sortDirection": "desc",
                                     "itemsView": "full" if full else "summary"})
    data = r.get("data") or []
    return data[0] if data else None


def all_turns(s: AppServer, tid: str):
    turns, cursor = [], None
    while True:
        params = {"threadId": tid, "limit": 50, "sortDirection": "asc", "itemsView": "full"}
        if cursor:
            params["cursor"] = cursor
        r = s.call("thread/turns/list", params)
        turns.extend(r.get("data") or [])
        cursor = r.get("nextCursor")
        if not cursor:
            return turns


def turn_state(turn) -> str:
    """Статус хода с поправкой на чтение с диска: незаконченный ход, который исполняет
    приложение, читается как `interrupted` (или `inProgress`) без completedAt."""
    st = (turn or {}).get("status") or "—"
    if st in ("interrupted", "inProgress") and not (turn or {}).get("completedAt"):
        return "inProgress"
    return st


def user_text(item) -> str:
    return "\n".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "text")


def final_answer(turn) -> str:
    msgs = [i for i in (turn or {}).get("items", []) if i.get("type") == "agentMessage"]
    if not msgs:
        return ""
    finals = [m for m in msgs if (m.get("phase") or "").lower() in ("final_answer", "final")]
    return (finals[-1] if finals else msgs[-1]).get("text", "")


def item_line(it) -> str | None:
    t = it.get("type")
    if t == "commandExecution":
        cmd = it.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        return f"  $ {str(cmd)[:160]}  (exit {it.get('exitCode')})"
    if t == "fileChange":
        paths = [c.get("path") for c in it.get("changes", []) if isinstance(c, dict)]
        return "  ✎ " + ", ".join(p for p in paths if p)[:200]
    if t in ("mcpToolCall", "dynamicToolCall"):
        return f"  ⚙ {it.get('server', '')}.{it.get('tool', '')}"
    if t == "webSearch":
        return f"  🔎 {it.get('query', '')}"
    return None


# --------------------------------------------------------------------------- helpers

def journal(event: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    event = {"ts": now_iso(), **event}
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_journal():
    if not os.path.exists(JOURNAL):
        return []
    out = []
    with open(JOURNAL, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def codex_app_running() -> bool:
    r = subprocess.run(["pgrep", "-f", f"{CODEX_APP}/Contents/MacOS/"], capture_output=True)
    return r.returncode == 0


def open_in_app(tid: str):
    """Фоново открыть тред в приложении Codex (фокус не перехватывается)."""
    if not codex_app_running():
        subprocess.run(["open", "-g", "-a", CODEX_APP])
        for _ in range(30):
            time.sleep(1)
            if codex_app_running():
                break
        time.sleep(5)
    subprocess.run(["open", "-g", f"codex://threads/{tid}"])


def queue_message(tid: str, text: str):
    r = subprocess.run([CODEX_BIN, "queue", "--thread", tid, "--message", text],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"codex queue не сработал: {r.stderr.strip()[-400:]}")


def _open_lock():
    """Фоновые открытия сериализуются между процессами: два почти одновременных
    `open -g codex://threads/...` перебивают друг друга, и первый тред не подключается."""
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(os.path.join(STATE_DIR, "open.lock"), "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def wait_started(s: AppServer, tid: str, prev_turn_id, open_first: bool, grace=12, limit=120):
    """Убедиться, что приложение взяло сообщение; при необходимости открыть тред в фоне
    (повторяя открытие каждые 20 с)."""
    lock, last_open = None, None
    deadline = time.time() + limit
    t0 = time.time()
    try:
        while time.time() < deadline:
            lt = latest_turn(s, tid, full=False)
            if lt and lt.get("id") != prev_turn_id:
                return True
            due = (last_open is None and (open_first or time.time() - t0 > grace)) or \
                  (last_open is not None and time.time() - last_open > 20)
            if due:
                if lock is None:
                    lock = _open_lock()           # ждём, пока другие откроют свои треды
                    deadline = time.time() + limit
                open_in_app(tid)
                last_open = time.time()
            time.sleep(3)
        return False
    finally:
        if lock is not None:
            lock.close()


def resolve_model(model: str, effort: str, astra_approved: bool):
    model = MODEL_ALIASES.get(model.lower(), model)
    effort = EFFORT_ALIASES.get(effort.lower(), effort.lower())
    if "astra" in model and not astra_approved:
        die("Astra запускается только после явного согласия пользователя. Спроси его одним "
            "вопросом и предложи уровень («Здесь нужна Astra на <effort>, потому что … Разрешаешь "
            "или Sol на <effort>?»); если разрешил — добавь --astra-approved, иначе sol.", 4)
    return model, effort


def detect_claude_session():
    """Текущий чат Claude Code = самый свежий транскрипт, изменённый за последние 15 минут."""
    files = [f for f in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"))]
    if not files:
        return None
    f = max(files, key=os.path.getmtime)
    if time.time() - os.path.getmtime(f) > 900:
        return None
    return os.path.splitext(os.path.basename(f))[0]


def read_prompt(args) -> str:
    if getattr(args, "prompt", None):
        return args.prompt
    if getattr(args, "prompt_file", None):
        with open(args.prompt_file, encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    die("нужен текст задачи: --prompt, --prompt-file или stdin")


def sender_and_session(a):
    """Кто пишет: агент Codex (внутри его команд есть CODEX_THREAD_ID) или Claude."""
    sender = os.environ.get("CODEX_THREAD_ID") or None
    if a.claude_session == "none":
        session = None
    elif a.claude_session == "auto":
        session = None if sender else detect_claude_session()
    else:
        session = a.claude_session
    return sender, session


def compose(prompt: str, claude_session: str | None, sender: str | None = None) -> str:
    if sender:
        head = (f"{CODEX_HEADER} Сообщение от агента Codex из треда {sender}. Свой итоговый ответ "
                f"дай здесь — отправитель прочитает его сам (`codex-bridge wait/read`); написать "
                f"ему отдельно: `codex-bridge send {sender} --prompt ...`.")
        return head + "\n\n" + prompt.strip()
    head = f"{HEADER} Задача от Claude (тимлид)."
    if claude_session:
        head += (f" Чат Claude: {claude_session} — полный контекст обсуждения: "
                 f"`codex-bridge claude-read {claude_session}`.")
    return head + "\n\n" + prompt.strip()


def fmt_ts(sec):
    if not sec:
        return "-"
    return datetime.fromtimestamp(sec).strftime("%d.%m %H:%M")


# --------------------------------------------------------------------------- commands

def cmd_start(a):
    model, effort = resolve_model(a.model, a.effort, a.astra_approved)
    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    if not os.path.isdir(cwd):
        die(f"нет такой папки: {cwd}")
    prompt = read_prompt(a)
    sender, session = sender_and_session(a)
    params = {"cwd": cwd, "model": model, "config": {"model_reasoning_effort": effort},
              "sandbox": a.sandbox, "approvalPolicy": a.approval}
    mode = bridge_mode()
    if mode == "shared":
        # Общий сервер живёт долго: тред остаётся в нём загруженным, ход идёт там же,
        # где его видит приложение. Окно Codex не переключается.
        with SharedServer() as s:
            tid = s.call("thread/start", params)["thread"]["id"]
            s.call("thread/name/set", {"threadId": tid, "name": a.title})
            s.call("turn/start", {"threadId": tid, "input": [
                {"type": "text", "text": compose(prompt, session, sender)}]})
        started = True
        if a.open:
            open_in_app(tid)
    else:
        # Тред создаётся во временном app-server, который сразу закрывается: иначе он сам
        # заберёт сообщение из очереди и прервёт ход при выходе. Исполнять должен GUI.
        with AppServer() as s:
            tid = s.call("thread/start", params)["thread"]["id"]
            s.call("thread/name/set", {"threadId": tid, "name": a.title})
        queue_message(tid, compose(prompt, session, sender))
    journal({"event": "start", "thread_id": tid, "title": a.title, "cwd": cwd, "model": model,
             "effort": effort, "claude_session": session, "from_thread": sender,
             "prev_turn_id": None, "mode": mode, "prompt_preview": prompt.strip()[:300]})
    if mode == "gui":
        if a.no_open:
            started = True
        else:
            with AppServer() as s:  # только чтение: тред в этом процессе не загружается
                started = wait_started(s, tid, None, open_first=True)
    out = {"thread_id": tid, "title": a.title, "model": model, "effort": effort, "cwd": cwd,
           "link": f"codex://threads/{tid}", "started": started}
    print(json.dumps(out, ensure_ascii=False) if a.json else
          f"✓ {a.title}\n  thread: {tid}\n  {model} · {effort} · {cwd}\n  {out['link']}"
          + ("" if started else "\n  ⚠ приложение пока не взяло задачу (проверь, что Codex открыт)"))
    if getattr(a, "_return_out", False):
        return out
    if a.wait:
        return wait_threads([tid], a.timeout, a.json)


CONSULT_KINDS = {
    "opinion": ("Мнение", "read-only",
                "Нужно твоё независимое экспертное мнение. Тебя опрашивают параллельно с другими "
                "экспертами, и мнения сравнят, поэтому ценно именно независимое суждение, а не "
                "угадывание ожидаемого ответа.\n\nРазбери вопрос сам, с нуля. Ищи и проверяй "
                "источники в интернете, если это помогает; читай указанные материалы. Ничего не "
                "меняй в файлах.\n\nОтветь так:\n1. Позиция — короткий ответ.\n2. Обоснование — "
                "по пунктам.\n3. Источники — ссылки и что именно в них сказано.\n4. Альтернативы — "
                "другие позиции и почему ты их не принимаешь.\n5. Уверенность — низкая / средняя / "
                "высокая для каждого ключевого утверждения.\n6. Что изменило бы твоё мнение и что "
                "стоит проверить дальше.\n\nВопрос и материалы:\n"),
    "critique": ("Критика", "read-only",
                 "Ты — рецензент (red team). Ниже материал, который готовит Claude. Найди "
                 "фактические ошибки, необоснованные утверждения, логические дыры, пропущенные "
                 "альтернативы и источники, слабые места методологии. Проверяй утверждения по "
                 "источникам, если это помогает. Ничего не меняй в файлах и не переписывай текст "
                 "целиком.\n\nДля каждого замечания: где, в чём проблема, насколько важно "
                 "(критично / существенно / мелочь), как исправить. В конце — три самых важных "
                 "замечания.\n\nМатериал:\n"),
    "research": ("Исследование", "danger-full-access",
                 "Это параллельный исследовательский трек: Claude ведёт свой анализ, а ты глубоко и "
                 "самостоятельно прорабатываешь поручение ниже — столько времени, сколько нужно. "
                 "Ищи источники и проверяй их, при необходимости пиши и запускай код. Не "
                 "останавливайся на первом найденном.\n\nИтог: главные находки с источниками; что "
                 "подтверждено, что нет; уровень уверенности; открытые вопросы и что проверить "
                 "дальше.\n\nПоручение:\n"),
}


def cmd_consult(a):
    """Консилиум: N тредов Codex с готовым брифом для второго мнения, критики или
    параллельного исследования. Модели по умолчанию нет: её и effort выбирают явно под
    задачу (минимально достаточные); Astra — только с --astra-approved."""
    if not a.model or not a.effort:
        die("укажи --model и --effort под задачу: terra · medium для простого, sol · high/xhigh "
            "для обычного, sol · max/ultra для трудного; astra — только с разрешения "
            "пользователя (--astra-approved)", 2)
    label, sandbox, wrapper = CONSULT_KINDS[a.kind]
    body = read_prompt(a)
    outs = []
    for k in range(1, a.n + 1):
        suffix = f" ({k}/{a.n})" if a.n > 1 else ""
        sa = argparse.Namespace(
            title=f"{label}: {a.title}{suffix}", cwd=a.cwd, model=a.model, effort=a.effort,
            astra_approved=a.astra_approved, sandbox=a.sandbox or sandbox, approval="never",
            no_open=False, open=False, prompt=wrapper + body.strip(), prompt_file=None,
            claude_session=a.claude_session, wait=False, timeout=a.timeout, json=a.json,
            _return_out=True)
        outs.append(cmd_start(sa))
    ids = " ".join(o["thread_id"] for o in outs)
    if not a.json:
        print(f"\nдождаться: codex-bridge wait {ids}")
    if a.wait:
        return wait_threads([o["thread_id"] for o in outs], a.timeout, a.json)


def cmd_send(a):
    prompt = read_prompt(a)
    sender, session = sender_and_session(a)
    if sender and a.thread == sender:
        die("нельзя отправлять сообщение в собственный тред: ответ просто дай в этом ходе")
    text = compose(prompt, session, sender)
    mode = bridge_mode()
    with connect(mode) as s:
        lt = latest_turn(s, a.thread, full=False)
        prev = lt.get("id") if lt else None
        busy = lt is not None and turn_state(lt) == "inProgress"
        journal({"event": "send", "thread_id": a.thread, "claude_session": session,
                 "from_thread": sender, "prev_turn_id": prev, "mode": mode,
                 "prompt_preview": prompt.strip()[:300]})
        if mode == "shared":
            loaded = s.call("thread/loaded/list", {}).get("data") or []
            if a.thread not in loaded:
                s.call("thread/resume", {"threadId": a.thread, "excludeTurns": True})
            if busy:
                queue_message(a.thread, text)   # сервер возьмёт после текущего хода
            else:
                s.call("turn/start", {"threadId": a.thread, "input": [{"type": "text", "text": text}]})
            started = True
        else:
            queue_message(a.thread, text)
            started = True if busy else wait_started(s, a.thread, prev, open_first=False)
    print(f"✓ отправлено в {a.thread}" + (" (встанет после текущего хода)" if busy else "")
          + ("" if started else "\n  ⚠ приложение пока не взяло сообщение"))
    if a.wait:
        return wait_threads([a.thread], a.timeout, a.json)


def last_prev_turn(tid):
    prev = "__none__"
    for e in read_journal():
        if e.get("thread_id") == tid and e.get("event") in ("start", "send"):
            prev = e.get("prev_turn_id")
    return prev


def wait_threads(tids, timeout, as_json=False):
    """Ждать, пока в каждом треде завершится ход, начатый после последней отправки моста."""
    targets = {t: last_prev_turn(t) for t in tids}
    pending = set(tids)
    results = {}
    t0 = time.time()
    s = None
    try:
        while pending and time.time() - t0 < timeout:
            try:
                if s is None:
                    s = connect()  # режим определяется заново: сервер мог перезапуститься
                for tid in sorted(pending):
                    lt = latest_turn(s, tid, full=True)
                    if not lt:
                        continue
                    prev = targets[tid]
                    if prev != "__none__" and lt.get("id") == prev:
                        continue  # новый ход ещё не начался
                    if turn_state(lt) in FINAL:
                        pending.discard(tid)
                        results[tid] = lt
                        emit_result(tid, lt, as_json)
            except (RuntimeError, OSError, ConnectionError) as ex:
                print(f"  (соединение потеряно: {ex}; переподключаюсь)", file=sys.stderr)
                if s is not None:
                    s.close()
                s = None
            if pending:
                el = time.time() - t0
                time.sleep(5 if el < 120 else 15 if el < 1800 else 30)
    finally:
        if s is not None:
            s.close()
    for tid in pending:
        print(json.dumps({"thread_id": tid, "status": "timeout"}) if as_json
              else f"⏱ {tid}: не завершился за {int(timeout)} с — проверь позже `codex-bridge wait {tid}`")
    if pending:
        return 3
    return 0 if all(r.get("status") == "completed" for r in results.values()) else 2


def emit_result(tid, turn, as_json):
    ans = final_answer(turn)
    if as_json:
        print(json.dumps({"thread_id": tid, "status": turn.get("status"),
                          "duration_ms": turn.get("durationMs"), "final": ans}, ensure_ascii=False))
        return
    dur = turn.get("durationMs")
    print(f"\n━━ {tid} · {turn.get('status')}" + (f" · {dur // 1000} с" if dur else ""))
    err = turn.get("error")
    if err:
        print(f"ошибка: {err.get('message', err)}")
    print(ans or "(нет итогового сообщения)")


def cmd_wait(a):
    return wait_threads(a.threads, a.timeout, a.json)


def cmd_read(a):
    with connect() as s:
        turns = all_turns(s, a.thread)
    if a.last:
        turns = turns[-a.last:]
    for t in turns:
        for it in t.get("items", []):
            if it.get("type") == "userMessage":
                txt = user_text(it)
                who = ("Claude" if txt.startswith(HEADER) else
                       "Агент Codex" if txt.startswith(CODEX_HEADER) else "Пользователь")
                print(f"\n### {who}:\n{txt[:a.max_chars]}")
        if a.items:
            lines = [l for l in (item_line(i) for i in t.get("items", [])) if l]
            if lines:
                print("\n".join(lines[-40:]))
        print(f"\n### Codex [{turn_state(t)}]:\n{final_answer(t)[:a.max_chars]}")


def cmd_status(a):
    seen, rows = set(), []
    for e in reversed(read_journal()):
        tid = e.get("thread_id")
        if tid and tid not in seen:
            seen.add(tid)
            rows.append(tid)
    meta = {}
    for e in read_journal():
        if e.get("event") == "start":
            meta[e["thread_id"]] = e
    rows = rows[:a.limit]
    if not rows:
        print("журнал пуст")
        return
    with connect() as s:
        for tid in rows:
            m = meta.get(tid, {})
            try:
                lt = latest_turn(s, tid, full=False)
                st = turn_state(lt) if lt else "—"
                ts = fmt_ts((lt or {}).get("completedAt") or (lt or {}).get("startedAt"))
            except Exception as ex:
                st, ts = f"ошибка: {ex}", "-"
            icon = {"inProgress": "⏳", "completed": "✓", "failed": "✗", "interrupted": "■"}.get(st, "·")
            print(f"{icon} {st:<11} {ts:<11} {m.get('model', '?')}·{m.get('effort', '?'):<6} "
                  f"{m.get('title', '?')[:50]}  {tid}")


def cmd_list(a):
    params = {"limit": a.limit}
    if a.search:
        params["searchTerm"] = a.search
    if a.cwd:
        params["cwd"] = os.path.abspath(os.path.expanduser(a.cwd))
    with connect() as s:
        r = s.call("thread/list", params)
    for t in r.get("data", []):
        print(f"{t['id']}  {fmt_ts(t.get('updatedAt') or t.get('createdAt'))}  "
              f"{(t.get('name') or t.get('preview') or '')[:60]}  [{t.get('cwd', '')}]")


STALE_SECONDS = 600
_HELPER_MARKERS = ("server.mjs", "node_repl", "SkyComputerUse", "research-memory", "codex-code-mode-host",
                   "exec-server", "mcp")


def busy_commands():
    """Процессы-команды, которые сейчас выполняют серверы Codex (без MCP-серверов и помощников).
    Нужны, чтобы долгий «молчащий» эксперимент не приняли за оборванный ход."""
    rows = _processes()
    servers = [pid for pid, _, cmd in rows if " app-server" in cmd and "CodexCLI.app" in cmd]
    children: dict[int, list] = {}
    for pid, ppid, cmd in rows:
        children.setdefault(ppid, []).append((pid, cmd))
    out, stack = [], list(servers)
    while stack:
        for pid, cmd in children.get(stack.pop(), []):
            if any(m in cmd for m in _HELPER_MARKERS):
                continue
            out.append((pid, cmd))
            stack.append(pid)
    return out


def running_threads(hours: float = 6):
    """(живые, оборванные): треды с незавершённым ходом. Ход без completedAt, но без
    активности > 10 мин и без выполняющихся команд считается оборванным (например,
    приложение закрыли посреди хода)."""
    now = time.time()
    commands = busy_commands()
    live, stale = [], []
    with connect() as s:
        r = s.call("thread/list", {"limit": 40, "sortKey": "updated_at"})
        for t in r.get("data", []):
            upd = t.get("updatedAt") or 0
            if now - upd > hours * 3600:
                continue
            d = s.call("thread/turns/list", {"threadId": t["id"], "limit": 1, "sortDirection": "desc",
                                             "itemsView": "notLoaded"}).get("data") or []
            if not d or turn_state(d[0]) != "inProgress":
                continue
            title = " ".join((t.get("name") or t.get("preview") or "").split())[:60]
            if now - upd <= STALE_SECONDS or commands:
                live.append((t["id"], title))
            else:
                stale.append((t["id"], title, int((now - upd) // 60)))
    return live, stale


def cmd_running(a):
    """Треды Codex (любые, не только мои), в которых сейчас идёт ход; оборванные — в stderr."""
    live, stale = running_threads(a.hours)
    for tid, title in live:
        print(f"{tid}  {title}")
    for tid, title, mins in stale:
        print(f"оборванный ход (нет активности {mins} мин): {tid}  {title}", file=sys.stderr)


def cmd_reload_mcp(a):
    """Перечитать MCP-серверы на общем сервере. Перезапускает MCP во ВСЕХ загруженных тредах
    и рвёт идущие ходы (2026-11-09 так упали две задачи) — поэтому только когда ничего не идёт."""
    if bridge_mode() != "shared":
        die("только для общего режима; в обычном Codex сам подхватит MCP в новых тредах")
    if not a.force:
        live, stale = running_threads()
        if live or stale:
            for tid, title in live:
                print(f"  идёт: {tid}  {title}", file=sys.stderr)
            for tid, title, mins in stale:
                print(f"  незавершённый ход ({mins} мин без активности): {tid}  {title}", file=sys.stderr)
            die("в Codex есть незавершённые ходы — перезагрузка MCP их оборвёт. Дождись или --force", 2)
    with SharedServer() as s:
        s.call("config/mcpServer/reload", None, timeout=120)
    print("MCP-серверы перечитаны")


def cmd_mode(a):
    mode = bridge_mode()
    own, alive = gui_on_own_server(), shared_alive()
    print(f"режим: {mode}\n  приложение Codex на своём сервере: {'да' if own else 'нет'}\n"
          f"  общий сервер ({SHARED_SOCK}): {'работает' if alive else 'не отвечает'}")
    if alive and own:
        print("  общий сервер поднят, но приложение к нему не подключено — "
              "перезапусти Codex через codex-shared-launch")


def cmd_open(a):
    open_in_app(a.thread)
    print(f"открыт в фоне: codex://threads/{a.thread}")


def cmd_archive(a):
    rc = 0
    with connect() as s:
        for tid in a.threads:
            try:
                s.call("thread/archive", {"threadId": tid})
            except RuntimeError as ex:
                rc = 1
                if "active writer" in str(ex):
                    print(f"не архивирован {tid}: тред подключён в приложении Codex — "
                          f"архивировать из приложения или после его перезапуска")
                else:
                    print(f"не архивирован {tid}: {ex}")
                continue
            journal({"event": "archive", "thread_id": tid})
            print(f"архивирован: {tid}")
    return rc


# ----------------------------------------------------------------- Claude transcripts

REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def claude_files():
    return [f for f in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"))]


def claude_meta(path):
    title, cwd, first = None, None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"custom-title"' in line or '"ai-title"' in line:
                try:
                    d = json.loads(line)
                    title = d.get("customTitle") or d.get("aiTitle") or title
                except Exception:
                    pass
            elif cwd is None or first is None:
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                cwd = cwd or d.get("cwd")
                if first is None and not d.get("isSidechain"):
                    c = d.get("message", {}).get("content")
                    txt = c if isinstance(c, str) else " ".join(
                        b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                    txt = REMINDER_RE.sub("", txt).strip()
                    if txt:
                        first = txt
    return title, cwd, first


def find_claude_session(sid):
    if os.path.isfile(sid):
        return sid
    hits = [f for f in claude_files() if os.path.basename(f).startswith(sid)]
    if not hits:
        die(f"чат Claude не найден: {sid}")
    return max(hits, key=os.path.getmtime)


def cmd_claude_sessions(a):
    files = sorted(claude_files(), key=os.path.getmtime, reverse=True)
    shown = 0
    for f in files:
        if shown >= a.limit:
            break
        title, cwd, first = claude_meta(f)
        hay = " ".join(x or "" for x in (title, cwd, first)).lower()
        if a.search and a.search.lower() not in hay:
            continue
        if a.cwd and (cwd or "") != os.path.abspath(os.path.expanduser(a.cwd)):
            continue
        sid = os.path.splitext(os.path.basename(f))[0]
        mt = datetime.fromtimestamp(os.path.getmtime(f)).strftime("%d.%m %H:%M")
        print(f"{sid}  {mt}  {(title or (first or '')[:60]).replace(chr(10), ' ')}  [{cwd or '?'}]")
        shown += 1


def cmd_claude_read(a):
    path = find_claude_session(a.session)
    msgs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") not in ("user", "assistant") or d.get("isSidechain"):
                continue
            c = d.get("message", {}).get("content")
            if isinstance(c, str):
                txt = c
            else:
                txt = "\n".join(b.get("text", "") for b in (c or [])
                                if isinstance(b, dict) and b.get("type") == "text")
            txt = REMINDER_RE.sub("", txt).strip()
            if not txt:
                continue
            who = "Пользователь" if d["type"] == "user" else "Claude"
            if msgs and msgs[-1][0] == who:
                msgs[-1] = (who, msgs[-1][1] + "\n\n" + txt)
            else:
                msgs.append((who, txt))
    if a.last:
        msgs = msgs[-a.last:]
    for who, txt in msgs:
        print(f"\n### {who}:\n{txt[:a.max_chars]}")


def cmd_links(a):
    by_session: dict[str, list] = {}
    for e in read_journal():
        if e.get("event") == "start":
            key = e.get("claude_session") or (f"codex:{e['from_thread']}" if e.get("from_thread") else "?")
            by_session.setdefault(key, []).append(e)
    for sid, evs in by_session.items():
        if a.claude_session and not sid.startswith(a.claude_session):
            continue
        print(f"Агент Codex {sid[6:]}:" if sid.startswith("codex:") else f"Чат Claude {sid}:")
        for e in evs:
            print(f"  {e['ts'][:16]}  {e['thread_id']}  {e.get('model')}·{e.get('effort')}  {e.get('title')}")


# --------------------------------------------------------------------------- CLI

def main():
    p = argparse.ArgumentParser(prog="codex-bridge", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def prompt_args(sp):
        sp.add_argument("--prompt", help="текст задачи")
        sp.add_argument("--prompt-file", help="файл с текстом задачи")
        sp.add_argument("--claude-session", default="auto",
                        help="id чата Claude для ссылки в сообщении (auto | none | <id>)")
        sp.add_argument("--wait", action="store_true", help="дождаться ответа")
        sp.add_argument("--timeout", type=float, default=4 * 3600)
        sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("start", help="новый тред-задача в Codex")
    sp.add_argument("--title", required=True)
    sp.add_argument("--cwd", required=True, help="папка проекта")
    sp.add_argument("--model", default=DEFAULT_MODEL, help="terra | sol | astra | полный slug")
    sp.add_argument("--effort", default=DEFAULT_EFFORT,
                    help="medium (med) | high (hard) | xhigh (extra) | max | ultra")
    sp.add_argument("--astra-approved", action="store_true",
                    help="пользователь явно разрешил Astra для этой задачи")
    sp.add_argument("--sandbox", default="danger-full-access",
                    choices=["read-only", "workspace-write", "danger-full-access"])
    sp.add_argument("--approval", default="never", choices=["untrusted", "on-request", "never"])
    sp.add_argument("--no-open", action="store_true",
                    help="режим gui: не открывать тред в приложении")
    sp.add_argument("--open", action="store_true",
                    help="режим shared: всё-таки переключить окно Codex на новый тред")
    prompt_args(sp)
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("consult", help="консилиум: второе мнение / критика / параллельное исследование")
    sp.add_argument("--kind", choices=list(CONSULT_KINDS), default="opinion")
    sp.add_argument("--title", required=True, help="короткое название вопроса")
    sp.add_argument("--cwd", default=os.getcwd(), help="папка, где лежат материалы")
    sp.add_argument("--n", type=int, default=1, help="сколько независимых тредов запустить")
    sp.add_argument("--model", help="обязательно: terra | sol | astra (с --astra-approved)")
    sp.add_argument("--effort", help="обязательно: medium (med) | high (hard) | xhigh (extra) | max | ultra")
    sp.add_argument("--astra-approved", action="store_true",
                    help="пользователь явно разрешил Astra для этой задачи")
    sp.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"],
                    help="по умолчанию read-only для opinion/critique, полный доступ для research")
    prompt_args(sp)
    sp.set_defaults(fn=cmd_consult)

    sp = sub.add_parser("mode", help="в каком режиме работает мост (shared | gui)")
    sp.set_defaults(fn=cmd_mode)

    sp = sub.add_parser("reload-mcp", help="перечитать MCP-серверы на общем сервере (если ничего не идёт)")
    sp.add_argument("--force", action="store_true", help="даже если идут ходы (оборвёт их)")
    sp.set_defaults(fn=cmd_reload_mcp)

    sp = sub.add_parser("running", help="треды Codex, в которых сейчас идёт ход")
    sp.add_argument("--hours", type=float, default=6, help="смотреть треды, обновлённые за N часов")
    sp.set_defaults(fn=cmd_running)

    sp = sub.add_parser("send", help="следующее сообщение в существующий тред")
    sp.add_argument("thread")
    prompt_args(sp)
    sp.set_defaults(fn=cmd_send)

    sp = sub.add_parser("wait", help="дождаться ответа в одном или нескольких тредах")
    sp.add_argument("threads", nargs="+")
    sp.add_argument("--timeout", type=float, default=4 * 3600)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_wait)

    sp = sub.add_parser("read", help="переписка треда Codex")
    sp.add_argument("thread")
    sp.add_argument("--last", type=int, default=0, help="только последние N ходов")
    sp.add_argument("--items", action="store_true", help="показать команды и правки файлов")
    sp.add_argument("--max-chars", type=int, default=20000)
    sp.set_defaults(fn=cmd_read)

    sp = sub.add_parser("status", help="задачи, поставленные через мост")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("list", help="треды Codex")
    sp.add_argument("--search")
    sp.add_argument("--cwd")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("open", help="открыть тред в приложении Codex в фоне")
    sp.add_argument("thread")
    sp.set_defaults(fn=cmd_open)

    sp = sub.add_parser("archive", help="архивировать треды")
    sp.add_argument("threads", nargs="+")
    sp.set_defaults(fn=cmd_archive)

    sp = sub.add_parser("claude-sessions", help="чаты Claude Code")
    sp.add_argument("--search")
    sp.add_argument("--cwd")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(fn=cmd_claude_sessions)

    sp = sub.add_parser("claude-read", help="переписка чата Claude")
    sp.add_argument("session", help="id (можно префикс) или путь к .jsonl")
    sp.add_argument("--last", type=int, default=0, help="только последние N сообщений")
    sp.add_argument("--max-chars", type=int, default=8000)
    sp.set_defaults(fn=cmd_claude_read)

    sp = sub.add_parser("links", help="какие треды Codex породил какой чат Claude")
    sp.add_argument("--claude-session")
    sp.set_defaults(fn=cmd_links)

    a = p.parse_args()
    rc = a.fn(a)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
