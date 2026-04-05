import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  canSendMessage,
  chatStream,
  clearAuth,
  createSession,
  CREDIT_PLANS,
  deleteProjectKnowledge,
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
  role: 'user' | 'assistant' | 'system' | 'file-edit'
  content: string
  agentLabel?: string
  streaming?: boolean
  fileEdit?: FileEditProposal
}

interface FileChange {
  id: string
  session_id: string
  file_path: string
  action: 'create' | 'update' | 'delete'
  summary: string
  completed: boolean
  created_at: string
}

interface TodoItem {
  id: string
  session_id: string
  text: string
  completed: boolean
  created_at: string
}

interface CodeLMSettings {
  fontSize: number
  uiScale: number  // 1–6, where 4 = 100% (default)
}

const SCALE_ZOOM = [0, 0.65, 0.75, 0.85, 1.0, 1.15, 1.35] // index 0 unused

// ── Scan shelf item ───────────────────────────────────────────────────────────

interface ScanItem {
  id: string
  type: 'file' | 'package'
  name: string
  path: string
}

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

// Apply scale immediately at module load to avoid layout flash.
// We temporarily apply zoom to <html> because #app-root doesn't exist yet.
// The IDE useEffect moves it to #app-root (and clears html zoom) before the
// first paint, so xterm is never mounted while html-level zoom is active.
;(function applyInitialScale() {
  const s = loadSettings()
  const scale = SCALE_ZOOM[s.uiScale] ?? 1.0
  if (scale !== 1.0) {
    document.documentElement.style.zoom = String(scale)
  }
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
  refreshSignal?: number
  onScanRequest?: (cmd: string) => void
  /** CSS scale factor applied to #app-root — needed to correct fixed-position
   *  context menu coords when transform:scale() is active. */
  menuScale?: number
}

function FileTreePanel({ rootPath, onFileOpen, onRefresh, pushUndo, refreshSignal, onScanRequest, menuScale = 1.0 }: FileTreePanelProps) {
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
  const lastSelectedPathRef = useRef<string | null>(null)
  const rightDragRef = useRef(false)

  // When #app-root has transform:scale(), position:fixed children are positioned
  // relative to that container, not the viewport. Divide clientX/Y by the scale
  // so the menu appears under the pointer.
  function menuCoords(clientX: number, clientY: number) {
    return { x: clientX / menuScale, y: clientY / menuScale }
  }

  useEffect(() => {
    fetchFileTree(rootPath)
      .then(t => { setTree(t); setExpanded(new Set([rootPath])) })
      .catch(() => setError('Could not load files'))
  }, [rootPath])

  // Refresh tree data without resetting expanded state
  useEffect(() => {
    if (!refreshSignal) return
    fetchFileTree(rootPath)
      .then(t => setTree(t))
      .catch(() => {/* ignore refresh errors */})
  }, [refreshSignal])

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
      rightDragRef.current = false
      if (hadSelection) {
        const c = menuCoords(e.clientX, e.clientY)
        setBulkMenu({ x: c.x, y: c.y })
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

  /** Return all visible item paths in DOM order (for Shift+Click range selection). */
  function getVisiblePaths(): string[] {
    if (!treeBodyRef.current) return []
    const allItems = Array.from(treeBodyRef.current.querySelectorAll<HTMLElement>('[data-tree-path]'))
    return allItems.map(el => el.getAttribute('data-tree-path') || '').filter(Boolean)
  }

  function handleItemClick(e: React.MouseEvent, path: string, isDir: boolean) {
    if (e.ctrlKey || e.metaKey) {
      // Ctrl+Click: toggle this single item
      e.preventDefault()
      setSelectedPaths(prev => {
        const next = new Set(prev)
        if (next.has(path)) next.delete(path)
        else next.add(path)
        selectedPathsRef.current = next
        return next
      })
      lastSelectedPathRef.current = path
    } else if (e.shiftKey) {
      // Shift+Click: select range from lastSelected to this item
      e.preventDefault()
      const visible = getVisiblePaths()
      const lastIdx = lastSelectedPathRef.current ? visible.indexOf(lastSelectedPathRef.current) : -1
      const thisIdx = visible.indexOf(path)
      if (lastIdx === -1) {
        // No previous selection — select from top to clicked
        const rangeEnd = thisIdx >= 0 ? thisIdx : 0
        const next = new Set(visible.slice(0, rangeEnd + 1))
        selectedPathsRef.current = next
        setSelectedPaths(next)
      } else {
        const [lo, hi] = lastIdx <= thisIdx ? [lastIdx, thisIdx] : [thisIdx, lastIdx]
        const next = new Set(visible.slice(lo, hi + 1))
        selectedPathsRef.current = next
        setSelectedPaths(next)
      }
      lastSelectedPathRef.current = path
    } else {
      // Plain click: normal behavior
      setSelectedPaths(new Set())
      selectedPathsRef.current = new Set()
      lastSelectedPathRef.current = null
      if (isDir) toggle(path)
      else onFileOpen?.(path)
    }
  }

  function handleRightClick(e: React.MouseEvent, path: string, isDir: boolean) {
    if (e.button === 2 && (e.ctrlKey || e.shiftKey)) {
      e.preventDefault()
      e.stopPropagation()
      handleItemClick(e, path, isDir)
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
            data-tree-path={node.path}
            className={`tree-item tree-dir${selectedPaths.has(node.path) ? ' tree-item-selected' : ''}`}
            style={{ paddingLeft: `${8 + indent}px` }}
            onClick={e => handleItemClick(e, node.path, true)}
            onMouseDown={e => handleRightClick(e, node.path, true)}
            onContextMenu={e => {
              if (rightDragRef.current || e.ctrlKey || e.shiftKey) return
              e.preventDefault()
              e.stopPropagation()
              const c = menuCoords(e.clientX, e.clientY)
              setCtxMenu({ x: c.x, y: c.y, node })
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
        data-tree-path={node.path}
        className={`tree-item tree-file${selectedPaths.has(node.path) ? ' tree-item-selected' : ''}`}
        style={{ paddingLeft: `${8 + indent}px` }}
        title={node.path}
        onClick={e => handleItemClick(e, node.path, false)}
        onMouseDown={e => handleRightClick(e, node.path, false)}
        onContextMenu={e => {
          if (rightDragRef.current || e.ctrlKey || e.shiftKey) return
          e.preventDefault()
          e.stopPropagation()
          const c = menuCoords(e.clientX, e.clientY)
          setCtxMenu({ x: c.x, y: c.y, node })
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
          if (e.button === 0 && (e.target as Element).closest('.tree-item')) return
          if (e.button !== 0 && e.button !== 2) return
          e.preventDefault()
          if (e.button === 2) rightDragRef.current = true
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
            {ctxMenu.node.type === 'dir' && (
              <div className="ctx-item" onClick={() => {
                onScanRequest?.(`/package-scan ${ctxMenu.node.path}`)
                setCtxMenu(null)
              }}>Package scan</div>
            )}
            {ctxMenu.node.type === 'file' && (
              <div className="ctx-item" onClick={() => {
                onScanRequest?.(`/auto-scan ${ctxMenu.node.path}`)
                setCtxMenu(null)
              }}>Auto scan</div>
            )}
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

// ── ScanShelf ─────────────────────────────────────────────────────────────────

function ScanShelf({
  items,
  activeIds,
  onToggle,
  onRemove,
}: {
  items: ScanItem[]
  activeIds: Set<string>
  onToggle: (id: string) => void
  onRemove: (id: string) => void
}) {
  const shelfRef = useRef<HTMLDivElement>(null)

  // Arrow key navigation across chips
  function handleKeyDown(e: React.KeyboardEvent<HTMLDivElement>, id: string) {
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault()
      onToggle(id)
      return
    }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault()
      const chips = Array.from(shelfRef.current?.querySelectorAll<HTMLElement>('.scan-chip') ?? [])
      const idx = chips.findIndex(el => el === e.currentTarget)
      const next = chips[idx + (e.key === 'ArrowRight' ? 1 : -1)]
      next?.focus()
    }
  }

  return (
    <div className="scan-shelf">
      <span className="scan-shelf-label">CONTEXT</span>
      <div className="scan-shelf-items" ref={shelfRef}>
        {items.map(item => (
          <div
            key={item.id}
            className={`scan-chip${activeIds.has(item.id) ? ' scan-chip-active' : ''}`}
            onClick={() => onToggle(item.id)}
            onKeyDown={e => handleKeyDown(e, item.id)}
            title={item.path}
            tabIndex={0}
            role="button"
            aria-pressed={activeIds.has(item.id)}
          >
            <span className="scan-chip-icon">{item.type === 'file' ? '\uD83D\uDCC4' : '\uD83D\uDCC1'}</span>
            <span className="scan-chip-name">{item.name}</span>
            <span
              className="scan-chip-remove"
              role="button"
              tabIndex={-1}
              onClick={e => { e.stopPropagation(); onRemove(item.id) }}
            >&#215;</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── ThinkingIndicator ────────────────────────────────────────────────────────

const THINKING_VERBS = [
  'Analyzing', 'Reading', 'Planning', 'Searching', 'Tracing',
  'Mapping', 'Checking', 'Reasoning', 'Scanning', 'Inspecting',
  'Connecting', 'Resolving', 'Indexing', 'Exploring', 'Thinking',
]

function ThinkingIndicator({ agent, tool }: { agent?: string; tool?: string }) {
  const [verbIdx, setVerbIdx] = useState(0)
  const [dotCount, setDotCount] = useState(1)
  const [displayedVerb, setDisplayedVerb] = useState('')
  const [isTyping, setIsTyping] = useState(false)
  const [currentVerb, setCurrentVerb] = useState(THINKING_VERBS[0])
  const [burst, setBurst] = useState(false)
  const particleOffsets = useRef<Array<{tx: number, ty: number}>>([])

  useEffect(() => {
    const verbTimer = setInterval(() => {
      setVerbIdx(i => (i + 1) % THINKING_VERBS.length)
    }, 900)
    const dotTimer = setInterval(() => {
      setDotCount(d => (d % 3) + 1)
    }, 400)
    return () => { clearInterval(verbTimer); clearInterval(dotTimer) }
  }, [])

  // Generate random offsets for particles
  useEffect(() => {
    particleOffsets.current = Array.from({ length: 5 }).map(() => ({
      tx: (Math.random() - 0.5) * 30,
      ty: -10 - Math.random() * 20
    }))
  }, [])

  // When verbIdx changes, start typing animation for the new verb
  useEffect(() => {
    const newVerb = tool ? `Querying ${tool}` : THINKING_VERBS[verbIdx]
    if (newVerb === currentVerb) return

    setIsTyping(true)
    setCurrentVerb(newVerb)
    setDisplayedVerb('')

    let i = 0
    const interval = setInterval(() => {
      setDisplayedVerb(newVerb.slice(0, i + 1))
      i++
      if (i >= newVerb.length) {
        clearInterval(interval)
        setIsTyping(false)
        setBurst(true)
        setTimeout(() => setBurst(false), 800)
      }
    }, 80) // typing speed

    return () => clearInterval(interval)
  }, [verbIdx, tool, currentVerb])

  // If tool changes, we need to update current verb immediately (no typing?)
  // For now, treat tool as immediate.

  const verb = tool ? `Querying ${tool}` : displayedVerb || currentVerb
  const dots = '.'.repeat(dotCount)
  const label = agent && agent !== 'main' ? `[${agent}] ` : ''

  return (
    <div className="thinking-indicator">
      <span className="thinking-agent">{label}</span>
      <span className="thinking-verb">{verb}</span>
      <span className="thinking-dots">{dots}</span>
      <span className={`thinking-particles ${burst ? 'thinking-burst' : ''}`} aria-hidden="true">
        {Array.from({ length: 5 }).map((_, i) => (
          <span
            key={i}
            className="thinking-particle"
            style={{
              animationDelay: `${i * 0.18}s`,
              ...(burst ? {
                '--tx': `${particleOffsets.current[i]?.tx || 0}px`,
                '--ty': `${particleOffsets.current[i]?.ty || -10}px`
              } : {})
            }}
          />
        ))}
      </span>
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
  const [applyBusy, setApplyBusy] = useState(false)
  const [scanBusy, setScanBusy] = useState(false)
  const [showChanges, setShowChanges] = useState(false)
  const [fileChanges, setFileChanges] = useState<FileChange[]>([])
  const [todos, setTodos] = useState<TodoItem[]>([])
  const [changesBadge, setChangesBadge] = useState(0)
  const [activeAgent, setActiveAgent] = useState<string | undefined>(undefined)
  const [activeTool, setActiveTool] = useState<string | undefined>(undefined)
  const [newChatSuggestion, setNewChatSuggestion] = useState<string | null>(null)

  // Settings
  const [settings, setSettings] = useState<CodeLMSettings>(loadSettings)
  const [showSettings, setShowSettings] = useState(false)
  const [settingsTab, setSettingsTab] = useState<'general' | 'project' | 'account'>('general')
  const [settingsApiKey, setSettingsApiKey] = useState('')
  const [settingsProvider, setSettingsProvider] = useState<'anthropic' | 'deepseek'>('anthropic')
  const [settingsAccountTab, setSettingsAccountTab] = useState<'key' | 'credits'>('credits')
  const [clearKnowledgeConfirm, setClearKnowledgeConfirm] = useState(false)
  const [clearKnowledgeLoading, setClearKnowledgeLoading] = useState(false)

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
  const [credits, setCredits] = useState<CreditsConfig>(loadCredits)

  // Delete chat confirmation
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)

  // Tree refresh key
  const [treeKey, setTreeKey] = useState(0)

  // Scan shelf
  const [scanShelf, setScanShelf] = useState<ScanItem[]>([])
  const [activeScanIds, setActiveScanIds] = useState<Set<string>>(new Set())

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

  // Tracks current CSS zoom factor for use in resize drag handlers
  // (those handlers close over an empty dep array so can't read state directly).
  const uiScaleRef = useRef(SCALE_ZOOM[settings.uiScale] ?? 1.0)

  // Apply UI scale with CSS zoom on #app-root.
  // CSS zoom (unlike transform:scale) affects actual layout, so elements fill
  // the viewport correctly at every scale level.
  // We apply it to #app-root, not <html>, so xterm canvas coordinate mapping
  // is handled via a counter-zoom wrapper on the terminal (see render below).
  useEffect(() => {
    document.documentElement.style.setProperty('--font-size', `${settings.fontSize}px`)
    const scale = SCALE_ZOOM[settings.uiScale] ?? 1.0
    uiScaleRef.current = scale
    // Clear the temporary html-level zoom set by the IIFE before React mounted.
    document.documentElement.style.zoom = ''
    const root = document.getElementById('app-root')
    if (root) {
      root.style.zoom = scale === 1.0 ? '' : String(scale)
      // Clear any legacy transform-based values from older code.
      root.style.transform = ''
      root.style.transformOrigin = ''
      root.style.width = ''
      root.style.height = ''
    }
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
      // e.clientX/Y are viewport pixels. Panel widths are CSS pixels inside
      // the zoomed #app-root, so we divide the delta by the current scale.
      const s = uiScaleRef.current
      if (isDraggingLeft.current) {
        const delta = (e.clientX - dragStartX.current) / s
        setLeftWidth(Math.max(120, Math.min(600, dragStartW.current + delta)))
      }
      if (isDraggingMid.current) {
        const available = window.innerWidth / s - leftWidth - 8;
        const chatMinPx = Math.max(320, Math.floor(available * 0.35));
        const maxMid = available - chatMinPx;
        const delta = (e.clientX - dragStartX.current) / s;
        setMidWidth(Math.max(200, Math.min(maxMid, dragStartW.current + delta)));
      }
      if (isDraggingTerm.current) {
        // Terminal uses a counter-zoom wrapper so its logical pixels = viewport pixels.
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
      if (leftPanelRef.current?.contains(t)) {
        e.preventDefault()
        const delta = e.deltaY < 0 ? 10 : -10
        setLeftWidth(w => Math.max(120, Math.min(600, w + delta)))
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

  // Constrain middle panel width when left panel or file content changes
  useEffect(() => {
    if (!openFile) return;
    const s = uiScaleRef.current;
    const available = window.innerWidth / s - leftWidth - 8;
    const chatMinPx = Math.max(320, Math.floor(available * 0.35));
    const maxMid = available - chatMinPx;
    setMidWidth(w => Math.max(200, Math.min(maxMid, w)));
  }, [openFile, leftWidth]);

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
    setShowChanges(false)
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
    refreshChanges(sessionId)
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
        folder_path: mode === 'folder' ? hint : undefined,
        entry_point: mode === 'smart' ? hint : undefined,
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
      // Cap file panel to 55% of available space so chat always has room (min 320px)
      const available = window.innerWidth - leftWidth - 12
      const chatMinPx = Math.max(320, Math.floor(available * 0.35))
      setMidWidth(Math.min(Math.floor(available * 0.65), available - chatMinPx))
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

    // Gate: need either own API key or sufficient balance
    if (!auth?.apiKey) {
      if (!canSendMessage(credits.balance_usd)) {
        setSettingsAccountTab('credits')
        setSettingsTab('account')
        setShowSettings(true)
        return
      }
    }
    let msg = input.trim()
    setInput('')
    setSlashSuggestions([])

    // Run deferred scans for queued shelf items, then prepend context hints
    if (activeScanIds.size > 0) {
      const activeItems = scanShelf.filter(s => activeScanIds.has(s.id))
      for (const item of activeItems) {
        try {
          setStatusLine(`Scanning ${item.name}...`)
          await runScan(item.type === 'package' ? 'folder' : 'smart', item.path)
        } catch { /* ignore scan errors — AI can still use read_file */ }
      }
      setStatusLine('')
      const ctxPaths = activeItems.map(s => `${s.type === 'file' ? 'file' : 'package'}:${s.path}`)
      if (ctxPaths.length > 0) {
        msg = `[Scan context: ${ctxPaths.join(', ')}]\n${msg}`
      }
    }

    // Slash command handling
    if (msg.startsWith('/full-scan')) {
      addSystem('Running full scan...')
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

    setNewChatSuggestion(null)
    addMessage('user', msg)
    setBusy(true)

    const assistantId = crypto.randomUUID()
    setMessages(prev => [
      ...prev,
      { id: assistantId, role: 'assistant', content: '', streaming: true },
    ])

    let agentLabel = ''

    streamCancelRef.current = chatStream(
      { project_id: projectId, message: msg, session_id: sessionId },
      {
        onChunk: text => {
          setActiveTool(undefined)  // clear tool indicator once text is flowing
          setMessages(prev =>
            prev.map(m =>
              m.id === assistantId
                ? { ...m, content: m.content + text, agentLabel }
                : m
            )
          )
        },
        onTool: name => { setActiveTool(name); setStatusLine('') },
        onAgent: name => {
          agentLabel = name
          setActiveAgent(name)
          setActiveTool(undefined)
          setStatusLine('')
        },
        onCost: (_cost, newBalance) => {
          setCredits({ balance_usd: newBalance })
          saveCredits({ balance_usd: newBalance })
        },
        onFileEdit: proposal => {
          if (acceptAll) {
            applyEdit(proposal).catch(() => {})
            addSystem(`Applied: ${proposal.file_path}`)
          } else {
            setMessages(prev => [...prev, {
              id: crypto.randomUUID(),
              role: 'file-edit' as const,
              content: '',
              fileEdit: proposal,
            }])
          }
        },
        onTodosAdded: () => {
          refreshChanges(sessionId)
        },
        onSuggestNewChat: reason => {
          setNewChatSuggestion(reason)
        },
        onDone: () => {
          setMessages(prev =>
            prev.map(m => (m.id === assistantId ? { ...m, streaming: false } : m))
          )
          setBusy(false)
          setStatusLine('')
          setActiveAgent(undefined)
          setActiveTool(undefined)
          listSessions(projectId).then(setSessions).catch(() => {})
          refreshChanges(sessionId)
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
          setActiveAgent(undefined)
          setActiveTool(undefined)
          setStatusLine('')
        },
      }
    )
  }

  // ── Change tracking ───────────────────────────────────────────────────────

  async function refreshChanges(sid: string | null) {
    if (!sid) return
    try {
      const [changes, newTodos] = await Promise.all([
        fetch(`/api/v1/sessions/${sid}/changes`).then(r => r.json()),
        fetch(`/api/v1/sessions/${sid}/todos`).then(r => r.json()),
      ])
      setFileChanges(Array.isArray(changes) ? changes : [])
      setTodos(Array.isArray(newTodos) ? newTodos : [])
      const unresolved = (Array.isArray(newTodos) ? newTodos : []).filter((t: TodoItem) => !t.completed).length
      setChangesBadge(unresolved)
    } catch { /* ignore */ }
  }

  // ── File edits ────────────────────────────────────────────────────────────

  async function applyEdit(proposal: FileEditProposal) {
    setApplyBusy(true)
    try {
      const r = await fetch('/api/v1/files/apply-edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...proposal, session_id: currentSessionId }),
      })
      if (!r.ok) {
        const body = await r.json().catch(() => ({ detail: r.statusText }))
        throw new Error(body.detail || r.statusText)
      }
      addSystem(`Written: ${proposal.file_path}`)
      refreshChanges(currentSessionId)
    } catch (e: any) {
      addSystem(`Write failed: ${e.message}`)
      throw e
    } finally {
      setApplyBusy(false)
    }
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function addMessage(role: 'user' | 'assistant', content: string) {
    setMessages(prev => [...prev, { id: crypto.randomUUID(), role, content }])
  }

  function addSystem(content: string) {
    setMessages(prev => [...prev, { id: crypto.randomUUID(), role: 'system', content }])
  }

  // ── Wheel zoom for file tree ──────────────────────────────────────────────
  function handleLeftPanelWheel(e: React.WheelEvent) {
    if (e.ctrlKey) {
      e.preventDefault()
      e.stopPropagation()
      const delta = e.deltaY > 0 ? -0.1 : 0.1
      setLeftZoom(z => Math.max(0.5, Math.min(2.0, z + delta)))
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div id="app-root" className="ide-outer">
      <div className="ide-panels">
        {/* Left panel */}
        <aside
          className="left-panel"
          ref={leftPanelRef}
          style={{ width: leftWidth, flexShrink: 0 }}
          onWheel={handleLeftPanelWheel}
        >
          <div className="sidebar-header">
            <span className="logo">Code LM</span>
            <span className={`dot ${backendOk ? 'green' : 'red'}`} title={backendOk ? 'Backend connected' : 'Backend offline'} />
          </div>

          <div className="panel-tabs">
            <button className={`panel-tab ${leftTab === 'files' ? 'active' : ''}`} onClick={() => setLeftTab('files')}>Files</button>
            <button className={`panel-tab ${leftTab === 'chats' ? 'active' : ''}`} onClick={() => setLeftTab('chats')}>Chats</button>
          </div>

          {/* Files tab — always mounted to preserve expansion state */}
          <div style={{ fontSize: `${leftZoom * 100}%`, flex: 1, minHeight: 0, overflow: 'hidden', display: leftTab === 'files' ? 'flex' : 'none', flexDirection: 'column' }}>
            <FileTreePanel
              rootPath={rootPath}
              onFileOpen={handleFileOpen}
              onRefresh={refreshTree}
              pushUndo={pushUndoOp}
              refreshSignal={treeKey}
              menuScale={SCALE_ZOOM[settings.uiScale] ?? 1.0}
              onScanRequest={async (cmd) => {
                setLeftTab('chats')
                const isPackage = cmd.startsWith('/package-scan ')
                const isAuto    = cmd.startsWith('/auto-scan ')
                if (!isPackage && !isAuto) { setInput(cmd); setTimeout(() => inputRef.current?.focus(), 50); return }
                const rawPath = cmd.replace(isPackage ? '/package-scan ' : '/auto-scan ', '').trim()
                const name = rawPath.split(/[/\\]/).filter(Boolean).pop() || rawPath
                const id = btoa(rawPath).replace(/[^a-zA-Z0-9]/g, '').slice(0, 20)
                // Defer scan until the user submits their message — just queue to shelf
                setScanShelf(prev => prev.some(s => s.path === rawPath) ? prev : [
                  ...prev,
                  { id, type: isPackage ? 'package' : 'file', name, path: rawPath },
                ])
                setActiveScanIds(prev => new Set([...prev, id]))
                addSystem(`Queued for scan: ${name} — will index when you send your message`)
                setTimeout(() => inputRef.current?.focus(), 50)
              }}
            />
          </div>

          {/* Chats tab */}
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
                <div className="auth-indicator" onClick={() => { setSettingsApiKey(auth.apiKey || ''); setSettingsProvider(auth.provider || 'anthropic'); setSettingsAccountTab('key'); setSettingsTab('account'); setShowSettings(true) }} title="API Key connected">
                  <span className="auth-dot connected" />
                  <span className="auth-label">My Key</span>
                </div>
              ) : (
                <div className="credits-indicator" onClick={() => { setSettingsApiKey(''); setSettingsAccountTab('credits'); setSettingsTab('account'); setShowSettings(true) }} title="API budget balance">
                  <span className="credits-icon">{'\uD83D\uDCB3'}</span>
                  <span className="credits-label">${(credits.balance_usd ?? 0).toFixed(2)}</span>
                </div>
              )}
              <button
                className={`toolbar-btn changes-btn${showChanges ? ' toolbar-btn-active' : ''}`}
                onClick={() => setShowChanges(v => !v)}
                title="Session changes"
              >
                Changes{changesBadge > 0 && <span className="changes-badge">{changesBadge}</span>}
              </button>
              <button className="toolbar-btn" onClick={() => setShowHelp(true)} title="Help">?</button>
              <button className="toolbar-btn" onClick={() => setShowSettings(true)} title="Settings">&#9881;</button>
              <button
                className={`toolbar-btn${terminalOpen ? ' toolbar-btn-active' : ''}`}
                onClick={() => setTerminalOpen(v => !v)}
                title="Terminal (Ctrl+T)"
              >&#9000;</button>
            </div>
          </div>

          {showChanges && (
            <ChangesPanel
              fileChanges={fileChanges}
              todos={todos}
              onClose={() => setShowChanges(false)}
            />
          )}

          <div className="messages">
            {messages.map(m => (
              <MessageBubble
                key={m.id}
                message={m}
                onApplyEdit={applyEdit}
                onRejectEdit={p => addSystem(`Rejected: ${p.file_path}`)}
              />
            ))}
            <div ref={messagesEndRef} />
          </div>

          {newChatSuggestion && (
            <div className="new-chat-suggestion">
              <span className="new-chat-suggestion-icon">↗</span>
              <span className="new-chat-suggestion-text">New topic detected — a fresh chat gives better results.</span>
              <button
                className="new-chat-suggestion-btn"
                onClick={async () => { setNewChatSuggestion(null); await newSession() }}
              >New Chat</button>
              <button
                className="new-chat-suggestion-dismiss"
                onClick={() => setNewChatSuggestion(null)}
              >Continue here</button>
            </div>
          )}
          {busy && !statusLine && (
            <ThinkingIndicator agent={activeAgent} tool={activeTool} />
          )}
          {statusLine && <div className="status-line">{statusLine}</div>}

          {scanShelf.length > 0 && (
            <ScanShelf
              items={scanShelf}
              activeIds={activeScanIds}
              onToggle={id => setActiveScanIds(prev => {
                const next = new Set(prev)
                if (next.has(id)) next.delete(id)
                else next.add(id)
                return next
              })}
              onRemove={id => {
                setScanShelf(prev => prev.filter(s => s.id !== id))
                setActiveScanIds(prev => { const next = new Set(prev); next.delete(id); return next })
              }}
            />
          )}

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

      {/* Terminal panel — no zoom wrapper needed because Electron/Chromium's
          layout engine handles offsetWidth/Height correctly for CSS zoom
          (unlike transform:scale which breaks xterm coordinate math). */}
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
            style={{ height: termHeight }}
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
            <div className="auth-tabs">
              <button className={`auth-tab ${settingsTab === 'general' ? 'active' : ''}`} onClick={() => setSettingsTab('general')}>General</button>
              <button className={`auth-tab ${settingsTab === 'project' ? 'active' : ''}`} onClick={() => { setSettingsTab('project'); setClearKnowledgeConfirm(false) }}>Project</button>
              <button className={`auth-tab ${settingsTab === 'account' ? 'active' : ''}`} onClick={() => setSettingsTab('account')}>Account</button>
            </div>
            <div className="modal-body">
              {settingsTab === 'general' && (
                <>
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
                </>
              )}
              {settingsTab === 'project' && (
                <>
                  <div className="setting-row" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: '8px' }}>
                    <label style={{ fontWeight: 600 }}>Clear Project Knowledge</label>
                    <p style={{ fontSize: '12px', color: 'var(--text-dim)', margin: 0 }}>
                      Deletes all indexed data for this project — Neo4j graph, Qdrant vectors, memory and rules.
                      After clearing, run a full scan to rebuild from scratch.
                    </p>
                    {!clearKnowledgeConfirm ? (
                      <button
                        className="auth-connect-btn"
                        style={{ background: '#c0392b', marginTop: '4px' }}
                        onClick={() => setClearKnowledgeConfirm(true)}
                      >
                        Clear Knowledge
                      </button>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '4px', width: '100%' }}>
                        <p style={{ fontSize: '12px', color: '#e74c3c', margin: 0, fontWeight: 600 }}>
                          This cannot be undone. All indexed data for this project will be permanently deleted.
                        </p>
                        <div style={{ display: 'flex', gap: '8px' }}>
                          <button
                            className="auth-connect-btn"
                            style={{ background: '#c0392b' }}
                            disabled={clearKnowledgeLoading}
                            onClick={async () => {
                              setClearKnowledgeLoading(true)
                              try {
                                await deleteProjectKnowledge(projectId)
                                setClearKnowledgeConfirm(false)
                                setShowSettings(false)
                                setMessages([{ id: 'sys-cleared', role: 'system', content: 'Project knowledge cleared. Run /full-scan to reindex.' }])
                              } catch (e: any) {
                                setMessages(prev => [...prev, { id: crypto.randomUUID(), role: 'system', content: `Failed to clear: ${e.message}` }])
                              } finally {
                                setClearKnowledgeLoading(false)
                              }
                            }}
                          >
                            {clearKnowledgeLoading ? 'Clearing…' : 'Yes, delete everything'}
                          </button>
                          <button
                            className="auth-connect-btn"
                            style={{ background: '#555' }}
                            disabled={clearKnowledgeLoading}
                            onClick={() => setClearKnowledgeConfirm(false)}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                </>
              )}
              {settingsTab === 'account' && (
                <>
                  <div className="auth-tabs" style={{ marginBottom: '12px' }}>
                    <button className={`auth-tab ${settingsAccountTab === 'key' ? 'active' : ''}`} onClick={() => setSettingsAccountTab('key')}>My API Key</button>
                    <button className={`auth-tab ${settingsAccountTab === 'credits' ? 'active' : ''}`} onClick={() => setSettingsAccountTab('credits')}>Credits</button>
                  </div>
                  {settingsAccountTab === 'key' && (
                    <>
                      <div className="auth-tabs" style={{ marginBottom: '12px' }}>
                        <button
                          className={`auth-tab ${settingsProvider === 'anthropic' ? 'active' : ''}`}
                          onClick={() => setSettingsProvider('anthropic')}
                        >Anthropic (Claude)</button>
                        <button
                          className={`auth-tab ${settingsProvider === 'deepseek' ? 'active' : ''}`}
                          onClick={() => setSettingsProvider('deepseek')}
                        >DeepSeek</button>
                      </div>
                      <p style={{ fontSize: '12px', color: 'var(--text-dim)', marginBottom: '16px' }}>
                        {settingsProvider === 'anthropic'
                          ? 'Use your own Anthropic (Claude) API key. No credits needed.'
                          : 'Use your own DeepSeek API key. No credits needed.'}
                      </p>
                      <input
                        type="password"
                        className="auth-input"
                        placeholder={settingsProvider === 'anthropic' ? 'sk-ant-...' : 'sk-...'}
                        value={settingsApiKey}
                        onChange={e => setSettingsApiKey(e.target.value)}
                        autoFocus
                      />
                      {settingsProvider === 'anthropic' ? (
                        <a className="auth-link" href="https://console.anthropic.com" target="_blank" rel="noopener noreferrer">
                          Get your Anthropic API key &rarr;
                        </a>
                      ) : (
                        <a className="auth-link" href="https://platform.deepseek.com" target="_blank" rel="noopener noreferrer">
                          Get your DeepSeek API key &rarr;
                        </a>
                      )}
                      <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
                        <button
                          className="auth-connect-btn"
                          disabled={!settingsApiKey.trim()}
                          onClick={() => {
                            const cfg = { apiKey: settingsApiKey.trim(), provider: settingsProvider }
                            saveAuth(cfg); setAuth(cfg)
                          }}
                        >
                          {auth?.apiKey ? 'Update Key' : 'Connect'}
                        </button>
                        {auth?.apiKey && (
                          <button className="auth-connect-btn" style={{ background: '#555' }} onClick={() => { clearAuth(); setAuth(null) }}>
                            Disconnect
                          </button>
                        )}
                      </div>
                    </>
                  )}
                  {settingsAccountTab === 'credits' && (
                    <>
                      <div className="credits-balance">
                        <div className="credits-amount">{'\uD83D\uDCB3'} ${(credits.balance_usd ?? 0).toFixed(4)}</div>
                        <div className="credits-unit">API budget remaining</div>
                        <div className={`credits-status ${(credits.balance_usd ?? 0) > 0 ? 'ok' : 'empty'}`}>
                          {(credits.balance_usd ?? 0) > 0 ? 'Budget active' : 'Budget empty — top up to continue'}
                        </div>
                      </div>
                      <div className="credits-plans">
                        {CREDIT_PLANS.map(plan => (
                          <button
                            key={plan.price}
                            className="credits-plan-btn"
                            onClick={() => { const c = { balance_usd: (credits.balance_usd ?? 0) + plan.api_budget }; saveCredits(c); setCredits(c) }}
                          >
                            <span className="plan-price">${plan.price}</span>
                            <span className="plan-credits">${plan.api_budget.toFixed(2)} budget</span>
                          </button>
                        ))}
                      </div>
                      <div className="credits-note">
                        Billed per actual token usage. Current task finishes even if budget hits $0 (up to $1 buffer).
                      </div>
                    </>
                  )}
                </>
              )}
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

      {/* Diff dialog */}
      {pendingEdit && (
        <DiffDialog
          proposal={pendingEdit}
          loading={applyBusy}
          onAccept={async () => {
            await applyEdit(pendingEdit)
            setPendingEdit(null)
          }}
          onAcceptAll={async () => {
            await applyEdit(pendingEdit)
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

// ── ChangesPanel ──────────────────────────────────────────────────────────────

function ChangesPanel({
  fileChanges,
  todos,
  onClose,
}: {
  fileChanges: FileChange[]
  todos: TodoItem[]
  onClose: () => void
}) {
  const grouped = { create: [] as FileChange[], update: [] as FileChange[], delete: [] as FileChange[] }
  for (const c of fileChanges) {
    if (grouped[c.action]) grouped[c.action].push(c)
  }

  return (
    <div className="changes-panel">
      <div className="changes-panel-header">
        <span className="changes-panel-title">Session Changes</span>
        <button className="changes-panel-close" onClick={onClose}>&#10005;</button>
      </div>
      <div className="changes-panel-body">
        {fileChanges.length === 0 && todos.length === 0 && (
          <div className="changes-empty">No changes or todos yet in this session.</div>
        )}

        {fileChanges.length > 0 && (
          <div className="changes-section">
            <div className="changes-section-title">Files Changed</div>
            {(['create', 'update', 'delete'] as const).map(action =>
              grouped[action].length > 0 ? (
                <div key={action} className="changes-action-group">
                  <div className={`changes-action-label changes-action-${action}`}>
                    {action === 'create' ? '+ Created' : action === 'update' ? '~ Updated' : '- Deleted'}
                  </div>
                  {grouped[action].map(c => (
                    <div key={c.id} className="changes-file-row">
                      <span className="changes-file-path">{c.file_path.split('/').pop()}</span>
                      {c.summary && <span className="changes-file-summary">{c.summary}</span>}
                    </div>
                  ))}
                </div>
              ) : null
            )}
          </div>
        )}

        {todos.length > 0 && (
          <div className="changes-section">
            <div className="changes-section-title">Todos</div>
            {todos.map(t => (
              <div key={t.id} className={`changes-todo-row${t.completed ? '' : ' changes-todo-pending'}`}>
                {t.completed ? '✓' : '⚠'} {t.text}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── InlineEditCard ────────────────────────────────────────────────────────────

function InlineEditCard({
  proposal,
  onApply,
  onReject,
}: {
  proposal: FileEditProposal
  onApply: (p: FileEditProposal) => Promise<void>
  onReject: (p: FileEditProposal) => void
}) {
  const [status, setStatus] = useState<'pending' | 'applying' | 'accepted' | 'rejected'>('pending')

  async function handleAccept() {
    setStatus('applying')
    try {
      await onApply(proposal)
      setStatus('accepted')
    } catch {
      setStatus('pending')
    }
  }

  function handleReject() {
    onReject(proposal)
    setStatus('rejected')
  }

  return (
    <div className="inline-edit-card">
      <div className="inline-edit-header">
        <span className="inline-edit-icon">±</span>
        <span className="inline-edit-path">{proposal.file_path}</span>
      </div>
      {proposal.description && (
        <p className="inline-edit-desc">{proposal.description}</p>
      )}
      <div className="inline-edit-diff">
        {proposal.original_snippet && (
          <pre className="inline-edit-old">{proposal.original_snippet}</pre>
        )}
        <pre className="inline-edit-new">{proposal.new_snippet}</pre>
      </div>
      {status === 'pending' && (
        <div className="inline-edit-actions">
          <button className="btn-reject" onClick={handleReject}>Reject</button>
          <button className="btn-accept" onClick={handleAccept}>Accept</button>
        </div>
      )}
      {status === 'applying' && (
        <div className="inline-edit-status">Applying…</div>
      )}
      {status === 'accepted' && (
        <div className="inline-edit-status inline-edit-accepted">✓ Applied</div>
      )}
      {status === 'rejected' && (
        <div className="inline-edit-status inline-edit-rejected">✗ Rejected</div>
      )}
    </div>
  )
}

// ── MessageBubble ─────────────────────────────────────────────────────────────

function MessageBubble({
  message,
  onApplyEdit,
  onRejectEdit,
}: {
  message: DisplayMessage
  onApplyEdit?: (p: FileEditProposal) => Promise<void>
  onRejectEdit?: (p: FileEditProposal) => void
}) {
  if (message.role === 'file-edit' && message.fileEdit) {
    return (
      <InlineEditCard
        proposal={message.fileEdit}
        onApply={onApplyEdit!}
        onReject={onRejectEdit!}
      />
    )
  }

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
      {message.role === 'assistant'
        ? <div className="message-content markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>
        : <pre className="message-content">{message.content}</pre>
      }
    </div>
  )
}

