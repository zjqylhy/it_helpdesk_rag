<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, ApiError, download, errorText, type ChatMessage, type KnowledgeBase, type Session } from '../../shared/api'
import { forgetPending, lastSession, pendingChats, savePending, selectSession, type PendingChat } from '../../shared/chat-state'
import { conversationPolling } from '../../shared/polling'
const props = defineProps<{ token: string; userId: string; knowledge: KnowledgeBase[] }>()
const sessions = ref<Session[]>([]), selected = ref(''), scope = ref<string[]>([]), messages = ref<ChatMessage[]>([])
const question = ref(''), sending = ref(false), stopping = ref(false), loading = ref(false), error = ref('')
const busy = computed(() => messages.value.some(m => m.status === 'generating'))
let alive = true, recovering = false
let pendingRetry: ReturnType<typeof setTimeout> | undefined
const poller = conversationPolling(
  (id, signal) => api<ChatMessage[]>(`/sessions/${id}/messages`, props.token, { signal }),
  rows => { messages.value = rows; loading.value = false; error.value = '' },
  reason => {
    loading.value = false
    if (reason instanceof ApiError && [400, 401, 403, 404, 409].includes(reason.status)) {
      messages.value = []; error.value = errorText(reason)
    } else error.value = '连接中断，后台任务继续执行，正在重新查询…'
  },
)
async function refresh() { sessions.value = await api<Session[]>('/sessions', props.token) }
function open(id: string) {
  selected.value = id; messages.value = []; error.value = ''; loading.value = true
  selectSession(props.userId, id); poller.open(id)
}
async function newSession() {
  error.value = ''
  try {
    const row = await api<{ id: string }>('/sessions', props.token, { method: 'POST', body: JSON.stringify({ kb_ids: scope.value }) })
    if (!alive) return
    await refresh(); if (alive) open(row.id)
  } catch (reason) { if (alive) error.value = errorText(reason) }
}
async function submit(pending: PendingChat) {
  const result = await api<{ message_id: string }>('/chat', props.token, { method: 'POST', body: JSON.stringify(pending) })
  if (!result.message_id) throw new Error('任务受理结果无效，请查询会话确认状态')
  forgetPending(props.userId, pending.request_id)
}
async function recoverPending() {
  if (recovering || !alive) return
  recovering = true
  clearTimeout(pendingRetry)
  try {
    for (const pending of pendingChats(props.userId)) {
      if (!alive) break
      try {
        const rows = await api<ChatMessage[]>(`/sessions/${pending.session_id}/messages`, props.token)
        if (rows.some(m => m.request_id === pending.request_id)) forgetPending(props.userId, pending.request_id)
        else await submit(pending)
      } catch (reason) {
        if (reason instanceof ApiError && [400, 401, 403, 404, 409].includes(reason.status)) {
          forgetPending(props.userId, pending.request_id)
          throw reason
        }
        if (alive) pendingRetry = setTimeout(() => { void recoverPending() }, 5000)
        throw reason
      }
    }
    if (alive) poller.refresh()
  } catch (reason) {
    if (alive) error.value = errorText(reason)
  } finally { recovering = false }
}
async function send() {
  const text = question.value.trim()
  if (!text || sending.value || busy.value || loading.value) return
  if (!selected.value) { await newSession(); if (!selected.value || !alive) return }
  const pending = { session_id: selected.value, request_id: crypto.randomUUID(), question: text }
  sending.value = true; error.value = ''
  try {
    savePending(props.userId, pending); question.value = ''
    await submit(pending)
    if (alive) { await refresh(); poller.refresh() }
  } catch (reason) {
    if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500) {
      forgetPending(props.userId, pending.request_id)
      if (alive) { question.value = text; error.value = errorText(reason) }
    } else if (alive) {
      error.value = '提交状态待确认，网络恢复后将查询原任务，不会重复执行。'
      pendingRetry = setTimeout(() => { void recoverPending() }, 1000)
    }
  } finally { if (alive) sending.value = false }
}
async function stopAnswer() {
  const message = messages.value.find(m => m.status === 'generating'), id = selected.value
  if (!message || stopping.value) return
  stopping.value = true
  try {
    await api(`/sessions/${id}/messages/${message.id}/stop`, props.token, { method: 'POST' })
    if (alive && selected.value === id) { poller.refresh(); error.value = '已停止回答，可刷新会话确认状态' }
  } catch (reason) { if (alive) error.value = errorText(reason) }
  finally { if (alive) stopping.value = false }
}
async function getFile(version: string) { try { await download(version, props.token) } catch (reason) { error.value = errorText(reason) } }
function resume() { if (document.visibilityState === 'visible') { poller.refresh(); void recoverPending() } }
onMounted(async () => {
  window.addEventListener('online', resume); document.addEventListener('visibilitychange', resume)
  try {
    await refresh()
    if (!alive) return
    const saved = lastSession(props.userId), id = sessions.value.find(s => s.id === saved)?.id ?? sessions.value[0]?.id
    if (id) open(id)
    await recoverPending()
  } catch (reason) { if (alive) error.value = errorText(reason) }
})
onBeforeUnmount(() => {
  alive = false; poller.stop(); clearTimeout(pendingRetry)
  window.removeEventListener('online', resume); document.removeEventListener('visibilitychange', resume)
})
const statusNames: Record<string, string> = { completed: '回答完成', partial: '部分回答', clarify: '需要澄清', refused: '资料不足', restricted: '访问权限已变化', interrupted: '回答已中断', error: '回答未完成', generating: '正在生成' }
</script>

<template>
  <div class="chat-layout">
    <aside class="panel">
      <h2>会话</h2><p class="muted small">新会话范围；留空使用默认资料</p>
      <label v-for="kb in knowledge" :key="kb.id" class="check"><input v-model="scope" type="checkbox" :value="kb.id" :disabled="sending">{{ kb.name }} · {{ kb.organization }}</label>
      <button :disabled="sending" @click="newSession">新建会话</button>
      <ul class="session-list"><li v-for="item in sessions" :key="item.id"><button :aria-current="selected === item.id" @click="open(item.id)">{{ item.title || '新会话' }}</button></li></ul>
    </aside>
    <section class="panel chat-main">
      <h2>知识问答</h2><p class="muted small">回答依据当前授权资料，切换范围请新建会话。</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="loading" role="status">正在加载历史…</p>
      <div v-if="!messages.length && !loading" class="empty"><h3>从一个具体问题开始</h3><p>涉及学校或组织时，请在问题中明确说明。</p></div>
      <article v-for="item in messages" :key="item.id" class="message"><h3>{{ item.question }}</h3><span class="tag">{{ statusNames[item.status] ?? item.status }}</span><span v-if="item.status === 'generating'" class="muted small">正在生成，内容尚未校验</span><p class="answer">{{ item.answer }}</p>
        <details v-for="citation in item.citations" :key="citation.label"><summary>{{ citation.label }} · {{ citation.organization }} · {{ citation.title }}</summary><blockquote>{{ citation.text }}</blockquote><p class="small">位置：{{ JSON.stringify(citation.locator) }}</p><button @click="getFile(citation.version_id)">下载引用原文</button></details>
      </article>
      <p v-if="busy" role="status" class="muted">正在生成，可切换页面，返回后继续查看</p>
      <form class="composer" @submit.prevent="send"><label for="question">你的问题</label><textarea id="question" v-model="question" :disabled="busy || sending || loading" maxlength="8000" placeholder="例如：推送接口返回成功，为什么用户仍未看到通知？" rows="3" required></textarea><div class="actions"><button v-if="busy" type="button" :disabled="stopping" @click="stopAnswer">停止回答</button><button v-else type="submit" :disabled="sending || loading || !question.trim()">发送问题</button><button type="button" :disabled="!selected" @click="poller.refresh()">刷新历史</button></div></form>
    </section>
  </div>
</template>
