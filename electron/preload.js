// Minimal preload — no Node APIs exposed to renderer.
// The React app talks to the backend over HTTP only.
const { contextBridge } = require('electron')

contextBridge.exposeInMainWorld('engramAI', {
  platform: process.platform,
})
