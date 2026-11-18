#!/usr/bin/env python3
"""把后端源码与前端构建产物铺进便携目录，供 Electron 随包分发。

产物布局（electron-builder 的 extraResources 目标 ``resources/app``）::

    app/
      lib/ server/ alembic/ packages/ public/ agent_runtime_profile/
      frontend/dist/          # SPA 构建产物，后端的 PROJECT_ROOT/frontend/dist
      alembic.ini pyproject.toml
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SOURCE_DIRS = (
    "lib",
    "server",
    "alembic",
    "packages",
    "public",
    "agent_runtime_profile",
)

SOURCE_FILES = (
    "alembic.ini",
    "pyproject.toml",
    "LICENSE",
    "NOTICE",
)

IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "node_modules",
    ".venv",
    "*.egg-info",
    ".pytest_cache",
)


def copy_dir(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"缺少目录: {source}")
    shutil.copytree(source, target, ignore=IGNORE)


def dir_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=ROOT / "desktop" / "build" / "app")
    args = parser.parse_args()

    dest: Path = args.dest.resolve()
    frontend_dist = ROOT / "frontend" / "dist"

    if not (frontend_dist / "index.html").is_file():
        print(f"缺少前端构建产物: {frontend_dist}/index.html（先运行 pnpm --dir frontend build）", file=sys.stderr)
        return 1

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for name in SOURCE_DIRS:
        copy_dir(ROOT / name, dest / name)
    for name in SOURCE_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, dest / name)

    copy_dir(frontend_dist, dest / "frontend" / "dist")

    required = (
        dest / "server" / "app.py",
        dest / "lib" / "infra" / "env_init.py",
        dest / "alembic" / "versions",
        dest / "frontend" / "dist" / "index.html",
        dest / "agent_runtime_profile",
        dest / "pyproject.toml",
    )
    missing = [str(item.relative_to(dest)) for item in required if not item.exists()]
    if missing:
        print(f"便携目录缺少必需内容: {', '.join(missing)}", file=sys.stderr)
        return 1

    size_mb = dir_size(dest) / (1024 * 1024)
    print(f"源码已铺好: {dest}（{size_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
