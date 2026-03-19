/**
 * FileContentPanel — editable file viewer.
 * Text files -> Monaco Editor (auto-saves every 1s after last change)
 * Images     -> <img> centered
 * Binary     -> placeholder
 */
import { useEffect, useRef } from 'react'
import Editor from '@monaco-editor/react'

const EXT_LANG: Record<string, string> = {
  js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
  py: 'python', java: 'java', kt: 'kotlin', swift: 'swift',
  go: 'go', rs: 'rust', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', c: 'c', h: 'c',
  cs: 'csharp', php: 'php', rb: 'ruby', scala: 'scala', dart: 'dart',
  json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'ini',
  xml: 'xml', html: 'html', htm: 'html', css: 'css', scss: 'scss', less: 'less',
  sh: 'shell', bash: 'shell', zsh: 'shell', sql: 'sql', graphql: 'graphql',
  md: 'markdown', mdx: 'markdown', tf: 'hcl', proto: 'protobuf',
  r: 'r', lua: 'lua', gradle: 'groovy',
}

export interface OpenFile {
  path: string
  type: 'text' | 'image' | 'binary'
  content: string
  mime?: string
  ext: string
  size: number
}

interface Props {
  file: OpenFile
  onClose: () => void
  fontSize?: number
}

function formatBytes(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 ** 2).toFixed(1)} MB`
}

export default function FileContentPanel({ file, onClose, fontSize = 13 }: Props) {
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const fileName = file.path.split(/[/\\]/).pop() || file.path
  const lang = EXT_LANG[file.ext.toLowerCase()] || 'plaintext'

  useEffect(() => () => {
    if (saveTimer.current) clearTimeout(saveTimer.current)
  }, [])

  function handleChange(value: string | undefined) {
    if (value === undefined) return
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => {
      fetch(`/api/v1/files/content?path=${encodeURIComponent(file.path)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: value }),
      }).catch(() => {})
    }, 1000)
  }

  return (
    <div className="file-content-panel">
      <div className="file-content-header">
        <span className="file-content-name">{fileName}</span>
        <span className="file-content-path">{file.path}</span>
        <button className="file-content-close" onClick={onClose} title="Close">&#10005;</button>
      </div>
      <div className="file-content-body">
        {file.type === 'image' && (
          <div className="file-content-image-wrap">
            <img
              src={`data:${file.mime};base64,${file.content}`}
              alt={fileName}
              style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }}
            />
          </div>
        )}
        {file.type === 'binary' && (
          <div className="file-content-binary">
            <div className="binary-icon">&#128196;</div>
            <div className="binary-name">{fileName}</div>
            <div className="binary-info">Binary &middot; {file.ext.toUpperCase()} &middot; {formatBytes(file.size)}</div>
            <div className="binary-msg">This file type cannot be previewed</div>
          </div>
        )}
        {file.type === 'text' && (
          <Editor
            key={file.path}
            height="100%"
            language={lang}
            defaultValue={file.content}
            theme="vs-dark"
            onChange={handleChange}
            options={{
              minimap: { enabled: false },
              fontSize,
              lineNumbers: 'on',
              wordWrap: 'off',
              scrollBeyondLastLine: false,
              automaticLayout: true,
              padding: { top: 8, bottom: 8 },
              scrollbar: {
                verticalScrollbarSize: 6,
                horizontalScrollbarSize: 6,
                useShadows: false,
              },
              overviewRulerLanes: 0,
              renderLineHighlight: 'line',
              contextmenu: true,
            }}
          />
        )}
      </div>
    </div>
  )
}
