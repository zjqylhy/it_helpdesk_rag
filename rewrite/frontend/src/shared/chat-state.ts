export interface PendingChat { session_id: string; request_id: string; question: string }
const prefix = (user: string) => `helpdesk.chat.${user}.`

export function pendingChats(user: string): PendingChat[] {
  const result: PendingChat[] = []
  for (const key of Object.keys(localStorage)) {
    if (!key.startsWith(prefix(user) + 'pending.')) continue
    try {
      const p = JSON.parse(localStorage.getItem(key) ?? 'null') as PendingChat | null
      if (p && typeof p.session_id === 'string' && typeof p.request_id === 'string' && typeof p.question === 'string') result.push(p)
      else localStorage.removeItem(key)
    } catch { localStorage.removeItem(key) }
  }
  return result
}
export function savePending(user: string, value: PendingChat) {
  localStorage.setItem(prefix(user) + 'pending.' + value.request_id, JSON.stringify(value))
}
export function forgetPending(user: string, request: string) { localStorage.removeItem(prefix(user) + 'pending.' + request) }
export function lastSession(user: string) { return localStorage.getItem(prefix(user) + 'selected') ?? '' }
export function selectSession(user: string, id: string) { localStorage.setItem(prefix(user) + 'selected', id) }
export function clearChatState(user: string) {
  for (const key of Object.keys(localStorage)) if (key.startsWith(prefix(user))) localStorage.removeItem(key)
}
