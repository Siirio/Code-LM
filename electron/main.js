const { app, BrowserWindow, dialog, shell } = require('electron')
const { autoUpdater } = require('electron-updater')
const { spawn, execFile, exec } = require('child_process')
const path = require('path')
const http = require('http')
const fs = require('fs')

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

function dockerComposePath() {
  return path.join(resourcesDir(), 'docker-compose.yml')
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
      <div class="logo">EngramAI</div>
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

// ── Docker ────────────────────────────────────────────────────────────────────

function isDockerAvailable() {
  return new Promise(resolve => {
    exec('docker info', (err) => resolve(!err))
  })
}

function dockerComposeUp() {
  return new Promise((resolve, reject) => {
    const composePath = dockerComposePath()
    // Try docker compose (v2) first, fall back to docker-compose (v1)
    const cmd = process.platform === 'win32' ? 'docker' : 'docker'
    const args = ['compose', '-f', composePath, 'up', '-d']
    const proc = execFile(cmd, args, { timeout: 60000 }, (err, stdout, stderr) => {
      if (err) {
        // Try legacy docker-compose
        execFile('docker-compose', ['-f', composePath, 'up', '-d'], { timeout: 60000 }, (err2) => {
          if (err2) reject(new Error('Could not start databases via Docker'))
          else resolve()
        })
      } else {
        resolve()
      }
    })
  })
}

async function ensureDatabases() {
  updateLoading('Checking Docker…')
  const dockerOk = await isDockerAvailable()

  if (!dockerOk) {
    const choice = await dialog.showMessageBox({
      type: 'warning',
      title: 'Docker required',
      message: 'EngramAI needs Docker to run its databases.',
      detail: 'Install Docker Desktop, then restart EngramAI.\n\nDocker Desktop: https://www.docker.com/products/docker-desktop',
      buttons: ['Open Docker website', 'Continue anyway', 'Quit'],
      defaultId: 0,
    })
    if (choice.response === 0) shell.openExternal('https://www.docker.com/products/docker-desktop')
    if (choice.response === 2) { app.quit(); process.exit(0) }
    return // continue without Docker (user chose "continue anyway")
  }

  updateLoading('Starting databases (first run may take a minute)…')
  try {
    await dockerComposeUp()
    updateLoading('Databases started')
    // Give containers a moment to initialise
    await sleep(2000)
  } catch (e) {
    console.error('Docker compose failed:', e.message)
    await dialog.showMessageBox({
      type: 'warning',
      title: 'Database startup failed',
      message: 'Could not start databases automatically.',
      detail: `${e.message}\n\nEngramAI will try to connect to existing databases.`,
      buttons: ['OK'],
    })
  }
}

// ── Backend process ───────────────────────────────────────────────────────────

function startBackend() {
  updateLoading('Starting EngramAI backend…')

  let bin, args, cwd

  if (app.isPackaged) {
    // Packaged: use PyInstaller binary
    bin = backendBinaryPath()
    args = []
    cwd = path.dirname(bin)
    if (!fs.existsSync(bin)) {
      dialog.showErrorBox('Missing backend', `Backend binary not found:\n${bin}`)
      app.quit()
      return
    }
  } else {
    // Dev mode: use system Python
    bin = process.platform === 'win32' ? 'python' : 'python3'
    args = ['main.py', '--dev']
    cwd = path.join(__dirname, '..', 'backend')
  }

  backendProcess = spawn(bin, args, { cwd, env: { ...process.env } })
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

function createMainWindow() {
  mainWindow = new BrowserWindow({
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
    title: 'EngramAI',
    show: false,
  })

  mainWindow.loadURL('http://localhost:8765')

  mainWindow.once('ready-to-show', () => {
    closeLoading()
    mainWindow.show()
    mainWindow.focus()
    // Check for app updates silently
    if (app.isPackaged) autoUpdater.checkForUpdatesAndNotify()
  })

  mainWindow.on('closed', () => { mainWindow = null })
}

// ── Auto updater ──────────────────────────────────────────────────────────────

autoUpdater.on('update-available', () => {
  dialog.showMessageBox({
    type: 'info',
    title: 'Update available',
    message: 'A new version of EngramAI is downloading…',
    buttons: ['OK'],
  })
})

autoUpdater.on('update-downloaded', () => {
  dialog.showMessageBox({
    type: 'question',
    buttons: ['Restart now', 'Later'],
    title: 'Update ready',
    message: 'EngramAI update downloaded. Restart to apply?',
  }).then(r => { if (r.response === 0) autoUpdater.quitAndInstall() })
})

// ── Helpers ───────────────────────────────────────────────────────────────────

function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }

// ── App lifecycle ─────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  showLoading('Initialising…')

  await ensureDatabases()
  startBackend()

  updateLoading('Waiting for backend…')
  try {
    await waitForBackend()
  } catch (e) {
    closeLoading()
    await dialog.showErrorBox('Backend failed to start', e.message + '\n\nCheck that ports 8765, 5433, 7687, 6333 are free.')
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

app.on('before-quit', () => {
  if (backendProcess) backendProcess.kill()
})
