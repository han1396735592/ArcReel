#!/usr/bin/env python3
"""下载并解压 python-build-standalone 运行时，供桌面便携版随包分发。

产物布局（electron-builder 的 extraResources 目标 ``resources/python``）::

    python/
      bin/python3        # Windows 为 python.exe
      lib/...
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 固定版本保证构建可复现；升级时同步改这两项（见 python-build-standalone releases）。
STANDALONE_TAG = "20261110"
PYTHON_VERSION = "3.12.16"

TRIPLES = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("win32", "AMD64"): "x86_64-pc-windows-msvc",
    ("win32", "ARM64"): "aarch64-pc-windows-msvc",
}


def asset_name() -> str:
    sys_platform = "win32" if sys.platform.startswith("win") else sys.platform
    machine = platform.machine()
    triple = TRIPLES.get((sys_platform, machine))
    if triple is None:
        raise SystemExit(f"不支持的平台: {sys_platform}/{machine}")
    return f"cpython-{PYTHON_VERSION}+{STANDALONE_TAG}-{triple}-install_only.tar.gz"


def download(url: str, target: Path) -> None:
    print(f"下载 {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "arcreel-desktop-build"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def extract(archive: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        extracted = Path(tmp) / "python"
        if not extracted.is_dir():
            raise SystemExit(f"压缩包结构异常，未找到 python/ 目录: {archive}")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(extracted), dest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=ROOT / "desktop" / "build" / "python",
        help="运行时解压目标目录（默认 desktop/build/python）",
    )
    parser.add_argument("--force", action="store_true", help="目标已存在时重新下载")
    args = parser.parse_args()

    dest: Path = args.dest.resolve()
    interpreter = dest / ("python.exe" if sys.platform.startswith("win") else "bin/python3")
    if interpreter.is_file() and not args.force:
        print(f"已存在运行时，跳过下载: {interpreter}")
    else:
        name = asset_name()
        url = (
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{STANDALONE_TAG}/{urllib.parse.quote(name)}"
        )
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / name
            download(url, archive)
            extract(archive, dest)

    result = subprocess.run([str(interpreter), "-V"], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode
    print(f"运行时就绪: {interpreter} ({result.stdout.strip()}) -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
