"use strict";

const { app, BrowserWindow, dialog, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

const isDev = !app.isPackaged;
const repoRoot = path.resolve(__dirname, "..");

// Linux 便携包不保证 chrome-sandbox 的 setuid 位，统一关闭 Chromium 沙箱；
// 后端的 Bash 工具沙箱由后端自行探测（缺 bwrap 时降级），与此无关。
if (process.platform === "linux") {
  app.commandLine.appendSwitch("no-sandbox");
}

let backendProcess = null;
let backendLog = null;
let mainWindow = null;
let quitting = false;

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
  app.whenReady().then(start).catch(reportFatal);
}

function resolvePaths() {
  const resourcesRoot = isDev ? repoRoot : process.resourcesPath;
  const appDir = isDev ? repoRoot : path.join(resourcesRoot, "app");
  const pythonDir = path.join(resourcesRoot, "python");
  const pythonBin =
    process.platform === "win32" ? path.join(pythonDir, "python.exe") : path.join(pythonDir, "bin", "python3");
  return { resourcesRoot, appDir, pythonDir, pythonBin };
}

function pickPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function buildBackendEnv(dataDir, paths) {
  const env = { ...process.env };
  delete env.PYTHONHOME;
  env.ARCREEL_DATA_DIR = dataDir;
  env.ARCREEL_PROFILE_DIR = path.join(paths.appDir, "agent_runtime_profile");
  env.CLAUDE_CONFIG_DIR = path.join(dataDir, "claude");
  env.AUTH_ENABLED = "false";
  env.PYTHONUNBUFFERED = "1";
  env.PYTHONNOUSERSITE = "1";
  env.PYTHONPATH = ["arcreel-market-core", "arcreel-catalog-core"]
    .map((name) => path.join(paths.appDir, "packages", name, "src"))
    .join(path.delimiter);
  // 让 Agent 技能脚本里的 `python` 解析到随包运行时。
  env.PATH = `${paths.pythonDir}${path.delimiter}${process.env.PATH || ""}`;
  return env;
}

function spawnBackend(port, paths, dataDir) {
  const env = buildBackendEnv(dataDir, paths);
  const args = ["-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", String(port)];

  if (isDev) {
    return spawn(process.env.ARCREEL_UV || "uv", ["run", "python", ...args], {
      cwd: repoRoot,
      env,
      windowsHide: true,
    });
  }
  if (!fs.existsSync(paths.pythonBin)) {
    throw new Error(`随包 Python 运行时缺失: ${paths.pythonBin}`);
  }
  return spawn(paths.pythonBin, args, {
    cwd: paths.appDir,
    env,
    windowsHide: true,
  });
}

function waitForHealth(port, timeoutSeconds = 120) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      if (backendProcess && backendProcess.exitCode !== null) {
        reject(new Error(`后端进程已退出（退出码 ${backendProcess.exitCode}）`));
        return;
      }
      if (Date.now() > deadline) {
        reject(new Error(`等待后端就绪超时（${timeoutSeconds}s）`));
        return;
      }
      const request = http.get({ host: "127.0.0.1", port, path: "/health", timeout: 2000 }, (response) => {
        response.resume();
        if (response.statusCode === 200) {
          resolve();
          return;
        }
        setTimeout(attempt, 500);
      });
      request.on("timeout", () => {
        request.destroy();
        setTimeout(attempt, 500);
      });
      request.on("error", () => setTimeout(attempt, 500));
    };
    attempt();
  });
}

function createWindow(url) {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    show: false,
    backgroundColor: "#0b0b0c",
    title: "ArcReel",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    shell.openExternal(target);
    return { action: "deny" };
  });
  mainWindow.loadURL(url);
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

async function start() {
  const paths = resolvePaths();
  const dataDir = path.join(app.getPath("userData"), "data");
  fs.mkdirSync(dataDir, { recursive: true });

  const logPath = path.join(app.getPath("userData"), "backend.log");
  backendLog = fs.createWriteStream(logPath, { flags: "a" });
  backendLog.write(`\n===== ${new Date().toISOString()} 启动后端 =====\n`);

  const port = await pickPort();
  backendProcess = spawnBackend(port, paths, dataDir);
  backendProcess.stdout.pipe(backendLog);
  backendProcess.stderr.pipe(backendLog);

  const url = process.env.ARCREEL_DEV_URL || `http://127.0.0.1:${port}`;
  try {
    await waitForHealth(port);
  } catch (error) {
    await reportBackendFailure(error, logPath);
    return;
  }
  createWindow(url);
}

async function reportBackendFailure(error, logPath) {
  let tail = "";
  try {
    tail = fs.readFileSync(logPath, "utf8").slice(-4000);
  } catch {
    tail = "（无法读取后端日志）";
  }
  dialog.showErrorBox("ArcReel 启动失败", `${error.message}\n\n日志: ${logPath}\n\n${tail}`);
  stopBackend();
  app.quit();
}

function reportFatal(error) {
  dialog.showErrorBox("ArcReel 启动失败", String(error && error.stack ? error.stack : error));
  stopBackend();
  app.quit();
}

function stopBackend() {
  if (!backendProcess || backendProcess.exitCode !== null) {
    return;
  }
  const child = backendProcess;
  backendProcess = null;
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true });
  } else {
    child.kill("SIGTERM");
  }
  if (backendLog) {
    backendLog.end();
    backendLog = null;
  }
}

app.on("before-quit", () => {
  quitting = true;
  stopBackend();
});

app.on("window-all-closed", () => {
  app.quit();
});

process.on("exit", () => {
  if (!quitting) stopBackend();
});
