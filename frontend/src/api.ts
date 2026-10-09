export const auth = { token: sessionStorage.getItem('token') || '' }
export async function api(path: string, method = 'GET', body?: unknown) {
  const isForm = body instanceof FormData
  const response = await fetch('/api/v1' + path, { method, headers: {
    Authorization: `Bearer ${auth.token}`, ...(!isForm && body ? {'Content-Type': 'application/json'} : {})
  }, body: body ? isForm ? body : JSON.stringify(body) : undefined })
  if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.error || `请求失败 (${response.status})`) }
  return response.status === 204 || response.status === 202 && !response.headers.get('Content-Type') ? null : response.json()
}
export async function chat(body: unknown, signal: AbortSignal, event: (kind: string, data: any) => void) {
  const response = await fetch('/api/v1/chat', { method: 'POST', headers: { Authorization: `Bearer ${auth.token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal })
  if (!response.ok) { const data = await response.json(); throw new Error(data.error) }
  const reader = response.body!.getReader(), decoder = new TextDecoder()
  let buffer = '', ended = false
  try {
    while (true) {
      const {value, done} = await reader.read()
      buffer += decoder.decode(value, {stream: !done}).replace(/\r\n/g, '\n')
      let end: number
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2)
        const kind = frame.split('\n').find(x => x.startsWith('event:'))?.slice(6).trim()
        const data = frame.split('\n').filter(x => x.startsWith('data:')).map(x => x.slice(5).trim()).join('\n')
        if (kind && data) { event(kind, JSON.parse(data)); if (kind === 'done' || kind === 'error') ended = true }
      }
      if (done) break
    }
  } finally { reader.releaseLock() }
  if (!ended) throw new Error('连接中断，回答尚未验证')
}
export async function download(version: string, filename: string) {
  const response = await fetch('/api/v1/files/' + version, {headers: {Authorization: `Bearer ${auth.token}`}})
  if (!response.ok) throw new Error('原文件下载失败')
  const url = URL.createObjectURL(await response.blob()), a = document.createElement('a')
  a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
