export interface Principal { ID: string; Role: 'admin' | 'user' }
export interface KnowledgeBase { id: string; name: string; organization: string; default_search: boolean }
export interface Session { id: string; title: string; kb_ids: string[]; created_at: string }

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
