"""Spike S1d: import-time side effects of Reflex and the UI packages (Task P0-05.3, Analyze A-16).

Usage: python -I import_audit.py [--block]
Installs a ``sys.addaudithook`` that records socket creation/connect/bind, file opens in write
modes, mkdir/rename/remove and subprocess spawns while importing reflex, reflex_silverpoint_react,
reflex_mermaidjs and the spike page module. With ``--block`` the operations raise instead, to see
whether the imports survive a sandbox that forbids them (what a REQ-190.c test would do).
Prints JSON: each operation with its first non-stdlib origin frame.
"""

from __future__ import annotations

import json
import os
import sys
import sysconfig
import traceback

BLOCK = "--block" in sys.argv
STDLIB = sysconfig.get_paths()["stdlib"]
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
events: list[dict] = []
_in_hook = False


class Blocked(PermissionError):
    pass


def origin() -> str:
    for fr in reversed(traceback.extract_stack()[:-3]):
        if not fr.filename.startswith(STDLIB) and "import_audit" not in fr.filename:
            short = fr.filename.split("site-packages/")[-1]
            return f"{short}:{fr.lineno} ({fr.name})"
    return "<stdlib>"


def is_write(ev: str, args: tuple) -> bool:
    if ev == "open":
        _, mode, flags = args
        if isinstance(mode, str):
            return any(c in mode for c in "wax+")
        return bool((flags or 0) & WRITE_FLAGS)
    return ev in {
        "os.mkdir",
        "os.rename",
        "os.remove",
        "os.rmdir",
        "os.symlink",
        "os.truncate",
        "shutil.copyfile",
        "shutil.rmtree",
        "shutil.move",
        "os.chmod",
    }


def hook(ev: str, args: tuple) -> None:
    global _in_hook
    if _in_hook:
        return
    kind = None
    if ev.startswith("socket.") and ev in {
        "socket.__new__",
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "socket.sendto",
    }:
        kind = "network"
    elif ev in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"}:
        kind = "process"
    elif is_write(ev, args):
        kind = "fs_write"
    if kind is None:
        return
    _in_hook = True
    try:
        target = str(args[0])[:160] if args else ""
        if ev.startswith("socket.") and len(args) > 1:
            target = str(args[1])[:160]
        events.append({"kind": kind, "event": ev, "target": target, "origin": origin()})
    finally:
        _in_hook = False
    if BLOCK:
        raise Blocked(f"{ev} blocked by audit hook")


sys.addaudithook(hook)

results = {}
for mod in (
    "reflex",
    "reflex_silverpoint_react",
    "reflex_mermaidjs",
    "reflex_agentlens._webapp.app",
):
    before = len(events)
    try:
        __import__(mod)
        results[mod] = {"ok": True}
    except Exception as e:  # noqa: BLE001 - we want every failure mode recorded
        results[mod] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    results[mod]["ops"] = events[before:]

print(json.dumps({"block": BLOCK, "cwd": os.getcwd(), "results": results}, indent=2))
