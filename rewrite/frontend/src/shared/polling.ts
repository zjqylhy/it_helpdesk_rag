import { ApiError, type ChatMessage } from './api'

// 单次查询完成后才安排下一次；切换页面后丢弃旧响应，不影响后台任务。
export function conversationPolling(
  load: (id: string, signal: AbortSignal) => Promise<ChatMessage[]>,
  update: (messages: ChatMessage[]) => void,
  failed: (reason: unknown) => void,
  interval = 1000,
) {
  let session = '', generation = 0, failures = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | null = null
  let refreshRequested = false
  function stop() {
    ++generation; clearTimeout(timer); controller?.abort(); controller = null; refreshRequested = false
  }
  async function check() {
    if (!session || controller) return
    clearTimeout(timer)
    const current = generation, active = new AbortController()
    controller = active
    let delay: number | undefined
    try {
      const messages = await load(session, active.signal)
      if (current !== generation) return
      update(messages); failures = 0
      if (messages.some(m => m.status === 'generating')) delay = interval
    } catch (reason) {
      if (current !== generation || active.signal.aborted) return
      failed(reason)
      const terminal = reason instanceof ApiError && [400, 401, 403, 404, 409].includes(reason.status)
      if (!terminal) delay = Math.min(15000, interval * 2 ** Math.min(failures++, 4))
    } finally {
      if (current === generation) {
        controller = null
        if (refreshRequested) { delay = 0; refreshRequested = false }
        if (delay !== undefined) timer = setTimeout(() => { void check() }, delay)
      }
    }
  }
  return {
    open(id: string) { stop(); session = id; failures = 0; void check() },
    refresh() { if (controller) refreshRequested = true; else void check() },
    stop,
  }
}
