import { DiffEditor as MonacoDiffEditor } from '@monaco-editor/react'
import type { FileEditProposal } from '../api/client'

interface Props {
  proposal: FileEditProposal
  onAccept: () => void
  onAcceptAll: () => void
  onReject: () => void
}

export default function DiffDialog({ proposal, onAccept, onAcceptAll, onReject }: Props) {
  const lang = proposal.file_path.endsWith('.py')
    ? 'python'
    : proposal.file_path.endsWith('.java')
    ? 'java'
    : proposal.file_path.endsWith('.kt')
    ? 'kotlin'
    : proposal.file_path.match(/\.[tj]sx?$/)
    ? 'typescript'
    : 'plaintext'

  return (
    <div className="diff-overlay">
      <div className="diff-dialog">
        <div className="diff-header">
          <div className="diff-title">
            <span className="diff-icon">±</span>
            <span className="diff-path">{proposal.file_path}</span>
          </div>
          <p className="diff-description">{proposal.description}</p>
        </div>

        <div className="diff-editor">
          <MonacoDiffEditor
            height="420px"
            language={lang}
            original={proposal.original_snippet || ''}
            modified={proposal.new_snippet}
            theme="vs-dark"
            options={{
              readOnly: true,
              renderSideBySide: true,
              fontSize: 13,
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              wordWrap: 'on',
            } as any}
          />
        </div>

        <div className="diff-actions">
          <button className="btn-reject" onClick={onReject}>Reject</button>
          <button className="btn-accept-all" onClick={onAcceptAll}>Accept All in Chat</button>
          <button className="btn-accept" onClick={onAccept}>Accept</button>
        </div>
      </div>
    </div>
  )
}
