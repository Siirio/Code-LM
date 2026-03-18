const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electron', {
  platform: process.platform,
  openFolder: () => ipcRenderer.invoke('open-folder'),
  onProjectOpened: (cb) => ipcRenderer.on('project-opened', (_, path) => cb(path)),
})
