#!/usr/bin/env python3
"""MCP-сервер (stdio) с инструментом `load_workspace_dependencies`.

В общем режиме у агентов Codex нет инструментов приложения (`codex_app`), а скиллы
документов, таблиц, слайдов и PDF первым шагом вызывают `load_workspace_dependencies`,
чтобы найти встроенный набор Codex (Node, Python, LibreOffice, poppler, git).
Этот сервер отдаёт те же пути в том же формате, читая их из
~/.cache/codex-runtimes/codex-primary-runtime. Только стандартная библиотека.

Подключение: codex mcp add codex_runtime -- <путь>/runtime_mcp.py
"""
from __future__ import annotations

import json
import os
import sys

RUNTIME = os.path.expanduser(os.environ.get(
    "CODEX_PRIMARY_RUNTIME", "~/.cache/codex-runtimes/codex-primary-runtime"))
TOOL = {
    "name": "load_workspace_dependencies",
    "description": ("Find the bundled Codex workspace runtime (Node.js, Python, LibreOffice, poppler, git) "
                    "and libraries for sheets, slides, documents, PDFs and images. Takes no arguments."),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _first(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def instructions() -> tuple[bool, str]:
    dep = os.path.join(RUNTIME, "dependencies")
    node = _first(os.path.join(dep, "node", "bin", "node"))
    node_modules = _first(os.path.join(dep, "node", "node_modules"))
    python = _first(os.path.join(dep, "python", "bin", "python3"))
    if not (node and node_modules and python):
        return False, f"No bundled workspace runtime found in {RUNTIME}."
    fallback = os.path.join(dep, "bin", "fallback")
    override = os.path.join(dep, "bin", "override")
    pnpm = _first(os.path.join(fallback, "pnpm"))
    git = _first(os.path.join(fallback, "git"), os.path.join(dep, "bin", "git"))
    py_bin = os.path.dirname(python)
    py_libs = os.path.dirname(py_bin) if os.path.basename(py_bin) == "bin" else py_bin
    version = None
    try:
        with open(os.path.join(RUNTIME, "runtime.json"), encoding="utf-8") as f:
            version = json.load(f).get("bundleVersion")
    except Exception:
        pass
    lines = ["### Workspace Dependencies",
             "Use these bundled paths for sheets, slides, documents, PDFs, images, or browser automation:"]
    if version:
        lines.append(f"- Bundle version: {version}")
    if git:
        lines.append(f"- Git executable: {git}")
    lines += [f"- Node.js executable: {node}", f"- Node.js packages: {node_modules}"]
    if pnpm:
        lines.append(f"- pnpm executable: {pnpm}")
    lines += [f"- Python executable: {python}", f"- Python packages: {py_libs}",
              f"- Override binaries: {override}", f"- Fallback binaries: {fallback}"]
    return True, ("Workspace dependencies are available for this local desktop thread.\n\n"
                  + "\n".join(lines))


def respond(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:          # уведомления (notifications/initialized и т.п.)
            continue
        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            respond(msg_id, {"protocolVersion": version, "capabilities": {"tools": {}},
                             "serverInfo": {"name": "codex_runtime", "version": "1.0.0"}})
        elif method == "tools/list":
            respond(msg_id, {"tools": [TOOL]})
        elif method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            if name != TOOL["name"]:
                respond(msg_id, error={"code": -32602, "message": f"unknown tool: {name}"})
                continue
            ok, text = instructions()
            respond(msg_id, {"content": [{"type": "text", "text": text}], "isError": not ok})
        elif method == "ping":
            respond(msg_id, {})
        else:
            respond(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
