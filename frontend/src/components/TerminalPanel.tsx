/**
 * TerminalPanel — xterm.js terminal with multiple tabs and shell selection.
 * Each tab opens a PTY-backed WebSocket session to the backend.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'

interface Tab {
  id: string
  shell: string
  label: string
}

interface Session {
  ws: WebSocket
  term: Terminal
  fit: FitAddon
  ro: ResizeObserver
}

interface Props {
  onClose: () => void
  style?: React.CSSProperties
}

function wsBase() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/api/v1/terminal`
}

export default function TerminalPanel({ onClose, style }: Props) {
  const [tabs, setTabs] = useState<Tab[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [shells, setShells] = useState<string[]>([])
  const [available, setAvailable] = useState<boolean | null>(null) // null = loading
  const [showDropdown, setShowDropdown] = useState(false)

  const sessions = useRef<Map<string, Session>>(new Map())
  const pendingMount = useRef<Map<string, string>>(new Map())

  useEffect(() => {
    fetch('/api/v1/terminal/shells')
      .then(r => r.json())
      .then(d => {
        const sh: string[] = d.shells || []
        setShells(sh)
        setAvailable(d.available !== false && sh.length > 0)
        if (sh.length > 0) createTab(sh[0])
      })
      .catch(() => setAvailable(false))
  }, [])

  useEffect(() => {
    if (!activeId) return
    const s = sessions.current.get(activeId)
    if (s) requestAnimationFrame(() => s.fit.fit())
  }, [activeId])

  // Clean up all sessions on unmount
  useEffect(() => () => {
    sessions.current.forEach(s => {
      s.ws.close()
      s.term.dispose()
      s.ro.disconnect()
    })
  }, [])

  function createTab(shell?: string) {
    const id = `t${Date.now()}`
    const sh = shell || shells[0] || 'bash'
    const label = sh.split('/').pop() || sh
    pendingMount.current.set(id, sh)
    setTabs(prev => [...prev, { id, shell: sh, label }])
    setActiveId(id)
    setShowDropdown(false)
  }

  function closeTab(id: string) {
    const s = sessions.current.get(id)
    if (s) { s.ws.close(); s.term.dispose(); s.ro.disconnect() }
    sessions.current.delete(id)
    pendingMount.current.delete(id)
    setTabs(prev => {
      const next = prev.filter(t => t.id !== id)
      if (activeId === id) setActiveId(next.length ? next[next.length - 1].id : null)
      return next
    })
  }

  const mountRef = useCallback((id: string, el: HTMLDivElement | null) => {
    if (!el || sessions.current.has(id)) return
    const shell = pendingMount.current.get(id) || 'bash'

    const term = new Terminal({
      fontFamily: "'Cascadia Code','Fira Code','JetBrains Mono',monospace",
      fontSize: 13,
      cursorBlink: true,
      theme: {
        background: '#1a1a1a', foreground: '#cccccc', cursor: '#cccccc',
        selectionBackground: 'rgba(14,99,156,0.4)',
        black: '#1e1e1e', brightBlack: '#555555',
        red: '#f44747', brightRed: '#f44747',
        green: '#4ec9b0', brightGreen: '#4ec9b0',
        yellow: '#dcdcaa', brightYellow: '#dcdcaa',
        blue: '#569cd6', brightBlue: '#569cd6',
        magenta: '#c586c0', brightMagenta: '#c586c0',
        cyan: '#9cdcfe', brightCyan: '#9cdcfe',
        white: '#d4d4d4', brightWhite: '#ffffff',
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(el)
    requestAnimationFrame(() => fit.fit())

    const ws = new WebSocket(`${wsBase()}/ws?shell=${encodeURIComponent(shell)}`)
    ws.binaryType = 'arraybuffer'

    ws.onopen = () => {
      fit.fit()
      term.onResize(({ rows, cols }: { rows: number; cols: number }) => {
        if (ws.readyState === WebSocket.OPEN)
          ws.send(JSON.stringify({ type: 'resize', rows, cols }))
      })
    }
    ws.onmessage = e => {
      term.write(e.data instanceof ArrayBuffer ? new Uint8Array(e.data) : e.data as string)
    }
    ws.onclose = () => term.writeln('\r\n\x1b[2m[session ended]\x1b[0m')
    ws.onerror = () => term.writeln('\r\n\x1b[31m[connection error]\x1b[0m')

    term.onData((d: string) => {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(new TextEncoder().encode(d))
    })

    const ro = new ResizeObserver(() => fit.fit())
    ro.observe(el)

    sessions.current.set(id, { ws, term, fit, ro })
  }, [])

  return (
    <div className="terminal-panel" style={style}>
      <div className="terminal-header">
        <div className="terminal-tabs">
          {tabs.map(tab => (
            <div
              key={tab.id}
              className={`terminal-tab${tab.id === activeId ? ' active' : ''}`}
              onClick={() => setActiveId(tab.id)}
            >
              <span className="terminal-tab-label">{tab.label}</span>
              <button
                className="terminal-tab-close"
                onClick={e => { e.stopPropagation(); closeTab(tab.id) }}
              >&#215;</button>
            </div>
          ))}
          <div className="terminal-new-wrap">
            <button className="terminal-add-btn" onClick={() => createTab()} title="New terminal">+</button>
            <button
              className="terminal-dropdown-btn"
              onClick={() => setShowDropdown(v => !v)}
              title="Select shell"
            >&#9662;</button>
            {showDropdown && (
              <div className="terminal-shell-dropdown">
                {shells.map(sh => (
                  <div
                    key={sh}
                    className="terminal-shell-option"
                    onMouseDown={() => createTab(sh)}
                  >
                    {sh.split('/').pop()}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
        <button className="terminal-close-btn" onClick={onClose} title="Close terminal">&#10005;</button>
      </div>
      <div className="terminal-body">
        {available === false && (
          <div className="terminal-unavailable">
            <div className="terminal-unavail-icon">⌨</div>
            <div className="terminal-unavail-title">Terminal not available</div>
            <div className="terminal-unavail-msg">
              Could not connect to the terminal backend. Make sure the backend is running
              and try reopening the terminal.
            </div>
          </div>
        )}
        {available !== false && tabs.map(tab => (
          <div
            key={tab.id}
            ref={el => mountRef(tab.id, el)}
            style={{
              position: 'absolute',
              inset: 0,
              visibility: tab.id === activeId ? 'visible' : 'hidden',
            }}
          />
        ))}
      </div>
    </div>
  )
}
