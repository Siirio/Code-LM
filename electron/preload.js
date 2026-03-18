const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electron', {
  platform: process.platform,
  openFolder: () => ipcRenderer.invoke('open-folder'),
  openInNewWindow: (path) => ipcRenderer.invoke('open-in-new-window', path),
})
