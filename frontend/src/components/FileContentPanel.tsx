/**
 * FileContentPanel — renders opened files with syntax highlighting.
 *
 * Handles:
 *  - Code / text  → Shiki (VS Code–grade syntax highlighting, vitesse-dark theme)
 *  - Markdown      → marked (rendered HTML)
 *  - Images        → <img> centered
 *  - Binary/other  → neutral "cannot preview" message
 */

import { useEffect, useRef, useState } from 'react'
import { createHighlighter, type Highlighter } from 'shiki'
import { marked } from 'marked'

// ── Extension → Shiki language map ───────────────────────────────────────────

const EXT_LANG: Record<string, string> = {
  js: 'javascript', jsx: 'jsx', ts: 'typescript', tsx: 'tsx',
  py: 'python', java: 'java', kt: 'kotlin', swift: 'swift',
  go: 'go', rs: 'rust', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', c: 'c', h: 'c',
  cs: 'csharp', php: 'php', rb: 'ruby', scala: 'scala', dart: 'dart',
  json: 'json', jsonc: 'jsonc', yaml: 'yaml', yml: 'yaml', toml: 'toml',
  xml: 'xml', html: 'html', htm: 'html', css: 'css', scss: 'scss', less: 'less',
  sh: 'bash', bash: 'bash', zsh: 'bash', fish: 'bash',
  sql: 'sql', graphql: 'graphql', gql: 'graphql',
  dockerfile: 'dockerfile', makefile: 'makefile',
  md: 'markdown', mdx: 'mdx',
  r: 'r', lua: 'lua', vim: 'viml', tf: 'hcl', hcl: 'hcl',
  proto: 'protobuf', gradle: 'groovy', groovy: 'groovy',
}

const SHIKI_LANGS = [...new Set(Object.values(EXT_LANG))]

// Singleton highlighter — created once, reused
let _highlighter: Highlighter | null = null
let _highlighterPromise: Promise<Highlighter> | null = null

function getHighlighter(): Promise<Highlighter> {
  if (_highlighter) return Promise.resolve(_highlighter)
  if (_highlighterPromise) return _highlighterPromise
  _highlighterPromise = createHighlighter({
    themes: ['vitesse-dark'],
    langs: SHIKI_LANGS,
  }).then(h => { _highlighter = h; return h })
  return _highlighterPromise!
}

// ── Component ─────────────────────────────────────────────────────────────────

export interface OpenFile {
  path: string
  type: 'text' | 'image' | 'binary'
  content: string   // text content or base64 for images
  mime?: string
  ext: string
  size: number
}

interface Props {
  file: OpenFile
  onClose: () => void
}

export default function FileContentPanel({ file, onClose }: Props) {
  const [html, setHtml] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)

  const fileName = file.path.split(/[/\\]/).pop() || file.path

  useEffect(() => {
    if (file.type !== 'text') { setHtml(null); return }

    const ext = file.ext.toLowerCase()

    // Markdown — render with marked
    if (ext === 'md' || ext === 'mdx') {
      setHtml(marked.parse(file.content) as string)
      return
    }

    // Code — highlight with Shiki
    const lang = EXT_LANG[ext] || 'text'
    setLoading(true)
    setHtml(null)

    getHighlighter().then(h => {
      const highlighted = h.codeToHtml(file.content, {
        lang,
        theme: 'vitesse-dark',
      })
      setHtml(highlighted)
      setLoading(false)
    }).catch(() => {
      // Fallback: plain text
      setHtml(`<pre style="white-space:pre-wrap;word-break:break-word">${escHtml(file.content)}</pre>`)
      setLoading(false)
    })
  }, [file.path, file.content, file.ext, file.type])

  return (
    <div className="file-content-panel">
      <div className="file-content-header">
        <span className="file-content-name">{fileName}</span>
        <span className="file-content-path">{file.path}</span>
        <button className="file-content-close" onClick={onClose} title="Close">✕</button>
      </div>

      <div className="file-content-body" ref={containerRef}>
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
            <div className="binary-icon">📄</div>
            <div className="binary-name">{fileName}</div>
            <div className="binary-info">
              Binary file · {file.ext.toUpperCase()} · {formatBytes(file.size)}
            </div>
            <div className="binary-msg">This file type cannot be previewed</div>
          </div>
        )}

        {file.type === 'text' && loading && (
          <div className="file-content-loading">Highlighting…</div>
        )}

        {file.type === 'text' && !loading && html && (
          file.ext === 'md' || file.ext === 'mdx'
            ? <div
                className="markdown-body"
                dangerouslySetInnerHTML={{ __html: html }}
              />
            : <div
                className="shiki-wrap"
                dangerouslySetInnerHTML={{ __html: html }}
              />
        )}
      </div>
    </div>
  )
}

// ── Utils ─────────────────────────────────────────────────────────────────────

function escHtml(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function formatBytes(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 ** 2).toFixed(1)} MB`
}
