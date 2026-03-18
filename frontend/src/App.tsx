import { useEffect, useRef, useState } from 'react'
import {
  chatStream,
  createSession,
  deleteSession,
  fetchFileTree,
  getMessages,
  getProjectStatus,
  listSessions,
  scanProject,
  type ChatMessage,
  type FileEditProposal,
  type Session,
} from './api/client'
import DiffDialog from './components/DiffDialog'
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
  interfaceScale: 'compact' | 'normal' | 'comfortable'
}

const DEFAULT_SETTINGS: CodeLMSettings = {
  fontSize: 14,
  interfaceScale: 'normal',
}

function loadSettings(): CodeLMSettings {
  try {
    const raw = localStorage.getItem('codelm_settings')
    if (raw) return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) }
  } catch { /* ignore */ }
  return { ...DEFAULT_SETTINGS }
}

function saveSettings(s: CodeLMSettings) {
  localStorage.setItem('codelm_settings', JSON.stringify(s))
}

// ── Slash command definitions ────────────────────────────────────────────────

const SLASH_COMMANDS = [
  { command: '/full-scan', description: 'Index entire project' },
  { command: '/auto-scan', description: 'AI finds context automatically' },
  { command: '/package-scan', description: 'Scan specific directory' },
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

function FileTreePanel({ rootPath }: { rootPath: string }) {
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')

  useEffect(() => {
    fetchFileTree(rootPath)
      .then(t => { setTree(t); setExpanded(new Set([rootPath])) })
      .catch(() => setError('Could not load files'))
  }, [rootPath])

  function toggle(path: string) {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  function renderNode(node: TreeNode, depth: number): React.ReactNode {
    const isOpen = expanded.has(node.path)
    const indent = depth * 14

    if (node.type === 'dir') {
      return (
        <div key={node.path}>
          <div
            className="tree-item tree-dir"
            style={{ paddingLeft: `${8 + indent}px` }}
            onClick={() => toggle(node.path)}
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
        className="tree-item tree-file"
        style={{ paddingLeft: `${8 + indent}px` }}
        title={node.path}
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
      <div className="tree-body">
        {tree.children.map(child => renderNode(child, 0))}
      </div>
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

  // Left panel tab
  const [leftTab, setLeftTab] = useState<'chats' | 'files'>('chats')

  // Slash command autocomplete
  const [slashSuggestions, setSlashSuggestions] = useState<typeof SLASH_COMMANDS>([])
  const [selectedSuggestion, setSelectedSuggestion] = useState(0)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const streamCancelRef = useRef<(() => void) | null>(null)

  // Apply settings to CSS
  useEffect(() => {
    document.documentElement.style.setProperty('--font-size', `${settings.fontSize}px`)
    document.documentElement.classList.remove('compact', 'comfortable')
    if (settings.interfaceScale !== 'normal') {
      document.documentElement.classList.add(settings.interfaceScale)
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

  async function removeSession(sessionId: string) {
    if (!confirm('Delete this chat and all its messages?')) return
    await deleteSession(sessionId)
    const updated = sessions.filter(s => s.id !== sessionId)
    setSessions(updated)
    if (sessionId === currentSessionId) {
      if (updated.length > 0) await switchSession(updated[0].id)
      else await newSession()
    }
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
    <div className="ide">
      {/* Left panel */}
      <aside className="left-panel">
        <div className="sidebar-header">
          <span className="logo">Code LM</span>
          <span className={`dot ${backendOk ? 'green' : 'red'}`} title={backendOk ? 'Backend connected' : 'Backend offline'} />
        </div>

        <div className="panel-tabs">
          <button className={`panel-tab ${leftTab === 'files' ? 'active' : ''}`} onClick={() => setLeftTab('files')}>Files</button>
          <button className={`panel-tab ${leftTab === 'chats' ? 'active' : ''}`} onClick={() => setLeftTab('chats')}>Chats</button>
        </div>

        {leftTab === 'files' && <FileTreePanel rootPath={rootPath} />}

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
                    onClick={e => { e.stopPropagation(); removeSession(s.id) }}
                    title="Delete chat"
                  >&#10005;</button>
                </div>
              ))}
            </div>
            <button className="new-chat-btn" onClick={newSession}>+ New Chat</button>
          </>
        )}
      </aside>

      {/* Main chat area */}
      <main className="chat-area">
        {/* Toolbar */}
        <div className="toolbar">
          <div className="toolbar-title">Code LM</div>
          <div className="toolbar-actions">
            <button className="toolbar-btn" onClick={() => setShowHelp(true)} title="Help">?</button>
            <button className="toolbar-btn" onClick={() => setShowSettings(true)} title="Settings">&#9881;</button>
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
                <label>Interface Scale</label>
                <div className="scale-options">
                  {(['compact', 'normal', 'comfortable'] as const).map(scale => (
                    <button
                      key={scale}
                      className={`scale-btn ${settings.interfaceScale === scale ? 'scale-btn-active' : ''}`}
                      onClick={() => setSettings(s => ({ ...s, interfaceScale: scale }))}
                    >
                      {scale.charAt(0).toUpperCase() + scale.slice(1)}
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
