const BASE = '/api/v1'

// ── Auth config (user's own Anthropic key — optional) ────────────────────────

export interface AuthConfig {
  apiKey: string
}

export function loadAuth(): AuthConfig | null {
  try {
    const raw = localStorage.getItem('codelm_auth')
    if (raw) return JSON.parse(raw)
  } catch { /* ignore */ }
  return null
}

export function saveAuth(config: AuthConfig) {
  localStorage.setItem('codelm_auth', JSON.stringify(config))
}

export function clearAuth() {
  localStorage.removeItem('codelm_auth')
}

// ── Budget config (real USD — 75% of purchase price goes to API spend) ────────
//
// Plan mapping (user pays → API budget):
//   $5  → $3.75   $10 → $7.50   $20 → $15.00
//
// The $1 overdraft rule: tasks already running may spend up to $1 past $0
// before the server stops them.  New tasks are blocked when balance <= -1.00.

export interface CreditsConfig {
  balance_usd: number
}

const CREDITS_KEY = 'codelm_credits'

// Passthrough ratio: 75% of what the user pays goes to API spend
const API_RATIO = 0.75

// Plans available at purchase (user-facing price → API budget added)
export const CREDIT_PLANS: { price: number; api_budget: number; label: string }[] = [
  { price: 5,  api_budget: 5  * API_RATIO, label: '500 msgs (~$3.75)' },
  { price: 10, api_budget: 10 * API_RATIO, label: '1,200 msgs (~$7.50)' },
  { price: 20, api_budget: 20 * API_RATIO, label: '2,500 msgs (~$15.00)' },
]

export function loadCredits(): CreditsConfig {
  try {
    const raw = localStorage.getItem(CREDITS_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      // Migrate old integer-credit format to USD
      if (typeof parsed.balance === 'number' && parsed.balance_usd === undefined) {
        return { balance_usd: 0 }
      }
      return parsed
    }
  } catch {}
  return { balance_usd: 0 }
}

export function saveCredits(c: CreditsConfig) {
  localStorage.setItem(CREDITS_KEY, JSON.stringify(c))
}

/** Deduct actual API cost (in USD) from stored balance. Returns updated balance. */
export function deductCost(cost_usd: number): number {
  const c = loadCredits()
  const updated = { balance_usd: Math.round((c.balance_usd - cost_usd) * 1_000_000) / 1_000_000 }
  saveCredits(updated)
  return updated.balance_usd
}

/** True if a new task is allowed to start (above the $1 overdraft floor). */
export function canSendMessage(balance_usd: number): boolean {
  return balance_usd > -1.00
}

function authHeaders(): Record<string, string> {
  const auth = loadAuth()
  if (!auth?.apiKey) return {}
  return { 'X-Api-Key': auth.apiKey }
}

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

export async function deleteProjectKnowledge(projectId: string): Promise<void> {
  const res = await fetch(`${BASE}/projects/${projectId}/knowledge`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || res.statusText)
  }
}

// ── Sessions ──────────────────────────────────────────────────────────────────

export async function listSessions(projectId: string): Promise<Session[]> {
  const res = await fetch(`${BASE}/projects/${projectId}/sessions`)
  if (!res.ok) return []
  return res.json()
}

export async function createSession(projectId: string, agentId?: string): Promise<Session> {
  const res = await fetch(`${BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId, agent_id: agentId }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: 'Database unavailable' }))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
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

// ── File tree ─────────────────────────────────────────────────────────────────

export async function fetchFileTree(root: string): Promise<any> {
  const r = await fetch(`${BASE}/files/tree?root=${encodeURIComponent(root)}`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function fetchFileContent(path: string): Promise<{
  type: 'text' | 'image' | 'binary'
  content: string
  mime?: string
  ext: string
  size: number
}> {
  const r = await fetch(`${BASE}/files/content?path=${encodeURIComponent(path)}`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
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
    onCost: (cost_usd: number, balance_usd: number) => void
    onDone: () => void
    onError: (err: string) => void
    onTodosAdded?: (count: number) => void
  }
): () => void {
  const controller = new AbortController()
  const auth = loadAuth()
  const balance = loadCredits().balance_usd

  ;(async () => {
    try {
      const res = await fetch(`${BASE}/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...authHeaders(),
          // Only send budget header in credits mode (no personal API key)
          ...(!auth?.apiKey ? { 'X-Budget-Balance': String(balance) } : {}),
        },
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
            else if (event.cost_usd !== undefined) {
              const newBalance = deductCost(event.cost_usd)
              callbacks.onCost(event.cost_usd, newBalance)
            }
            else if (event.todos_added && callbacks.onTodosAdded) callbacks.onTodosAdded(event.todos_added)
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
