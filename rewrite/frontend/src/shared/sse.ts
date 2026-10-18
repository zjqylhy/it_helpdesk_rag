export interface StreamEvent { event: string; data: Record<string, unknown> }
const names = new Set(['status', 'evidence', 'answer_delta', 'citations', 'refusal', 'done', 'error'])

// 保留未完成的 UTF-8 字符与 CRLF，不把网络分块当作事件边界。
export async function consumeSSE(response: Response, requestId: string, onEvent: (event: StreamEvent) => void): Promise<void> {
  if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) throw new Error('回答流格式无效')
  const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true })
  let buffer = '', eventName = '', data: string[] = [], seq = 0, messageId = '', terminal = false
  function line(value: string) {
    if (value === '') {
      if (data.length) {
        if (terminal || !names.has(eventName)) throw new Error('回答事件无效或终态重复')
        const payload: unknown = JSON.parse(data.join('\n'))
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('回答数据无效')
        const p = payload as Record<string, unknown>
        if (p.request_id !== requestId || p.seq !== seq + 1 || typeof p.message_id !== 'string' || !p.message_id || (messageId && p.message_id !== messageId)) throw new Error('回答序号或请求编号无效')
        messageId = p.message_id; seq++
        terminal = eventName === 'done' || eventName === 'error'
        onEvent({ event: eventName, data: p })
      }
      eventName = ''; data = []; return
    }
    if (value.startsWith(':')) return
    const separator = value.indexOf(':'), field = separator < 0 ? value : value.slice(0, separator)
    const text = separator < 0 ? '' : value.slice(separator + 1).replace(/^ /, '')
    if (field === 'event') eventName = text
    if (field === 'data') data.push(text)
  }
  function drain(eof = false) {
    while (true) {
      const match = /[\r\n]/.exec(buffer)
      if (!match || (!eof && match[0] === '\r' && match.index === buffer.length - 1)) break
      const index = match.index, width = buffer.slice(index, index + 2) === '\r\n' ? 2 : 1
      line(buffer.slice(0, index)); buffer = buffer.slice(index + width)
    }
    if (buffer.length + data.join('').length > 1024 * 1024) throw new Error('回答事件过大')
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true }); drain()
    }
    buffer += decoder.decode(); drain(true)
    if (buffer || data.length || !terminal) throw new Error('回答连接中断，尚未完成')
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
}

export async function chatStream(token: string, sessionId: string, question: string, signal: AbortSignal, onEvent: (event: StreamEvent) => void) {
  const requestId = crypto.randomUUID()
  const response = await fetch('/api/v1/chat', { method: 'POST', signal, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sessionId, request_id: requestId, question }) })
  if (!response.ok) {
    const error = await response.json().catch(() => ({})) as { error?: string }
    throw new Error(error.error ?? '暂时无法提问')
  }
  await consumeSSE(response, requestId, onEvent)
}
