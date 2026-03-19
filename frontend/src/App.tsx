import { useEffect, useRef, useState } from 'react'
import {
  chatStream,
  clearAuth,
  createSession,
  deductCredit,
  deleteSession,
  fetchFileContent,
  fetchFileTree,
  getMessages,
  getProjectStatus,
  listSessions,
  loadAuth,
  loadCredits,
  saveAuth,
  saveCredits,
  scanProject,
  type AuthConfig,
  type ChatMessage,
  type CreditsConfig,
  type FileEditProposal,
  type Session,
} from './api/client'
import DiffDialog from './components/DiffDialog'
import FileContentPanel, { type OpenFile } from './components/FileContentPanel'
import TerminalPanel from './components/TerminalPanel'
import './index.css'

// ── Electron typings ─────────────────────────────────────────────────────────

declare global {
  interface Window {
    electron?: {
      platform: string
      openFolder: () => Promise<string | null>
      openInNewWindow: (path: string) => Promise<void>
    }
  }
}

// ── Config ────────────────────────────────────────────────────────────────────

function getProjectConfig(): { projectId: string; rootPath: string } {
  const params = new URLSearchParams(window.location.search)
  const rootPath = params.get('root') || localStorage.getItem('codelm_root') || ''
  const projectId = params.get('pid') || localStorage.getItem('codelm_pid') || ''
  return { projectId, rootPath }
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface DisplayMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  agentLabel?: string
  streaming?: boolean
}

interface CodeLMSettings {
  fontSize: number
  uiScale: number  // 1–6, where 4 = 100% (default)
}

const SCALE_ZOOM = [0, 0.65, 0.75, 0.85, 1.0, 1.15, 1.35] // index 0 unused

const DEFAULT_SETTINGS: CodeLMSettings = {
  fontSize: 14,
  uiScale: 4,
}

function loadSettings(): CodeLMSettings {
  try {
    const raw = localStorage.getItem('codelm_settings')
    if (raw) return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) }
  } catch { /* ignore */ }
  return { ...DEFAULT_SETTINGS }
}

// Apply zoom immediately at module load to avoid layout flash
;(function applyInitialZoom() {
  const s = loadSettings()
  document.documentElement.style.zoom = String(SCALE_ZOOM[s.uiScale] ?? 1.0)
})()

function saveSettings(s: CodeLMSettings) {
  localStorage.setItem('codelm_settings', JSON.stringify(s))
}

// ── Slash command definitions ────────────────────────────────────────────────

const SLASH_COMMANDS = [
  { command: '/full-scan', description: 'Index entire project' },
  { command: '/auto-scan', description: 'AI finds context automatically' },
  { command: '/package-scan', description: 'Scan specific directory' },
  { command: '/terminal', description: 'Open terminal panel' },
]

// ── Helpers ──────────────────────────────────────────────────────────────────

function isElectron(): boolean {
  return Boolean(window.electron)
}

// ── App ──────────────────────────────────────────────────────────────────────

export default function App() {
  const { projectId, rootPath } = getProjectConfig()

  const [setupDone, setSetupDone] = useState(Boolean(projectId && rootPath))
  const [setupRoot, setSetupRoot] = useState('')

  function commitSetup(root: string) {
    const pid = btoa(root).replace(/[^a-zA-Z0-9]/g, '').slice(0, 32)
    localStorage.setItem('codelm_root', root)
    localStorage.setItem('codelm_pid', pid)
    window.location.search = `?root=${encodeURIComponent(root)}&pid=${pid}`
  }

  async function handleBrowse() {
    if (window.electron?.openFolder) {
      const folder = await window.electron.openFolder()
      if (folder) {
        setSetupRoot(folder)
      }
    }
  }

  if (!setupDone) {
    return (
      <div className="setup-screen">
        <div className="setup-logo">Code LM</div>
        <div className="setup-headline">AI that knows your project. Not just your files.</div>
        <div className="setup-subheading">Ship with confidence. Code LM knows your project the way a senior engineer does — and keeps that knowledge across every session, every feature, every change.</div>
        <p className="setup-label">Open a project to get started:</p>
        <div className="setup-input-row">
          <input
            autoFocus
            value={setupRoot}
            onChange={e => setSetupRoot(e.target.value)}
            placeholder="/home/user/my-project"
            onKeyDown={e => {
              if (e.key === 'Enter' && setupRoot.trim()) {
                commitSetup(setupRoot.trim())
              }
            }}
          />
          {isElectron() && (
            <button className="browse-btn" onClick={handleBrowse}>Browse</button>
          )}
        </div>
        <button
          className="setup-continue-btn"
          disabled={!setupRoot.trim()}
          onClick={() => commitSetup(setupRoot.trim())}
        >
          Open Project
        </button>
        <p className="hint">Press Enter or click Open Project to continue</p>
      </div>
    )
  }

  return <IDE projectId={projectId} rootPath={rootPath} />
}

// ── FileTreePanel ─────────────────────────────────────────────────────────────

interface TreeNode {
  name: string
  path: string
  type: 'file' | 'dir'
  children: TreeNode[]
}

interface UndoOp {
  label: string
  undo: () => Promise<void>
  redo: () => Promise<void>
}

interface FileTreePanelProps {
  rootPath: string
  onFileOpen?: (path: string) => void
  onRefresh?: () => void
  pushUndo?: (op: UndoOp) => void
}

function FileTreePanel({ rootPath, onFileOpen, onRefresh, pushUndo }: FileTreePanelProps) {
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; node: TreeNode } | null>(null)
  const [creating, setCreating] = useState<{ parentPath: string; type: 'file' | 'dir'; name: string } | null>(null)
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set())
  const [dragSel, setDragSel] = useState<{ x1: number; y1: number; x2: number; y2: number } | null>(null)
  const [bulkMenu, setBulkMenu] = useState<{ x: number; y: number } | null>(null)
  const treeBodyRef = useRef<HTMLDivElement>(null)
  const itemRefs = useRef<Map<string, HTMLDivElement>>(new Map())
  const selectedPathsRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    fetchFileTree(rootPath)
      .then(t => { setTree(t); setExpanded(new Set([rootPath])) })
      .catch(() => setError('Could not load files'))
  }, [rootPath])

  // Drag selection handlers
  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (!dragSel || !treeBodyRef.current) return
      const rect = treeBodyRef.current.getBoundingClientRect()
      const x2 = e.clientX - rect.left
      const y2 = e.clientY - rect.top
      setDragSel(d => d ? { ...d, x2, y2 } : null)

      const selX1 = Math.min(dragSel.x1, x2) + rect.left
      const selY1 = Math.min(dragSel.y1, y2) + rect.top
      const selX2 = Math.max(dragSel.x1, x2) + rect.left
      const selY2 = Math.max(dragSel.y1, y2) + rect.top

      const newSelected = new Set<string>()
      itemRefs.current.forEach((el, path) => {
        const r = el.getBoundingClientRect()
        if (r.bottom > selY1 && r.top < selY2 && r.right > selX1 && r.left < selX2) {
          newSelected.add(path)
        }
      })
      selectedPathsRef.current = newSelected
      setSelectedPaths(newSelected)
    }

    function onUp(e: MouseEvent) {
      if (!dragSel) return
      const hadSelection = selectedPathsRef.current.size > 1
      setDragSel(null)
      if (hadSelection) {
        setBulkMenu({ x: e.clientX, y: e.clientY })
      }
    }

    if (dragSel) {
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
      return () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
      }
    }
  }, [dragSel])

  function toggle(path: string) {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  // Compact single-child directory chains
  function compactNode(node: TreeNode): TreeNode {
    if (node.type !== 'dir') return node
    let current = node
    let compactedName = node.name
    while (
      current.children.length === 1 &&
      current.children[0].type === 'dir'
    ) {
      current = current.children[0]
      compactedName += '/' + current.name
    }
    return {
      ...current,
      name: compactedName,
      path: current.path,
      children: current.children.map(compactNode),
    }
  }

  async function handleDelete(node: TreeNode) {
    if (!window.confirm(`Delete "${node.name}"? This cannot be undone.`)) return
    let cachedContent = ''
    if (node.type === 'file') {
      try {
        const r = await fetch(`/api/v1/files/content?path=${encodeURIComponent(node.path)}`)
        if (r.ok) { const d = await r.json(); cachedContent = d.content || '' }
      } catch { /* ignore */ }
    }

    await fetch(`/api/v1/files?path=${encodeURIComponent(node.path)}`, { method: 'DELETE' })
    onRefresh?.()

    pushUndo?.({
      label: `Delete ${node.name}`,
      undo: async () => {
        await fetch('/api/v1/files/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: node.path, is_dir: node.type === 'dir', content: cachedContent }),
        })
      },
      redo: async () => {
        await fetch(`/api/v1/files?path=${encodeURIComponent(node.path)}`, { method: 'DELETE' })
      },
    })
  }

  async function handleCreate(parentPath: string, type: 'file' | 'dir', name: string) {
    const path = `${parentPath}/${name}`
    await fetch('/api/v1/files/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, is_dir: type === 'dir', content: '' }),
    })
    onRefresh?.()

    pushUndo?.({
      label: `Create ${name}`,
      undo: async () => {
        await fetch(`/api/v1/files?path=${encodeURIComponent(path)}`, { method: 'DELETE' })
      },
      redo: async () => {
        await fetch('/api/v1/files/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path, is_dir: type === 'dir', content: '' }),
        })
      },
    })
  }

  async function addToGitignore(paths: string[]) {
    const gitignorePath = `${rootPath}/.gitignore`
    const relPaths = paths.map(p => p.replace(rootPath + '/', ''))
    const newEntries = '\n' + relPaths.join('\n') + '\n'

    try {
      const r = await fetch(`/api/v1/files/content?path=${encodeURIComponent(gitignorePath)}`)
      const existing = r.ok ? (await r.json()).content || '' : ''

      await fetch(`/api/v1/files/content?path=${encodeURIComponent(gitignorePath)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: existing + newEntries }),
      })
    } catch {
      await fetch('/api/v1/files/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: gitignorePath, is_dir: false, content: relPaths.join('\n') + '\n' }),
      })
    }
  }

  function renderNode(node: TreeNode, depth: number): React.ReactNode {
    const isOpen = expanded.has(node.path)
    const indent = depth * 14

    if (node.type === 'dir') {
      return (
        <div key={node.path}>
          <div
            ref={(el: HTMLDivElement | null) => {
              if (el) itemRefs.current.set(node.path, el)
              else itemRefs.current.delete(node.path)
            }}
            className={`tree-item tree-dir${selectedPaths.has(node.path) ? ' tree-item-selected' : ''}`}
            style={{ paddingLeft: `${8 + indent}px` }}
            onClick={() => toggle(node.path)}
            onContextMenu={e => {
              e.preventDefault()
              e.stopPropagation()
              setCtxMenu({ x: e.clientX, y: e.clientY, node })
            }}
          >
            <span className="tree-arrow">{isOpen ? '\u25BE' : '\u25B8'}</span>
            <span className="tree-icon">{'\uD83D\uDCC1'}</span>
            <span className="tree-name">{node.name}</span>
          </div>
          {isOpen && node.children.map(child => renderNode(child, depth + 1))}
        </div>
      )
    }

    const ext = node.name.split('.').pop() || ''
    const icon = ['ts','tsx','js','jsx'].includes(ext) ? '\u27E8\u27E9' :
                 ['py'].includes(ext) ? '\uD83D\uDC0D' :
                 ['java','kt'].includes(ext) ? '\u2615' :
                 ['json','yml','yaml'].includes(ext) ? '{}' :
                 ['md'].includes(ext) ? '\uD83D\uDCDD' : '\uD83D\uDCC4'

    return (
      <div
        key={node.path}
        ref={(el: HTMLDivElement | null) => {
          if (el) itemRefs.current.set(node.path, el)
          else itemRefs.current.delete(node.path)
        }}
        className={`tree-item tree-file${selectedPaths.has(node.path) ? ' tree-item-selected' : ''}`}
        style={{ paddingLeft: `${8 + indent}px` }}
        title={node.path}
        onClick={() => onFileOpen?.(node.path)}
        onContextMenu={e => {
          e.preventDefault()
          e.stopPropagation()
          setCtxMenu({ x: e.clientX, y: e.clientY, node })
        }}
      >
        <span className="tree-icon-file">{icon}</span>
        <span className="tree-name">{node.name}</span>
      </div>
    )
  }

  if (error) return <div className="tree-error">{error}</div>
  if (!tree) return <div className="tree-loading">Loading...</div>

  return (
    <div className="file-tree">
      <div className="tree-header">
        <span className="tree-header-label">FILES</span>
      </div>
      {creating && (
        <div className="tree-create-input-wrap">
          <input
            autoFocus
            className="tree-create-input"
            placeholder={creating.type === 'file' ? 'filename.ext' : 'folder-name'}
            value={creating.name}
            onChange={e => setCreating(c => c ? { ...c, name: e.target.value } : null)}
            onKeyDown={e => {
              if (e.key === 'Enter' && creating.name.trim()) {
                handleCreate(creating.parentPath, creating.type, creating.name.trim())
                setCreating(null)
              }
              if (e.key === 'Escape') setCreating(null)
            }}
            onBlur={() => setCreating(null)}
          />
        </div>
      )}
      <div
        className="tree-body"
        ref={treeBodyRef}
        style={{ position: 'relative' }}
        onMouseDown={e => {
          if ((e.target as Element).closest('.tree-item')) return
          const rect = treeBodyRef.current!.getBoundingClientRect()
          setDragSel({ x1: e.clientX - rect.left, y1: e.clientY - rect.top, x2: e.clientX - rect.left, y2: e.clientY - rect.top })
          setSelectedPaths(new Set())
        }}
      >
        {tree.children.map(child => renderNode(compactNode(child), 0))}
        {dragSel && (
          <div
            className="tree-drag-select"
            style={{
              left: Math.min(dragSel.x1, dragSel.x2),
              top: Math.min(dragSel.y1, dragSel.y2),
              width: Math.abs(dragSel.x2 - dragSel.x1),
              height: Math.abs(dragSel.y2 - dragSel.y1),
            }}
          />
        )}
      </div>

      {/* Context menu */}
      {ctxMenu && (
        <>
          <div style={{ position: 'fixed', inset: 0, zIndex: 999 }} onClick={() => setCtxMenu(null)} />
          <div
            className="tree-context-menu"
            style={{ position: 'fixed', left: ctxMenu.x, top: ctxMenu.y, zIndex: 1000 }}
          >
            <div className="ctx-item" onClick={() => {
              setCreating({ parentPath: ctxMenu.node.type === 'dir' ? ctxMenu.node.path : ctxMenu.node.path.split('/').slice(0, -1).join('/'), type: 'file', name: '' })
              setCtxMenu(null)
            }}>New File</div>
            <div className="ctx-item" onClick={() => {
              setCreating({ parentPath: ctxMenu.node.type === 'dir' ? ctxMenu.node.path : ctxMenu.node.path.split('/').slice(0, -1).join('/'), type: 'dir', name: '' })
              setCtxMenu(null)
            }}>New Folder</div>
            <div className="ctx-separator" />
            <div className="ctx-item ctx-item-danger" onClick={() => {
              handleDelete(ctxMenu.node)
              setCtxMenu(null)
            }}>Delete</div>
          </div>
        </>
      )}

      {/* Bulk action menu */}
      {bulkMenu && selectedPaths.size > 1 && (
        <>
          <div style={{ position: 'fixed', inset: 0, zIndex: 999 }} onClick={() => { setBulkMenu(null); setSelectedPaths(new Set()) }} />
          <div className="tree-context-menu" style={{ position: 'fixed', left: bulkMenu.x, top: bulkMenu.y, zIndex: 1000 }}>
            <div className="ctx-item-header">{selectedPaths.size} items selected</div>
            <div className="ctx-separator" />
            <div className="ctx-item" onClick={async () => {
              await addToGitignore(Array.from(selectedPaths))
              setBulkMenu(null); setSelectedPaths(new Set())
            }}>Add to .gitignore</div>
            <div className="ctx-item ctx-item-danger" onClick={async () => {
              for (const p of selectedPaths) {
                await fetch(`/api/v1/files?path=${encodeURIComponent(p)}`, { method: 'DELETE' })
              }
              onRefresh?.()
              setBulkMenu(null); setSelectedPaths(new Set())
            }}>Delete All</div>
          </div>
        </>
      )}
    </div>
  )
}

// ── IDE ───────────────────────────────────────────────────────────────────────

function IDE({ projectId, rootPath }: { projectId: string; rootPath: string }) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [statusLine, setStatusLine] = useState('')
  const [backendOk, setBackendOk] = useState(false)
  const [acceptAll, setAcceptAll] = useState(false)
  const [pendingEdit, setPendingEdit] = useState<FileEditProposal | null>(null)
  const [scanBusy, setScanBusy] = useState(false)

  // Settings
  const [settings, setSettings] = useState<CodeLMSettings>(loadSettings)
  const [showSettings, setShowSettings] = useState(false)

  // Help modal
  const [showHelp, setShowHelp] = useState(false)

  // File viewer
  const [openFile, setOpenFile] = useState<OpenFile | null>(null)

  // Window title
  const projectName = rootPath.split(/[/\\]/).filter(Boolean).pop() || 'Code LM'
  useEffect(() => {
    document.title = `${projectName} — Code LM`
  }, [projectName])

  // Auth: own key OR credits
  const [auth, setAuth] = useState<AuthConfig | null>(loadAuth)
  const [showAuth, setShowAuth] = useState(false)
  const [credits, setCredits] = useState<CreditsConfig>(loadCredits)

  // Delete chat confirmation
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)

  // Tree refresh key
  const [treeKey, setTreeKey] = useState(0)

  // Undo/Redo stacks
  const undoStack = useRef<UndoOp[]>([])
  const redoStack = useRef<UndoOp[]>([])

  // Left panel tab
  const [leftTab, setLeftTab] = useState<'chats' | 'files'>('chats')

  // Panel widths (resizable)
  const [leftWidth, setLeftWidth] = useState(240)
  const [midWidth, setMidWidth] = useState(480)

  // Content zoom (Ctrl+wheel per panel)
  const [leftZoom, setLeftZoom] = useState(1.0)
  const [midFontSize, setMidFontSize] = useState(13)

  // Terminal
  const [terminalOpen, setTerminalOpen] = useState(false)
  const [termHeight, setTermHeight] = useState(260)

  // Resize refs
  const leftPanelRef = useRef<HTMLElement>(null)
  const midPanelRef = useRef<HTMLDivElement>(null)
  const chatPanelRef = useRef<HTMLElement>(null)
  const isDraggingLeft = useRef(false)
  const isDraggingMid = useRef(false)
  const isDraggingTerm = useRef(false)
  const dragStartX = useRef(0)
  const dragStartY = useRef(0)
  const dragStartW = useRef(0)
  const dragStartH = useRef(0)

  // Slash command autocomplete
  const [slashSuggestions, setSlashSuggestions] = useState<typeof SLASH_COMMANDS>([])
  const [selectedSuggestion, setSelectedSuggestion] = useState(0)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const streamCancelRef = useRef<(() => void) | null>(null)

  // Apply settings to CSS
  useEffect(() => {
    document.documentElement.style.setProperty('--font-size', `${settings.fontSize}px`)
    document.documentElement.style.zoom = String(SCALE_ZOOM[settings.uiScale] ?? 1.0)
    saveSettings(settings)
  }, [settings])

  // ── Startup ─────────────────────────────────────────────────────────────────

  useEffect(() => {
    checkBackend()
  }, [])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Resize drag handlers
  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (isDraggingLeft.current) {
        setLeftWidth(Math.max(120, Math.min(600, dragStartW.current + e.clientX - dragStartX.current)))
      }
      if (isDraggingMid.current) {
        setMidWidth(Math.max(200, Math.min(1200, dragStartW.current + e.clientX - dragStartX.current)))
      }
      if (isDraggingTerm.current) {
        setTermHeight(Math.max(100, Math.min(800, dragStartH.current + dragStartY.current - e.clientY)))
      }
    }
    function onMouseUp() {
      isDraggingLeft.current = false
      isDraggingMid.current = false
      isDraggingTerm.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    return () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
    }
  }, [])

  // Ctrl+wheel — zoom content inside left panel
  useEffect(() => {
    function onWheel(e: WheelEvent) {
      if (!e.ctrlKey) return
      const t = e.target as Element
      const zoomIn = e.deltaY < 0
      if (leftPanelRef.current?.contains(t)) {
        e.preventDefault()
        setLeftZoom(z => Math.round(Math.max(0.5, Math.min(2.0, z + (zoomIn ? 0.1 : -0.1))) * 10) / 10)
      }
    }
    window.addEventListener('wheel', onWheel, { passive: false })
    return () => window.removeEventListener('wheel', onWheel)
  }, [])

  // Ctrl+wheel — zoom font size in mid panel (direct listener avoids Monaco iframe issues)
  useEffect(() => {
    const el = midPanelRef.current
    if (!el) return
    const handler = (e: WheelEvent) => {
      if (!e.ctrlKey) return
      e.preventDefault()
      e.stopPropagation()
      setMidFontSize(f => Math.max(8, Math.min(28, f + (e.deltaY < 0 ? 1 : -1))))
    }
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [openFile])

  // Ctrl+Z / Ctrl+Y undo/redo for file tree operations
  useEffect(() => {
    async function onKeyDown(e: KeyboardEvent) {
      const active = document.activeElement
      if (active && (active.classList.contains('inputarea') || active.tagName === 'TEXTAREA')) return

      if (e.ctrlKey && e.key === 'z' && !e.shiftKey) {
        const op = undoStack.current[undoStack.current.length - 1]
        if (!op) return
        e.preventDefault()
        await op.undo()
        undoStack.current = undoStack.current.slice(0, -1)
        redoStack.current = [...redoStack.current, op]
        refreshTree()
      }
      if ((e.ctrlKey && e.key === 'y') || (e.ctrlKey && e.shiftKey && e.key === 'z')) {
        const op = redoStack.current[redoStack.current.length - 1]
        if (!op) return
        e.preventDefault()
        await op.redo()
        redoStack.current = redoStack.current.slice(0, -1)
        undoStack.current = [...undoStack.current, op]
        refreshTree()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // Ctrl+T toggle terminal
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.ctrlKey && e.key === 't') {
        e.preventDefault()
        setTerminalOpen(v => !v)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  async function checkBackend(attempt = 0) {
    try {
      const res = await fetch('/health')
      if (!res.ok) throw new Error()
      setBackendOk(true)
      const status = await getProjectStatus(projectId)
      if (status.indexed) {
        addSystem(`Backend connected. Project indexed — ${status.files_indexed} files.`)
      } else {
        addSystem('Backend connected. Project not indexed. Use /full-scan when ready.')
      }
      await loadSessions()
    } catch {
      if (isElectron() && attempt < 30) {
        setTimeout(() => checkBackend(attempt + 1), 2000)
        if (attempt === 0) addSystem('Connecting to backend...')
      } else {
        addSystem(isElectron() ? 'Backend failed to start. Please restart the app.' : 'Backend not available. Start it: cd backend && python main.py')
      }
    }
  }

  async function loadSessions() {
    try {
      const list = await listSessions(projectId)
      setSessions(list)
      if (list.length > 0) {
        await switchSession(list[0].id)
      } else {
        await newSession()
      }
    } catch {
      await newSession()
    }
  }

  async function newSession() {
    try {
      const s = await createSession(projectId)
      setSessions(prev => [s as Session, ...prev])
      setCurrentSessionId(s.id)
      setMessages([])
      setAcceptAll(false)
    } catch (e) {
      addSystem(`Could not create session: ${e}`)
    }
  }

  async function switchSession(sessionId: string) {
    setCurrentSessionId(sessionId)
    setAcceptAll(false)
    try {
      const msgs = await getMessages(sessionId)
      if (msgs.length === 0) {
        setMessages([])
      } else {
        setMessages(msgs.map((m, i) => ({ id: String(i), role: m.role, content: m.content })))
      }
    } catch {
      setMessages([])
    }
  }

  function askDeleteSession(sessionId: string) {
    setConfirmDeleteId(sessionId)
  }

  async function confirmDeleteSession() {
    if (!confirmDeleteId) return
    await deleteSession(confirmDeleteId)
    const updated = sessions.filter(s => s.id !== confirmDeleteId)
    setSessions(updated)
    if (confirmDeleteId === currentSessionId) {
      if (updated.length > 0) await switchSession(updated[0].id)
      else await newSession()
    }
    setConfirmDeleteId(null)
  }

  // ── Scan ─────────────────────────────────────────────────────────────────────

  async function runScan(mode: string, hint?: string) {
    setScanBusy(true)
    setStatusLine(`Scanning (${mode})...`)
    try {
      const result = await scanProject({
        project_id: projectId,
        root_path: rootPath,
        scan_mode: mode,
        entry_point: hint,
      })
      addSystem(`Scan complete — ${result.files_found} files, ${result.classes_found} classes indexed`)
    } catch (e: unknown) {
      addSystem(`Scan failed: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setScanBusy(false)
      setStatusLine('')
    }
  }

  // ── File open ─────────────────────────────────────────────────────────────

  async function handleFileOpen(path: string) {
    try {
      const data = await fetchFileContent(path)
      setOpenFile({ path, ...data })
      setMidWidth(Math.floor((window.innerWidth - leftWidth - 12) * 0.75))
    } catch (e) {
      addSystem(`Could not open file: ${e}`)
    }
  }

  function refreshTree() { setTreeKey(k => k + 1) }

  function pushUndoOp(op: UndoOp) {
    undoStack.current = [...undoStack.current, op]
    redoStack.current = []
  }

  // ── Slash autocomplete logic ──────────────────────────────────────────────

  function updateSlashSuggestions(value: string) {
    if (value.startsWith('/') && !value.includes(' ')) {
      const filtered = SLASH_COMMANDS.filter(c => c.command.startsWith(value))
      setSlashSuggestions(filtered)
      setSelectedSuggestion(0)
    } else {
      setSlashSuggestions([])
    }
  }

  function selectSlashCommand(command: string) {
    // If the command expects an argument, add a space; otherwise just set it
    setInput(command + (command === '/full-scan' ? '' : ' '))
    setSlashSuggestions([])
    inputRef.current?.focus()
  }

  function handleInputKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (slashSuggestions.length > 0) {
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedSuggestion(prev => (prev - 1 + slashSuggestions.length) % slashSuggestions.length)
        return
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedSuggestion(prev => (prev + 1) % slashSuggestions.length)
        return
      }
      if (e.key === 'Tab') {
        e.preventDefault()
        selectSlashCommand(slashSuggestions[selectedSuggestion].command)
        return
      }
      if (e.key === 'Enter') {
        e.preventDefault()
        selectSlashCommand(slashSuggestions[selectedSuggestion].command)
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setSlashSuggestions([])
        return
      }
    } else {
      if (e.key === 'Enter' && !e.shiftKey) {
        send()
      }
    }
  }

  // ── Send message ──────────────────────────────────────────────────────────

  async function send() {
    if (busy || !input.trim()) return

    // Gate: need either own API key or credits
    if (!auth?.apiKey) {
      if (!deductCredit()) {
        setShowAuth(true)
        return
      }
      setCredits(loadCredits())
    }
    let msg = input.trim()
    setInput('')
    setSlashSuggestions([])

    // Slash command handling
    if (msg.startsWith('/full-scan')) {
      addSystem('Starting full scan...')
      await runScan('full')
      const rest = msg.replace('/full-scan', '').trim()
      if (rest) { setInput(rest); setTimeout(send, 100) }
      return
    }
    if (msg.startsWith('/auto-scan')) {
      const hint = msg.replace('/auto-scan', '').trim() || undefined
      addSystem(`Auto-scan${hint ? `: ${hint}` : ''}...`)
      await runScan('smart', hint)
      if (hint) { setInput(hint); setTimeout(send, 100) }
      return
    }
    if (msg.startsWith('/package-scan')) {
      const dir = msg.replace('/package-scan', '').trim() || rootPath
      addSystem(`Package scan: ${dir}`)
      await runScan('folder', dir)
      return
    }
    if (msg.startsWith('/terminal')) {
      setTerminalOpen(true)
      return
    }

    if (!backendOk) { addSystem('Backend not running.'); return }

    let sessionId = currentSessionId
    if (!sessionId) {
      const s = await createSession(projectId)
      sessionId = s.id
      setCurrentSessionId(s.id)
      setSessions(prev => [s as Session, ...prev])
    }

    addMessage('user', msg)
    setBusy(true)

    const assistantId = Date.now().toString()
    setMessages(prev => [
      ...prev,
      { id: assistantId, role: 'assistant', content: '', streaming: true },
    ])

    let agentLabel = ''

    streamCancelRef.current = chatStream(
      { project_id: projectId, message: msg, session_id: sessionId },
      {
        onChunk: text => {
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantId
                ? { ...m, content: m.content + text, agentLabel }
                : m
            )
          )
        },
        onTool: name => setStatusLine(`Querying ${name}...`),
        onAgent: name => {
          agentLabel = name
          setStatusLine(`[${name}] thinking...`)
        },
        onFileEdit: proposal => {
          if (acceptAll) {
            applyEdit(proposal)
            addSystem(`Applied: ${proposal.file_path}`)
          } else {
            setPendingEdit(proposal)
          }
        },
        onDone: () => {
          setMessages(prev =>
            prev.map(m => (m.id === assistantId ? { ...m, streaming: false } : m))
          )
          setBusy(false)
          setStatusLine('')
          listSessions(projectId).then(setSessions).catch(() => {})
          inputRef.current?.focus()
        },
        onError: err => {
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantId
                ? { ...m, content: `Error: ${err}`, streaming: false }
                : m
            )
          )
          setBusy(false)
          setStatusLine('')
        },
      }
    )
  }

  // ── File edits ────────────────────────────────────────────────────────────

  function applyEdit(proposal: FileEditProposal) {
    fetch('/api/v1/files/apply-edit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(proposal),
    })
      .then(r => {
        if (!r.ok) throw new Error(r.statusText)
        addSystem(`Written: ${proposal.file_path}`)
      })
      .catch(e => addSystem(`Write failed: ${e.message}`))
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function addMessage(role: 'user' | 'assistant', content: string) {
    setMessages(prev => [...prev, { id: Date.now().toString(), role, content }])
  }

  function addSystem(content: string) {
    setMessages(prev => [...prev, { id: Date.now().toString(), role: 'system', content }])
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="ide-outer">
      <div className="ide-panels">
        {/* Left panel */}
        <aside
          className="left-panel"
          ref={leftPanelRef}
          style={{ width: leftWidth, flexShrink: 0 }}
        >
          <div className="sidebar-header">
            <span className="logo">Code LM</span>
            <span className={`dot ${backendOk ? 'green' : 'red'}`} title={backendOk ? 'Backend connected' : 'Backend offline'} />
          </div>

          <div className="panel-tabs">
            <button className={`panel-tab ${leftTab === 'files' ? 'active' : ''}`} onClick={() => setLeftTab('files')}>Files</button>
            <button className={`panel-tab ${leftTab === 'chats' ? 'active' : ''}`} onClick={() => setLeftTab('chats')}>Chats</button>
          </div>

          {leftTab === 'files' && (
            <div key={treeKey} style={{ zoom: leftZoom, flex: 1, minHeight: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
              <FileTreePanel rootPath={rootPath} onFileOpen={handleFileOpen} onRefresh={refreshTree} pushUndo={pushUndoOp} />
            </div>
          )}

          {leftTab === 'chats' && (
            <>
              <div className="sessions-label">Chats</div>
              <div className="sessions-list">
                {sessions.map(s => (
                  <div
                    key={s.id}
                    className={`session-item ${s.id === currentSessionId ? 'active' : ''}`}
                    onClick={() => switchSession(s.id)}
                  >
                    <span className="session-title">{s.title || 'New chat'}</span>
                    <button
                      className="delete-btn"
                      onClick={e => { e.stopPropagation(); askDeleteSession(s.id) }}
                      title="Delete chat"
                    >&#10005;</button>
                  </div>
                ))}
              </div>
              <button className="new-chat-btn" onClick={newSession}>+ New Chat</button>
            </>
          )}
        </aside>

        {/* Drag handle: left | mid/chat */}
        <div
          className="panel-divider panel-divider-v"
          onMouseDown={e => {
            isDraggingLeft.current = true
            dragStartX.current = e.clientX
            dragStartW.current = leftWidth
            document.body.style.cursor = 'col-resize'
            document.body.style.userSelect = 'none'
          }}
        />

        {/* File content panel (middle) */}
        {openFile && (
          <>
            <div
              ref={midPanelRef}
              style={{ width: midWidth, flexShrink: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}
            >
              <FileContentPanel file={openFile} onClose={() => setOpenFile(null)} fontSize={midFontSize} />
            </div>
            <div
              className="panel-divider panel-divider-v"
              onMouseDown={e => {
                isDraggingMid.current = true
                dragStartX.current = e.clientX
                dragStartW.current = midWidth
                document.body.style.cursor = 'col-resize'
                document.body.style.userSelect = 'none'
              }}
            />
          </>
        )}

        {/* Main chat area */}
        <main className="chat-area" ref={chatPanelRef}>
          {/* Toolbar */}
          <div className="toolbar">
            <div className="toolbar-title">{projectName}</div>
            <div className="toolbar-actions">
              {auth?.apiKey ? (
                <div className="auth-indicator" onClick={() => setShowAuth(true)} title="API Key connected">
                  <span className="auth-dot connected" />
                  <span className="auth-label">My Key</span>
                </div>
              ) : (
                <div className="credits-indicator" onClick={() => setShowAuth(true)} title="Credits balance">
                  <span className="credits-icon">{'\uD83D\uDCB3'}</span>
                  <span className="credits-label">{credits.balance} cr</span>
                </div>
              )}
              <button className="toolbar-btn" onClick={() => setShowHelp(true)} title="Help">?</button>
              <button className="toolbar-btn" onClick={() => setShowSettings(true)} title="Settings">&#9881;</button>
              <button
                className={`toolbar-btn${terminalOpen ? ' toolbar-btn-active' : ''}`}
                onClick={() => setTerminalOpen(v => !v)}
                title="Terminal (Ctrl+T)"
              >&#9000;</button>
            </div>
          </div>

          <div className="messages">
            {messages.map(m => (
              <MessageBubble key={m.id} message={m} />
            ))}
            <div ref={messagesEndRef} />
          </div>

          {statusLine && <div className="status-line">{statusLine}</div>}

          <div className="input-bar">
            <div className="input-wrapper">
              {slashSuggestions.length > 0 && (
                <div className="slash-dropdown">
                  {slashSuggestions.map((s, i) => (
                    <div
                      key={s.command}
                      className={`slash-item ${i === selectedSuggestion ? 'slash-item-selected' : ''}`}
                      onMouseDown={e => { e.preventDefault(); selectSlashCommand(s.command) }}
                      onMouseEnter={() => setSelectedSuggestion(i)}
                    >
                      <span className="slash-cmd">{s.command}</span>
                      <span className="slash-desc">{s.description}</span>
                    </div>
                  ))}
                </div>
              )}
              <input
                ref={inputRef}
                className="chat-input"
                value={input}
                onChange={e => {
                  setInput(e.target.value)
                  updateSlashSuggestions(e.target.value)
                }}
                onKeyDown={handleInputKeyDown}
                placeholder="/full-scan, /auto-scan, /package-scan, or just ask..."
                disabled={busy}
              />
            </div>
            <button className="send-btn" onClick={send} disabled={busy || !input.trim()}>
              {busy ? '...' : 'Send'}
            </button>
          </div>
        </main>
      </div>

      {/* Terminal panel */}
      {terminalOpen && (
        <>
          <div
            className="panel-divider panel-divider-h"
            onMouseDown={e => {
              isDraggingTerm.current = true
              dragStartY.current = e.clientY
              dragStartH.current = termHeight
              document.body.style.cursor = 'row-resize'
              document.body.style.userSelect = 'none'
            }}
          />
          <TerminalPanel
            onClose={() => setTerminalOpen(false)}
            style={{ height: termHeight, flexShrink: 0 }}
          />
        </>
      )}

      {/* Settings modal */}
      {showSettings && (
        <div className="modal-overlay" onClick={() => setShowSettings(false)}>
          <div className="modal settings-modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <span className="modal-title">Settings</span>
              <button className="modal-close" onClick={() => setShowSettings(false)}>&#10005;</button>
            </div>
            <div className="modal-body">
              <div className="setting-row">
                <label>Font Size: {settings.fontSize}px</label>
                <input
                  type="range"
                  min={12}
                  max={20}
                  value={settings.fontSize}
                  onChange={e => setSettings(s => ({ ...s, fontSize: Number(e.target.value) }))}
                />
              </div>
              <div className="setting-row">
                <label>Interface Scale: {Math.round((SCALE_ZOOM[settings.uiScale] ?? 1) * 100)}%</label>
                <div className="scale-options">
                  {[1, 2, 3, 4, 5, 6].map(n => (
                    <button
                      key={n}
                      className={`scale-btn ${settings.uiScale === n ? 'scale-btn-active' : ''}`}
                      onClick={() => setSettings(s => ({ ...s, uiScale: n }))}
                    >
                      {n}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Help modal */}
      {showHelp && (
        <div className="modal-overlay" onClick={() => setShowHelp(false)}>
          <div className="modal help-modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <span className="modal-title">Code LM — AI Software Architect</span>
              <button className="modal-close" onClick={() => setShowHelp(false)}>&#10005;</button>
            </div>
            <div className="modal-body help-body">
              <section>
                <h3>Getting Started</h3>
                <ol>
                  <li>Open your project folder (File &gt; Open Project)</li>
                  <li>Run <code>/full-scan</code> to index your codebase</li>
                  <li>Ask anything about your code</li>
                </ol>
              </section>
              <section>
                <h3>Chat Commands</h3>
                <div className="help-commands">
                  <div><code>/full-scan</code><span>Index your entire project</span></div>
                  <div><code>/auto-scan &lt;hint&gt;</code><span>Scan only what's relevant to your question</span></div>
                  <div><code>/package-scan</code><span>Scan a specific directory</span></div>
                </div>
              </section>
              <section>
                <h3>What Code LM can do</h3>
                <ul>
                  <li>Find bugs and explain root causes</li>
                  <li>Implement new features in your existing code</li>
                  <li>Analyze architecture and detect violations</li>
                  <li>Suggest refactoring with your approval</li>
                </ul>
              </section>
              <section>
                <h3>File Edits</h3>
                <p>Every code change requires your approval before being written to disk. Use "Accept", "Accept All", or "Reject" in the diff view.</p>
              </section>
            </div>
          </div>
        </div>
      )}

      {/* Auth modal */}
      {showAuth && (
        <AuthModal
          auth={auth}
          onSaveKey={(config) => { saveAuth(config); setAuth(config); setShowAuth(false) }}
          onDisconnect={() => { clearAuth(); setAuth(null); setShowAuth(false) }}
          credits={credits}
          onCreditsChange={(c) => { saveCredits(c); setCredits(c) }}
          onClose={() => setShowAuth(false)}
        />
      )}

      {/* Diff dialog */}
      {pendingEdit && (
        <DiffDialog
          proposal={pendingEdit}
          onAccept={() => {
            applyEdit(pendingEdit)
            setPendingEdit(null)
          }}
          onAcceptAll={() => {
            applyEdit(pendingEdit)
            setAcceptAll(true)
            setPendingEdit(null)
            addSystem('Accept all enabled — remaining edits in this chat will be applied automatically.')
          }}
          onReject={() => {
            addSystem(`Rejected: ${pendingEdit.file_path}`)
            setPendingEdit(null)
          }}
        />
      )}

      {/* Confirm delete chat modal */}
      {confirmDeleteId && (
        <div className="modal-overlay" onClick={() => setConfirmDeleteId(null)}>
          <div className="modal confirm-modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <span className="modal-title">Delete Chat</span>
              <button className="modal-close" onClick={() => setConfirmDeleteId(null)}>&#10005;</button>
            </div>
            <div className="modal-body">
              <p className="confirm-text">Delete this chat and all its messages? This cannot be undone.</p>
              <div className="confirm-actions">
                <button className="confirm-cancel-btn" onClick={() => setConfirmDeleteId(null)}>Cancel</button>
                <button className="confirm-delete-btn" onClick={confirmDeleteSession}>Delete</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── MessageBubble ─────────────────────────────────────────────────────────────

function MessageBubble({ message }: { message: DisplayMessage }) {
  const cls = `message message-${message.role}`
  return (
    <div className={cls}>
      {message.role === 'assistant' && (
        <div className="message-header">
          <span className="sender">Code LM</span>
          {message.agentLabel && (
            <span className={`agent-badge agent-${message.agentLabel}`}>{message.agentLabel}</span>
          )}
          {message.streaming && <span className="typing-dot" />}
        </div>
      )}
      {message.role === 'user' && <div className="message-header"><span className="sender">You</span></div>}
      <pre className="message-content">{message.content}</pre>
    </div>
  )
}

// ── AuthModal ─────────────────────────────────────────────────────────────────

function AuthModal({
  auth,
  onSaveKey,
  onDisconnect,
  credits,
  onCreditsChange,
  onClose,
}: {
  auth: AuthConfig | null
  onSaveKey: (config: AuthConfig) => void
  onDisconnect: () => void
  credits: CreditsConfig
  onCreditsChange: (c: CreditsConfig) => void
  onClose: () => void
}) {
  const [tab, setTab] = useState<'key' | 'credits'>(auth?.apiKey ? 'key' : 'credits')
  const [apiKey, setApiKey] = useState(auth?.apiKey || '')

  const purchaseCredits = (amount: number) => {
    onCreditsChange({ balance: credits.balance + amount })
  }

  return (
    <div className="modal-overlay">
      <div className="modal auth-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <span className="modal-title">Connect your AI</span>
          <button className="modal-close" onClick={onClose} title="Close">&#10005;</button>
        </div>

        <div className="auth-tabs">
          <button className={`auth-tab ${tab === 'key' ? 'active' : ''}`} onClick={() => setTab('key')}>
            My API Key
          </button>
          <button className={`auth-tab ${tab === 'credits' ? 'active' : ''}`} onClick={() => setTab('credits')}>
            Code LM Credits
          </button>
        </div>

        <div className="modal-body">
          {tab === 'key' && (
            <>
              <p style={{ fontSize: '12px', color: 'var(--text-dim)', marginBottom: '16px' }}>
                Use your own Anthropic (Claude) API key. No credits needed.
              </p>
              <input
                type="password"
                className="auth-input"
                placeholder="sk-ant-..."
                value={apiKey}
                onChange={e => setApiKey(e.target.value)}
                autoFocus
              />
              <a
                className="auth-link"
                href="https://console.anthropic.com"
                target="_blank"
                rel="noopener noreferrer"
              >
                Get your Anthropic API key &rarr;
              </a>
              <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
                <button
                  className="auth-connect-btn"
                  disabled={!apiKey.trim()}
                  onClick={() => onSaveKey({ apiKey: apiKey.trim() })}
                >
                  {auth?.apiKey ? 'Update Key' : 'Connect'}
                </button>
                {auth?.apiKey && (
                  <button className="auth-connect-btn" style={{ background: '#555' }} onClick={onDisconnect}>
                    Disconnect
                  </button>
                )}
              </div>
            </>
          )}

          {tab === 'credits' && (
            <>
              <div className="credits-balance">
                <div className="credits-amount">{'\uD83D\uDCB3'} {credits.balance}</div>
                <div className="credits-unit">credits</div>
                <div className={`credits-status ${credits.balance > 0 ? 'ok' : 'empty'}`}>
                  {credits.balance > 0 ? 'Credits active' : 'No credits — top up to start chatting'}
                </div>
              </div>
              <div className="credits-plans">
                <button className="credits-plan-btn" onClick={() => purchaseCredits(500)}>
                  <span className="plan-price">$5</span>
                  <span className="plan-credits">500 credits</span>
                </button>
                <button className="credits-plan-btn" onClick={() => purchaseCredits(1200)}>
                  <span className="plan-price">$10</span>
                  <span className="plan-credits">1,200 credits</span>
                </button>
                <button className="credits-plan-btn" onClick={() => purchaseCredits(3500)}>
                  <span className="plan-price">$25</span>
                  <span className="plan-credits">3,500 credits</span>
                </button>
              </div>
              <div className="credits-note">
                Credits are consumed per message. 1 message = 2 credits.
              </div>
              <button
                className="credits-start-btn"
                disabled={credits.balance <= 0}
                onClick={onClose}
              >
                Start chatting {'\u2192'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
