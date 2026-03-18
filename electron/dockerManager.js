/**
 * dockerManager.js
 *
 * Orchestrates Docker container lifecycle for EngramAI:
 *  1. If containers already running → load saved ports from userData, reuse them
 *  2. Otherwise → find free ports → write runtime compose → docker compose up
 *  3. Save chosen ports to userData/engramai-ports.json for next launch
 *
 * Exports:
 *   startContainers(composeDir, updateLoading) → Promise<Ports>
 *   stopContainers(composeDir)                → Promise<void>
 */

'use strict'

const { execFile, exec } = require('child_process')
const { app }             = require('electron')
const fs                  = require('fs')
const path                = require('path')

const { findEngramAIPorts } = require('./portFinder')
const { writeRuntimeCompose } = require('./composeWriter')

const USER_DATA   = () => app.getPath('userData')
const PORTS_FILE  = () => path.join(USER_DATA(), 'engramai-ports.json')
const RUNTIME_COMPOSE = () => path.join(USER_DATA(), 'docker-compose.runtime.yml')

// ── Helpers ───────────────────────────────────────────────────────────────────

function runCompose(composePath, args) {
  return new Promise((resolve, reject) => {
    execFile('docker', ['compose', '-p', 'engramai', '-f', composePath, ...args],
      { timeout: 90000 },
      (err, _stdout, stderr) => {
        if (err) {
          // Fall back to legacy docker-compose binary
          execFile('docker-compose', ['-p', 'engramai', '-f', composePath, ...args],
            { timeout: 90000 },
            (err2, _stdout2, stderr2) => {
              if (err2) reject(new Error(
                `docker compose: ${stderr || err.message}\n` +
                `docker-compose: ${stderr2 || err2.message}`
              ))
              else resolve()
            }
          )
        } else {
          resolve()
        }
      }
    )
  })
}

function isDockerAvailable() {
  return new Promise(resolve => exec('docker info', err => resolve(!err)))
}

function areContainersRunning() {
  return new Promise(resolve => {
    exec(
      'docker ps --filter name=engramai_postgres --filter status=running --format {{.Names}}',
      (err, stdout) => resolve(!err && stdout.trim().length > 0)
    )
  })
}

function savePorts(ports) {
  try {
    fs.writeFileSync(PORTS_FILE(), JSON.stringify(ports, null, 2), 'utf8')
  } catch (e) {
    console.warn('[dockerManager] Could not save ports file:', e.message)
  }
}

function loadSavedPorts() {
  try {
    const raw = fs.readFileSync(PORTS_FILE(), 'utf8')
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)) }

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Start EngramAI containers, returning the ports they're bound to.
 * Runtime compose file is written to userData (always writable, even in Program Files installs).
 *
 * @param {Function} updateLoading  Callback(message) to update the splash screen
 * @returns {Promise<{postgres: number, neo4jBolt: number, neo4jBrowser: number, qdrant: number}>}
 */
async function startContainers(updateLoading = () => {}) {
  updateLoading('Checking Docker…')

  const dockerOk = await isDockerAvailable()
  if (!dockerOk) return null  // caller handles the "no Docker" dialog

  const alreadyRunning = await areContainersRunning()

  if (alreadyRunning) {
    const saved = loadSavedPorts()
    if (saved) {
      console.log('[dockerManager] Containers already running, reusing ports:', saved)
      updateLoading('Databases already running…')
      return saved
    }
    console.warn('[dockerManager] Containers running but no saved ports — restarting')
  }

  // ── Find free ports ────────────────────────────────────────────────────────
  updateLoading('Finding available ports…')
  const ports = await findEngramAIPorts()
  console.log('[dockerManager] Allocated ports:', ports)

  // ── Write runtime compose to userData (writable on all platforms) ──────────
  const runtimeComposePath = writeRuntimeCompose(USER_DATA(), ports)
  console.log('[dockerManager] Wrote', runtimeComposePath)

  // ── Start containers ───────────────────────────────────────────────────────
  updateLoading('Starting databases (first run may take a minute)…')
  await runCompose(runtimeComposePath, ['up', '-d'])

  // Give containers a moment to initialise
  await sleep(2000)

  // ── Persist ports for next launch ─────────────────────────────────────────
  savePorts(ports)
  return ports
}

/**
 * Stop EngramAI containers gracefully.
 * Called on app.before-quit.
 *
 * @param {string} composeDir
 */
async function stopContainers() {
  const runtimePath = RUNTIME_COMPOSE()
  if (!fs.existsSync(runtimePath)) return
  try {
    await runCompose(runtimePath, ['down'])
    console.log('[dockerManager] Containers stopped')
  } catch (e) {
    console.warn('[dockerManager] Could not stop containers:', e.message)
  }
}

module.exports = { startContainers, stopContainers }
