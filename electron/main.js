const { app, BrowserWindow, dialog, shell, ipcMain, Menu } = require('electron')
let autoUpdater = null
try { autoUpdater = require('electron-updater').autoUpdater } catch (_) {}
const { spawn } = require('child_process')
const path = require('path')
const http = require('http')
const fs   = require('fs')

const { startContainers, stopContainers } = require('./dockerManager')

let mainWindow
let backendProcess
let loadingWindow

// ── Paths ─────────────────────────────────────────────────────────────────────

function resourcesDir() {
  return app.isPackaged ? process.resourcesPath : path.join(__dirname, '..')
}

function backendBinaryPath() {
  const dir = path.join(resourcesDir(), 'backend-bin', 'engramai-backend')
  if (process.platform === 'win32') return dir + '.exe'
  return dir
}

function composeDir() {
  return resourcesDir()
}

// ── Loading window ────────────────────────────────────────────────────────────

function showLoading(message) {
  if (!loadingWindow || loadingWindow.isDestroyed()) {
    loadingWindow = new BrowserWindow({
      width: 420,
      height: 160,
      frame: false,
      resizable: false,
      backgroundColor: '#1e1e1e',
      alwaysOnTop: true,
      skipTaskbar: true,
      webPreferences: { nodeIntegration: false },
    })
  }
  loadingWindow.loadURL(`data:text/html,<!DOCTYPE html>
    <html><head><meta charset="utf-8">
    <style>
      * { margin:0; padding:0; box-sizing:border-box; }
      body {
        background: #1e1e1e;
        color: #ccc;
        font-family: 'Segoe UI', system-ui, sans-serif;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        height: 100vh;
        gap: 14px;
        -webkit-app-region: drag;
      }
      .logo { font-size: 22px; font-weight: 700; color: #4ec9b0; letter-spacing: 2px; }
      .msg  { font-size: 13px; color: #858585; }
      .bar  { width: 220px; height: 3px; background: #333; border-radius: 2px; overflow: hidden; }
      .fill { height: 100%; background: #4ec9b0; border-radius: 2px; animation: slide 1.5s ease-in-out infinite; }
      @keyframes slide { 0%{width:0%} 50%{width:80%} 100%{width:100%} }
    </style></head>
    <body>
      <div class="logo">Code LM</div>
      <div class="msg" id="m">${message}</div>
      <div class="bar"><div class="fill"></div></div>
    </body></html>`)
}

function updateLoading(message) {
  if (loadingWindow && !loadingWindow.isDestroyed()) {
    loadingWindow.webContents.executeJavaScript(
      `document.getElementById('m').textContent = ${JSON.stringify(message)}`
    ).catch(() => {})
  }
}

function closeLoading() {
  if (loadingWindow && !loadingWindow.isDestroyed()) {
    loadingWindow.close()
    loadingWindow = null
  }
}

// ── Backend process ───────────────────────────────────────────────────────────

function startBackend(ports) {
  updateLoading('Starting Code LM backend…')

  if (!app.isPackaged) {
    // Dev mode: backend started manually by developer
    updateLoading('Waiting for backend on :8765…')
    return
  }

  const bin = backendBinaryPath()
  if (!fs.existsSync(bin)) {
    dialog.showErrorBox('Missing backend', `Backend binary not found:\n${bin}`)
    app.quit()
    return
  }

  // Pass dynamic ports to backend via environment variables
  const env = { ...process.env }
  if (ports) {
    env.ENGRAMAI_POSTGRES_PORT = String(ports.postgres)
    env.ENGRAMAI_NEO4J_URI     = `bolt://localhost:${ports.neo4jBolt}`
    env.ENGRAMAI_QDRANT_PORT   = String(ports.qdrant)
  }

  backendProcess = spawn(bin, [], { cwd: path.dirname(bin), env })
  backendProcess.stdout.on('data', d => console.log('[backend]', d.toString().trim()))
  backendProcess.stderr.on('data', d => console.error('[backend]', d.toString().trim()))
  backendProcess.on('exit', code => {
    console.log('[backend] exited with code', code)
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.executeJavaScript(
        `document.body.innerHTML += '<div style="position:fixed;bottom:12px;left:50%;transform:translateX(-50%);background:#f44747;color:#fff;padding:8px 16px;border-radius:4px;font-size:13px;">Backend stopped (code ${code})</div>'`
      ).catch(() => {})
    }
  })
}

// ── Wait for backend ──────────────────────────────────────────────────────────

function waitForBackend(retries = 40, interval = 500) {
  return new Promise((resolve, reject) => {
    let attempts = 0
    const check = () => {
      http.get('http://localhost:8765/health', res => {
        if (res.statusCode === 200) resolve()
        else retry()
      }).on('error', retry)
    }
    const retry = () => {
      if (++attempts >= retries) reject(new Error('Backend did not respond'))
      else setTimeout(check, interval)
    }
    check()
  })
}

// ── Main window ───────────────────────────────────────────────────────────────

function createMainWindow(url) {
  const win = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    backgroundColor: '#1e1e1e',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
    title: 'Code LM',
    show: false,
  })

  win.loadURL(url || 'http://localhost:8765')

  win.once('ready-to-show', () => {
    closeLoading()
    win.show()
    win.focus()
    if (app.isPackaged && autoUpdater) autoUpdater.checkForUpdatesAndNotify()
  })

  win.on('closed', () => {
    if (win === mainWindow) mainWindow = null
  })

  if (!mainWindow) mainWindow = win
  return win
}

// ── Auto updater ──────────────────────────────────────────────────────────────

if (autoUpdater) {
  autoUpdater.on('update-available', () => {
    dialog.showMessageBox({
      type: 'info', title: 'Update available',
      message: 'A new version of Code LM is downloading…', buttons: ['OK'],
    })
  })
  autoUpdater.on('update-downloaded', () => {
    dialog.showMessageBox({
      type: 'question', buttons: ['Restart now', 'Later'],
      title: 'Update ready', message: 'Code LM update downloaded. Restart to apply?',
    }).then(r => { if (r.response === 0) autoUpdater.quitAndInstall() })
  })
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }

// ── IPC handlers ─────────────────────────────────────────────────────────────

ipcMain.handle('open-folder', async () => {
  const result = await dialog.showOpenDialog({ properties: ['openDirectory'] })
  return result.canceled ? null : result.filePaths[0]
})

ipcMain.handle('open-in-new-window', async (_event, folderPath) => {
  const pid = Buffer.from(folderPath).toString('base64').replace(/[^a-zA-Z0-9]/g, '').slice(0, 32)
  const url = `http://localhost:8765?root=${encodeURIComponent(folderPath)}&pid=${pid}`
  createMainWindow(url)
})

// ── Application menu ─────────────────────────────────────────────────────────

function buildMenu() {
  const template = [
    {
      label: 'File',
      submenu: [
        {
          label: 'Open Project...',
          accelerator: 'CmdOrCtrl+O',
          click: async () => {
            const result = await dialog.showOpenDialog({ properties: ['openDirectory'] })
            if (!result.canceled && result.filePaths[0]) {
              const folderPath = result.filePaths[0]
              const pid = Buffer.from(folderPath).toString('base64').replace(/[^a-zA-Z0-9]/g, '').slice(0, 32)
              createMainWindow(`http://localhost:8765?root=${encodeURIComponent(folderPath)}&pid=${pid}`)
            }
          },
        },
        {
          label: 'New Window',
          accelerator: 'CmdOrCtrl+Shift+N',
          click: () => createMainWindow(),
        },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' }, { role: 'redo' }, { type: 'separator' },
        { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' }, { role: 'forceReload' }, { role: 'toggleDevTools' },
        { type: 'separator' }, { role: 'togglefullscreen' },
        { label: 'Exit Full Screen', accelerator: 'Escape', click: () => {
          if (mainWindow && mainWindow.isFullScreen()) mainWindow.setFullScreen(false)
        }},
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ── App lifecycle ─────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  buildMenu()
  showLoading('Initialising…')

  // ── Start Docker containers with dynamic ports ──────────────────────────
  let ports = null
  try {
    ports = await startContainers(composeDir(), updateLoading)

    if (ports === null) {
      // Docker not available
      const choice = await dialog.showMessageBox({
        type: 'warning',
        title: 'Docker required',
        message: 'Code LM needs Docker to run its databases.',
        detail: 'Install Docker Desktop, then restart Code LM.\n\nDocker Desktop: https://www.docker.com/products/docker-desktop',
        buttons: ['Open Docker website', 'Continue anyway', 'Quit'],
        defaultId: 0,
      })
      if (choice.response === 0) shell.openExternal('https://www.docker.com/products/docker-desktop')
      if (choice.response === 2) { app.quit(); return }
      // "Continue anyway" — proceed without Docker, backend will use defaults
    }
  } catch (e) {
    console.error('[main] Docker startup failed:', e.message)
    await dialog.showMessageBox({
      type: 'warning',
      title: 'Database startup failed',
      message: 'Could not start databases automatically.',
      detail: `${e.message}\n\nCode LM will try to connect to existing databases.`,
      buttons: ['OK'],
    })
  }

  // ── Start Python backend, passing dynamic port env vars ────────────────
  startBackend(ports)

  updateLoading('Waiting for backend…')
  try {
    await waitForBackend()
  } catch (e) {
    closeLoading()
    const portInfo = ports
      ? `Postgres :${ports.postgres}  Neo4j :${ports.neo4jBolt}  Qdrant :${ports.qdrant}`
      : 'Could not determine ports'
    await dialog.showErrorBox(
      'Backend failed to start',
      `${e.message}\n\n${portInfo}`
    )
    app.quit()
    return
  }

  createMainWindow()
})

app.on('window-all-closed', () => {
  if (backendProcess) backendProcess.kill()
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  if (!mainWindow) createMainWindow()
})

app.on('before-quit', async () => {
  if (backendProcess) backendProcess.kill()
  await stopContainers(composeDir())
})
