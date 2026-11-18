#!/usr/bin/env python3
"""启动便携目录中的后端，验证 API 与 SPA 都能正常响应。

用与 Electron 相同的启动方式（``python -m uvicorn server.app:app``，cwd 为便携目录），
因此能同时覆盖依赖安装、PROJECT_ROOT 资源布局与前端静态挂载三类问题。
"""

from __future__ import annotations

import argparse
import http.client
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT_SECONDS = 180.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def wait_ready(process: subprocess.Popen[str], port: int, log: Path) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"后端提前退出，退出码 {process.returncode}", file=sys.stderr)
            return False
        try:
            status, _ = request(port, "/health")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.5)
    print(f"等待 /health 超时（{STARTUP_TIMEOUT_SECONDS}s）：{log}", file=sys.stderr)
    return False


def stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if sys.platform.startswith("win"):
        subprocess.run(
            ["taskkill", "/pid", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        type=Path,
        default=ROOT / "desktop" / "build" / "python" / ("python.exe" if sys.platform.startswith("win") else "bin/python3"),
    )
    parser.add_argument("--app", type=Path, default=ROOT / "desktop" / "build" / "app")
    parser.add_argument("--log", type=Path, default=ROOT / "desktop" / "build" / "backend-smoke.log")
    args = parser.parse_args()

    interpreter = args.python.resolve()
    app_dir = args.app.resolve()
    if not interpreter.is_file():
        print(f"找不到 Python 运行时: {interpreter}（先运行 fetch_python_runtime.py）", file=sys.stderr)
        return 1
    if not (app_dir / "server" / "app.py").is_file():
        print(f"找不到后端源码: {app_dir}（先运行 stage_app.py）", file=sys.stderr)
        return 1

    port = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="arcreel-smoke-"))
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.update(
        {
            "ARCREEL_DATA_DIR": str(data_dir),
            "ARCREEL_PROFILE_DIR": str(app_dir / "agent_runtime_profile"),
            "AUTH_ENABLED": "false",
            "CLAUDE_CONFIG_DIR": str(data_dir / "claude"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": os.pathsep.join(
                str(app_dir / "packages" / name / "src")
                for name in ("arcreel-market-core", "arcreel-catalog-core")
            ),
        }
    )

    args.log.parent.mkdir(parents=True, exist_ok=True)
    print(f"启动后端: {interpreter} -m uvicorn server.app:app --port {port}（cwd={app_dir}）")
    with args.log.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [str(interpreter), "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=app_dir,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            if not wait_ready(process, port, args.log):
                return 1

            checks = {
                "/health": b'"status":"ok"',
                "/": b'id="app-root"',
                "/app/": b'id="app-root"',
            }
            for path, marker in checks.items():
                status, body = request(port, path)
                if status != 200 or marker not in body:
                    print(f"检查失败 {path}: status={status} marker_missing={marker not in body}", file=sys.stderr)
                    return 1

            status, _ = request(port, "/api/v1/tasks")
            if status != 200:
                print(f"检查失败 /api/v1/tasks: status={status}", file=sys.stderr)
                return 1
            print("后端 smoke 通过：/health、SPA 与 API 均正常")
            return 0
        finally:
            stop(process)
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
