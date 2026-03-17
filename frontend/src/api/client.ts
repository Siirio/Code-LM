const BASE = '/api/v1'

export interface ProjectStatus {
  project_id: string
  indexed: boolean
  files_indexed: number
  last_scanned_at: string | null
}

export interface ScanResult {
  project_id: string
  status: string
  files_found: number
  classes_found: number
  functions_found: number
  modules: string[]
  message: string
}

export interface Session {
  id: string
  title: string | null
  created_at: string
  message_count: number
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface FileEditProposal {
  file_path: string
  description: string
  original_snippet: string
  new_snippet: string
}

export interface AgentPersona {
  id: string
  name: string
  description: string | null
}

// ── Projects ──────────────────────────────────────────────────────────────────

export async function getProjectStatus(projectId: string): Promise<ProjectStatus> {
  const res = await fetch(`${BASE}/projects/${projectId}/status`)
  return res.json()
}

export async function scanProject(params: {
  project_id: string
  root_path: string
  scan_mode?: string
  folder_path?: string
  entry_point?: string
}): Promise<ScanResult> {
  const res = await fetch(`${BASE}/projects/scan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scan_mode: 'full', ...params }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
  return res.json()
}

// ── Sessions ──────────────────────────────────────────────────────────────────

export async function listSessions(projectId: string): Promise<Session[]> {
  const res = await fetch(`${BASE}/projects/${projectId}/sessions`)
  return res.json()
}

export async function createSession(projectId: string, agentId?: string): Promise<Session> {
  const res = await fetch(`${BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId, agent_id: agentId }),
  })
  return res.json()
}

export async function deleteSession(sessionId: string): Promise<void> {
  await fetch(`${BASE}/sessions/${sessionId}`, { method: 'DELETE' })
}

export async function getMessages(sessionId: string): Promise<ChatMessage[]> {
  const res = await fetch(`${BASE}/sessions/${sessionId}/messages`)
  return res.json()
}

// ── Agents ────────────────────────────────────────────────────────────────────

export async function listAgents(projectId: string): Promise<AgentPersona[]> {
  const res = await fetch(`${BASE}/projects/${projectId}/agents`)
  return res.json()
}

export async function createAgent(params: {
  project_id: string
  name: string
  description: string
  system_prompt_extra: string
}): Promise<AgentPersona> {
  const res = await fetch(`${BASE}/agents`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  return res.json()
}

// ── Chat stream ───────────────────────────────────────────────────────────────

export function chatStream(
  params: {
    project_id: string
    message: string
    session_id?: string
    agent_id?: string
    conversation_id?: string
  },
  callbacks: {
    onChunk: (text: string) => void
    onTool: (name: string) => void
    onAgent: (name: string) => void
    onFileEdit: (proposal: FileEditProposal) => void
    onDone: () => void
    onError: (err: string) => void
  }
): () => void {
  const controller = new AbortController()

  ;(async () => {
    try {
      const res = await fetch(`${BASE}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
        signal: controller.signal,
      })

      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }))
        callbacks.onError(body.detail || res.statusText)
        return
      }

      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const event = JSON.parse(line.slice(6))
            if (event.chunk !== undefined) callbacks.onChunk(event.chunk)
            else if (event.tool) callbacks.onTool(event.tool)
            else if (event.agent) callbacks.onAgent(event.agent)
            else if (event.file_edit) callbacks.onFileEdit(event.file_edit)
            else if (event.done) callbacks.onDone()
          } catch {
            // skip malformed SSE lines
          }
        }
      }
    } catch (err: unknown) {
      if (err instanceof Error && err.name !== 'AbortError') {
        callbacks.onError(err.message)
      }
    }
  })()

  return () => controller.abort()
}
