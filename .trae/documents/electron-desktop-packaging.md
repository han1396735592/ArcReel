# Electron 桌面版与 GitHub Actions 自动打包

## Context

ArcReel 目前以 Docker 方式部署（FastAPI + 前端静态产物，端口 1241）。用户希望提供桌面安装包，并用 GitHub Actions 自动构建。已确认的决策：

- **内嵌 Python 后端**：PyInstaller 冻结 `server.app:app`，随 Electron 安装包分发，Electron 启动后端子进程并加载 `http://127.0.0.1:<port>`。
- **三平台全量**：Windows x64、macOS arm64 + x64、Linux x64，共 4 个构建任务。
- **触发方式**：`v*` tag 推送 + `workflow_dispatch`。
- **随包 Python 运行时**：供 Agent 技能脚本（`.claude/skills/*/scripts/*.py`，stdlib-only）在 Bash 沙箱里调用。

关键事实（已核实）：

- 后端 SPA 服务取 `PROJECT_ROOT / "frontend" / "dist"`（[app.py](file:///workspace/server/app.py#L970-L993)）；`PROJECT_ROOT` 由 `lib/` 的 `__file__` 推导（[env_init.py](file:///workspace/lib/infra/env_init.py#L26-L47)），frozen 后指向 PyInstaller 的 `_internal`。
- Alembic `script_location` 取 `Path(__file__).parents[2] / "alembic"`，不读 `alembic.ini`（[lib/db/__init__.py](file:///workspace/lib/db/__init__.py#L46-L56)）；版本号读 `PROJECT_ROOT/pyproject.toml`（[app_version.py](file:///workspace/lib/app_version.py#L9)）。
- 数据根、SQLite、`logs/` 全部由 `ARCREEL_DATA_DIR` 派生（[app_data_dir.py](file:///workspace/lib/infra/app_data_dir.py#L25-L42)、[data_root_layout.py](file:///workspace/lib/infra/data_root_layout.py#L49-L51)、[logging_config.py](file:///workspace/lib/infra/logging_config.py#L24-L26)）。
- `AUTH_ENABLED=false` 时跳过密码生成与 `.env` 回写（[auth.py](file:///workspace/server/auth.py#L56-L66)、[auth.py](file:///workspace/server/auth.py#L226-L245)）。
- `/health` 返回 200（[app.py](file:///workspace/server/app.py#L837-L840)），可作为就绪探针。
- 随包 ffmpeg 经 `importlib.resources` 从 `imageio_ffmpeg/binaries/` 读取（[ffmpeg.py](file:///workspace/lib/infra/ffmpeg.py#L54-L80)）。

## 架构

```
Electron main (Node)
├─ 选空闲端口，设置环境变量（数据根/日志/认证/PATH 含随包 Python）
├─ spawn resources/backend/arcreel-backend(.exe)  ← PyInstaller onedir
│    └─ uvicorn server.app:app (127.0.0.1:<port>)
│         └─ 同源提供 SPA（frontend/dist）+ /api + /health
└─ 轮询 /health 就绪 → BrowserWindow.loadURL(http://127.0.0.1:<port>)
```

同源加载避免 `file://` 下相对路径 `/api/v1` 失效，也不需要额外静态代理。

## 文件清单

### 新增 `desktop/`

1. **`desktop/package.json`**
   - `"private": true`、无 `"type": "module"`（main 用 CommonJS）；`electron`、`electron-builder` 固定版本。
   - scripts：`dev`（`electron .`）、`build`（`electron-builder --config electron-builder.yml`）。
   - `productName: ArcReel`，`version` 由 release-please 维护。
2. **`desktop/main.js`**（~150 行）
   - 单实例锁；`app.commandLine.appendSwitch("no-sandbox")` 仅当 `process.env.APPIMAGE`。
   - 端口：`net.createServer().listen(0, "127.0.0.1")` 取空闲端口。
   - 子进程环境：`ARCREEL_DATA_DIR=<userData>/data`、`ARCREEL_PROFILE_DIR=<resources>/agent_runtime_profile`、`AUTH_ENABLED=false`、`CLAUDE_CONFIG_DIR=<data>/claude`、`PATH=<resources>/python/bin:<resources>/python（Windows）:${PATH}`、`cwd=<userData>`。
   - 打包态 spawn `process.resourcesPath/backend/arcreel-backend[.exe]`；开发态 spawn `uv run uvicorn server.app:app --host 127.0.0.1 --port <port>`（cwd 为仓库根）。
   - 后端 stdout/stderr 写 `<userData>/backend.log`；子进程提前退出时弹 dialog 并附日志尾部。
   - 每 500ms 轮询 `/health`，最长 120s；就绪后 `loadURL`。
   - `before-quit`：Unix `child.kill()`，Windows `taskkill /pid <pid> /T /F`。
   - `webPreferences`：`contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`。
3. **`desktop/preload.js`**：仅 `contextBridge.exposeInMainWorld("arcreel", { version })`。
4. **`desktop/electron-builder.yml`**
   - `appId: com.arcreel.desktop`、`productName: ArcReel`、`directories.output: dist`、`files: [main.js, preload.js]`、`asar: true`。
   - `extraResources`：`build/backend/arcreel-backend → backend`、`build/python → python`。
   - `win.target: nsis`（per-user，免 UAC）；`mac.target: dmg`、`mac.identity: "-"`（ad-hoc 签名，arm64 必需）；`linux.target: [AppImage, deb]`、`category: AudioVideo`。
   - `publish: null`（Release 资产由 workflow 用 `gh` 上传，避免与 release-please 抢建 Release）。
5. **`desktop/backend_entry.py`**
   - `argparse --host 127.0.0.1 --port`；`multiprocessing.freeze_support()`。
   - frozen 下断言 `_internal/frontend/dist/index.html`、`_internal/alembic/versions`、`_internal/agent_runtime_profile`、`_internal/pyproject.toml` 存在，缺失即非零退出并打印明确原因。
   - `uvicorn.run("server.app:app", host=..., port=..., log_level="info")`（用导入字符串，确保 env 先落地）。
6. **`desktop/arcreel-backend.spec`**
   - `onedir`（onefile 每次启动解压 ~120MB CLI，不可接受）。
   - `ROOT = Path(SPECPATH).parent.parent`；`datas`：`frontend/dist → frontend/dist`、`agent_runtime_profile → agent_runtime_profile`、`public → public`、`alembic → alembic`、`pyproject.toml → .`。
   - `collect_all("claude_agent_sdk")` **只调用一次**（wheel 内含 ~120MB CLI），返回值按 `(dest, src)` 去重后合并；`imageio_ffmpeg` 的 `binaries/ffmpeg-*` 放进 `binaries` 以保留可执行位；`grpcio`、`lxml`、`pdf_oxide`、`pyjianyingdraft`、`charset_normalizer` 各 `collect_all` 一次。
   - `hiddenimports`：`collect_submodules("lib")`、`collect_submodules("server")`、`collect_submodules("arcreel_market_core")`、`collect_submodules("arcreel_catalog_core")`，外加 `uvicorn.logging`、`uvicorn.loops.auto/asyncio`、`uvicorn.protocols.http.auto/h11_impl/httptools_impl`、`uvicorn.protocols.websockets.auto/wsproto_impl`、`uvicorn.lifespan.on`、`sqlalchemy.dialects.sqlite.aiosqlite`、`sqlalchemy.dialects.postgresql.asyncpg`、`alembic.runtime.migration`、`aiosqlite`、`asyncpg`。
   - `pathex` 加 `packages/*/src`（uv workspace 子包）；`excludes = ["tests", "pytest", "_pytest", "tkinter", "matplotlib"]`。
7. **`desktop/scripts/smoke_backend.py`**：以 127.0.0.1 随机端口启动打包后的后端，轮询 `/health`，再断言 `/`（SPA）与 `/api/v1/system/version` 返回 200，最后终止进程。
8. **`desktop/scripts/fetch_python_runtime.py`**：按 `sys.platform`/`platform.machine()` 映射 python-build-standalone `install_only` 包，`urllib` 下载 + `tarfile` 解压到 `desktop/build/python`（Unix 保留可执行位）。
9. **`desktop/pnpm-lock.yaml`**：本地 `pnpm install` 生成后提交，CI 用 `--frozen-lockfile`。

### 修改

10. **`.github/workflows/electron-release.yml`**（新增）
    - `on: push.tags: ["v*"]` + `workflow_dispatch`；`permissions: contents: read`；`concurrency: electron-release-${{ github.ref }}`，`cancel-in-progress: false`；actions 全部复用仓库已有完整 SHA（checkout、pnpm、setup-node、setup-uv、setup-python、upload/download-artifact）。
    - `build` job，matrix：
      | os | 产物 |
      |---|---|
      | `windows-latest` | `*.exe` (nsis) |
      | `macos-14` | `*.dmg` (arm64) |
      | `macos-13` | `*.dmg` (x64) |
      | `ubuntu-22.04` | `*.AppImage`、`*.deb` |
    - 步骤：checkout → pnpm + node 24（cache `frontend/pnpm-lock.yaml`）→ `pnpm --dir frontend install --frozen-lockfile && pnpm --dir frontend build` → setup-uv + python 3.12 → `uv sync --frozen --no-dev` → `uv pip install --python .venv "pyinstaller>=6.16,<7"` → `uv run pyinstaller desktop/arcreel-backend.spec --noconfirm` → `python desktop/scripts/fetch_python_runtime.py` → `python desktop/scripts/smoke_backend.py` → `pnpm --dir desktop install --frozen-lockfile` → `pnpm --dir desktop exec electron-builder --config electron-builder.yml -c.extraMetadata.version=$VERSION --publish never` → 断言 `desktop/dist` 产物 → `upload-artifact`。
    - `VERSION`：tag 触发取 `${GITHUB_REF_NAME#v}`；手动触发取 `desktop/package.json` 的 version。
    - 缓存 `~/.cache/electron*`、`~/Library/Caches/electron*`、`%LOCALAPPDATA%\electron*`（key 含 OS + electron 版本）。
    - `publish` job（`if: startsWith(github.ref, 'refs/tags/v')`，`contents: write`）：下载并合并 artifact → 重试 `gh release view "$TAG"`（约 5 分钟）→ 缺失则 `gh release create --verify-tag` → `gh release upload "$TAG" <产物> --clobber`。用 `gh` 而非新 action，减少需要 pin 的第三方 action。
11. **`.release-please-config.json`**：`extra-files` 增加 `desktop/package.json` 的 `$.version`，让桌面版版本跟随后端版本。
12. **`.gitignore`**：`desktop/node_modules/`、`desktop/build/`、`desktop/dist/`。

## 实施顺序

1. `desktop/backend_entry.py` + `arcreel-backend.spec` → 本地 PyInstaller 打包 + `smoke_backend.py` 通过。
2. `desktop/package.json` + `main.js` + `preload.js` + `electron-builder.yml` → 本地 `pnpm dev`（走 uv）与 `pnpm build` 出 Linux 包。
3. `fetch_python_runtime.py` + Python 运行时 PATH 注入验证。
4. `.github/workflows/electron-release.yml` + release-please/gitignore 改动 → actionlint / zizmor 校验。
5. 推 tag 干跑；失败可回退为「仅上传 artifact、不挂 Release」。

## 验证

- **后端冻结**：`uv run pyinstaller desktop/arcreel-backend.spec`；`python desktop/scripts/smoke_backend.py --binary desktop/build/backend-dist/arcreel-backend/arcreel-backend` 断言 `/health`、`/`、版本接口均 200。
- **Electron**：`cd desktop && pnpm dev`（开发态）；`pnpm build` 后运行 `dist/linux-unpacked/arcreel --no-sandbox`，确认窗口加载、首次启动建库、退出后子进程被回收。
- **CI 静态检查**：`uvx zizmor@1.29.0 --offline .github/workflows/electron-release.yml`；`actionlint`（pre-commit 同款镜像）。
- **发布链路**：`workflow_dispatch` 全绿后检查 artifact；推 `v*` tag 后确认 Release 中 4 类安装包齐全。

## 风险与回退

- **PyInstaller 收集不全**（grpc / pdf-oxide / alembic / SDK CLI）：以 `smoke_backend.py` 为闸门，缺什么补 `collect_all`；frozen 后 SDK CLI 若按 `__file__` 相对定位失败，就在 `backend_entry` 中显式设置 `CLAUDE_CODE_PATH` 指向 `_internal` 内二进制。
- **产物体积**：预计 400–700MB/平台；CI 断言 `_internal` 内 >20MB 的 `claude*` 文件恰好 1 个，防止重复打包。
- **macOS 未签名**：ad-hoc 签名可运行，但 Gatekeeper 仍提示；后续接 Apple Developer ID 时在 workflow 注入证书 secrets 并补 entitlements（`allow-jit`、`disable-library-validation`）。Windows 未签名会有 SmartScreen 提示。
- **Linux 沙箱**：无 `bwrap`/`socat` 时 Agent Bash 工具降级（后端只告警不崩溃）；AppImage 内需 `--no-sandbox`（main.js 已处理）。
- **Release 竞态**：release-please 建 Release 与本 workflow 上传可能并发；`gh release view` 重试 + `--clobber` 兜底。
