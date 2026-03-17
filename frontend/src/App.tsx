import { useEffect, useRef, useState } from 'react'
import {
  chatStream,
  createSession,
  deleteSession,
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

// ── Config ────────────────────────────────────────────────────────────────────

// The project root is passed via URL param or env, falling back to a prompt.
// In Electron the main process sets ?root= on launch.
function getProjectConfig(): { projectId: string; rootPath: string } {
  const params = new URLSearchParams(window.location.search)
  const rootPath = params.get('root') || localStorage.getItem('engramai_root') || ''
  // Stable UUID from root path string
  const projectId = params.get('pid') || localStorage.getItem('engramai_pid') || ''
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

const SCAN_HELP = `EngramAI — AI Software Architect

Scan modes:
  /full-scan         — index entire project
  /auto-scan <hint>  — AI finds context automatically (default)
  /package-scan      — scan directory of current focus

Agents auto-assigned per message:
  [debugger]   — bugs, errors, root-cause analysis
  [codegen]    — implement, add, create new code
  [architect]  — design, structure, DRY, dependencies

File edits require your approval before writing to disk.
`

export default function App() {
  const { projectId, rootPath } = getProjectConfig()

  // Setup prompt if no config yet
  const [setupDone, setSetupDone] = useState(Boolean(projectId && rootPath))
  const [setupRoot, setSetupRoot] = useState('')

  if (!setupDone) {
    return (
      <div className="setup-screen">
        <h1>EngramAI</h1>
        <p>Enter the absolute path to your project root:</p>
        <input
          autoFocus
          value={setupRoot}
          onChange={e => setSetupRoot(e.target.value)}
          placeholder="/home/user/my-project"
          onKeyDown={e => {
            if (e.key === 'Enter' && setupRoot.trim()) {
              const root = setupRoot.trim()
              // derive a stable ID from the path
              const pid = btoa(root).replace(/[^a-zA-Z0-9]/g, '').slice(0, 32)
              localStorage.setItem('engramai_root', root)
              localStorage.setItem('engramai_pid', pid)
              window.location.search = `?root=${encodeURIComponent(root)}&pid=${pid}`
            }
          }}
        />
        <p className="hint">Press Enter to continue</p>
      </div>
    )
  }

  return <IDE projectId={projectId} rootPath={rootPath} />
}

// ── IDE ───────────────────────────────────────────────────────────────────────

function IDE({ projectId, rootPath }: { projectId: string; rootPath: string }) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<DisplayMessage[]>([
    { id: 'help', role: 'system', content: SCAN_HELP },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [statusLine, setStatusLine] = useState('')
  const [backendOk, setBackendOk] = useState(false)
  const [acceptAll, setAcceptAll] = useState(false)
  const [pendingEdit, setPendingEdit] = useState<FileEditProposal | null>(null)
  const [scanBusy, setScanBusy] = useState(false)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const streamCancelRef = useRef<(() => void) | null>(null)

  // ── Startup ─────────────────────────────────────────────────────────────────

  useEffect(() => {
    checkBackend()
  }, [])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function checkBackend() {
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
      addSystem('Backend not running. Start it: cd backend && python main.py')
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
      setMessages([{ id: 'help', role: 'system', content: SCAN_HELP }])
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
        setMessages([{ id: 'help', role: 'system', content: SCAN_HELP }])
      } else {
        setMessages(msgs.map((m, i) => ({ id: String(i), role: m.role, content: m.content })))
      }
    } catch {
      setMessages([{ id: 'help', role: 'system', content: SCAN_HELP }])
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
    setStatusLine(`Scanning (${mode})…`)
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

  // ── Send message ──────────────────────────────────────────────────────────────

  async function send() {
    if (busy || !input.trim()) return
    let msg = input.trim()
    setInput('')

    // Slash command handling
    if (msg.startsWith('/full-scan')) {
      addSystem('Starting full scan…')
      await runScan('full')
      const rest = msg.replace('/full-scan', '').trim()
      if (rest) { setInput(rest); setTimeout(send, 100) }
      return
    }
    if (msg.startsWith('/auto-scan')) {
      const hint = msg.replace('/auto-scan', '').trim() || undefined
      addSystem(`Auto-scan${hint ? `: ${hint}` : ''}…`)
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
        onTool: name => setStatusLine(`Querying ${name}…`),
        onAgent: name => {
          agentLabel = name
          setStatusLine(`[${name}] thinking…`)
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
          // Refresh sessions so title updates
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

  // ── File edits ────────────────────────────────────────────────────────────────

  function applyEdit(proposal: FileEditProposal) {
    // POST to backend to write file
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

  // ── Helpers ───────────────────────────────────────────────────────────────────

  function addMessage(role: 'user' | 'assistant', content: string) {
    setMessages(prev => [...prev, { id: Date.now().toString(), role, content }])
  }

  function addSystem(content: string) {
    setMessages(prev => [...prev, { id: Date.now().toString(), role: 'system', content }])
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  return (
    <div className="ide">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <span className="logo">EngramAI</span>
          <span className={`dot ${backendOk ? 'green' : 'red'}`} title={backendOk ? 'Backend connected' : 'Backend offline'} />
        </div>

        <div className="scan-buttons">
          <button className="scan-btn" onClick={() => runScan('full')} disabled={scanBusy}>
            Full Scan
          </button>
          <button className="scan-btn" onClick={() => {
            const hint = prompt('Entry point (class/file name):')
            if (hint !== null) runScan('smart', hint || undefined)
          }} disabled={scanBusy}>
            Auto Scan
          </button>
        </div>

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
              >✕</button>
            </div>
          ))}
        </div>

        <button className="new-chat-btn" onClick={newSession}>+ New Chat</button>
      </aside>

      {/* Main chat area */}
      <main className="chat-area">
        <div className="messages">
          {messages.map(m => (
            <MessageBubble key={m.id} message={m} />
          ))}
          <div ref={messagesEndRef} />
        </div>

        {statusLine && <div className="status-line">{statusLine}</div>}

        <div className="input-bar">
          <input
            ref={inputRef}
            className="chat-input"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
            placeholder="/full-scan · /auto-scan · /package-scan · or just ask…"
            disabled={busy}
          />
          <button className="send-btn" onClick={send} disabled={busy || !input.trim()}>
            {busy ? '…' : 'Send'}
          </button>
        </div>
      </main>

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
          <span className="sender">EngramAI</span>
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
