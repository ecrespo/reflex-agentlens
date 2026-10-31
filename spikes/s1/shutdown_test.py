"""Spike S1c: SIGTERM during an export burst (Task P0-04.2, REQ-331.a feasibility, DD-001).

Usage: python shutdown_test.py --python VENV_PY --data-dir DIR --port P --client-cmd "..."
Boots the app with boot.py, starts the OTLP client in load mode, sends SIGTERM to the server after
``--after`` seconds and checks: exit code, time to exit, HTTP and gRPC ports released, no child
process left. Prints JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def children(pid: int) -> list[str]:
    out = subprocess.run(
        ["ps", "-o", "pid=,args=", "--ppid", str(pid)], capture_output=True, text=True, check=False
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--port", type=int, default=9123)
    ap.add_argument("--grpc-port", type=int, default=4317)
    ap.add_argument("--client-cmd", required=True)
    ap.add_argument("--after", type=float, default=3.0)
    ap.add_argument("--log", default="shutdown-server.log")
    args = ap.parse_args()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.environ["HOME"],
        "AGENTLENS_SPIKE_GRPC_PORT": str(args.grpc_port),
    }
    log = open(args.log, "w")  # noqa: SIM115
    srv = subprocess.Popen(
        [args.python, str(HERE / "boot.py"), "--data-dir", args.data_dir, "--port", str(args.port)],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{args.port}"
    for _ in range(300):
        try:
            urllib.request.urlopen(url + "/ping", timeout=1)  # noqa: S310
            break
        except OSError:
            time.sleep(0.1)
    kids_before = children(srv.pid)
    client = subprocess.Popen(
        shlex.split(args.client_cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    time.sleep(args.after)
    counts_before = json.load(urllib.request.urlopen(url + "/_spike/counts", timeout=5))  # noqa: S310
    t0 = time.monotonic()
    srv.send_signal(signal.SIGTERM)
    try:
        code = srv.wait(timeout=15)
    except subprocess.TimeoutExpired:
        code = None
        os.killpg(srv.pid, signal.SIGKILL)
    exit_s = round(time.monotonic() - t0, 2)
    client.kill()
    client.wait()
    time.sleep(0.2)
    leftover = subprocess.run(
        ["pgrep", "-f", f"--port {args.port}"], capture_output=True, text=True, check=False
    ).stdout.split()
    log.close()
    report = {
        "exit_code": code,
        "exit_s": exit_s,
        "http_port_released": port_free(args.port),
        "grpc_port_released": port_free(args.grpc_port),
        "worker_processes_before": kids_before,
        "leftover_pids": leftover,
        "counts_at_sigterm": counts_before,
        "server_log_tail": Path(args.log).read_text().splitlines()[-12:],
    }
    print(json.dumps(report, indent=2))
    ok = code == 0 and exit_s <= 5 and report["http_port_released"] and report["grpc_port_released"]
    return 0 if ok and not leftover else 1


if __name__ == "__main__":
    raise SystemExit(main())
