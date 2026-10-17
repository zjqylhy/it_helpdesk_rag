export interface Principal { ID: string; Role: 'admin' | 'user' }
export interface KnowledgeBase { id: string; name: string; organization: string; default_search: boolean }
export interface Session { id: string; title: string; kb_ids: string[]; created_at: string }
export interface Citation { label: string; version_id: string; chunk_id: string; title: string; organization: string; text: string; locator: Record<string, unknown> }
export interface ChatMessage { id: string; question: string; answer: string; status: string; citations: Citation[] }
export interface Document { id: string; title: string; enabled: boolean; active_version: string | null; job_status: string; progress: number; error: string | null }
export interface Version { id: string; filename: string; status: string; progress: number; error: string | null; job_id: string }
export interface User { id: string; username: string; role: string; disabled: boolean; usage_kind: 'demo' | 'real_usage' }
export interface Group { id: string; name: string; user_ids: string[] }
export interface Grant { visibility: 'all_users' | 'groups'; group_ids: string[] }

export function errorText(reason: unknown): string { return reason instanceof Error ? reason.message : '请求失败' }

export async function download(version: string, token: string) {
  const response = await fetch(`/api/v1/files/${encodeURIComponent(version)}`, { headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok) throw new Error('文件不可访问，请刷新资料或重新登录')
  const blob = await response.blob(), url = URL.createObjectURL(blob)
  const name = response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''(.+)$/)?.[1]
  const link = document.createElement('a'); link.href = url; link.download = name ? decodeURIComponent(name) : '原始资料'
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message) }
}

export async function api<T>(path: string, token = '', options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (typeof options.body === 'string') headers.set('Content-Type', 'application/json')
  const response = await fetch(`/api/v1${path}`, { ...options, headers })
  if (!response.ok) {
    const error = await response.json().catch(() => ({})) as { code?: string; error?: string }
    throw new ApiError(response.status, error.code ?? 'request_failed', error.error ?? '请求失败，请稍后重试')
  }
  return response.status === 204 ? undefined as T : await response.json() as T
}
