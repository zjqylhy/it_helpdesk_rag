<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, download, errorText, type ChatMessage, type Citation, type KnowledgeBase, type Session } from '../../shared/api'
import { chatStream } from '../../shared/sse'
const props = defineProps<{ token: string; knowledge: KnowledgeBase[] }>()
const sessions = ref<Session[]>([]), selected = ref(''), scope = ref<string[]>([]), messages = ref<ChatMessage[]>([])
const question = ref(''), busy = ref(false), loading = ref(false), error = ref(''), progress = ref('')
let controller: AbortController | null = null, loadGeneration = 0
async function refresh() { sessions.value = await api<Session[]>('/sessions', props.token) }
async function open(id: string) {
  if (busy.value) return
  const generation = ++loadGeneration; selected.value = id; messages.value = []; error.value = ''; loading.value = true
  try { const data = await api<ChatMessage[]>(`/sessions/${id}/messages`, props.token); if (generation === loadGeneration) messages.value = data }
  catch (reason) { error.value = errorText(reason) } finally { if (generation === loadGeneration) loading.value = false }
}
async function newSession() {
  error.value = ''
  try { const row = await api<{ id: string }>('/sessions', props.token, { method: 'POST', body: JSON.stringify({ kb_ids: scope.value }) }); await refresh(); await open(row.id) }
  catch (reason) { error.value = errorText(reason) }
}
async function send() {
  const text = question.value.trim()
  if (!text || busy.value || loading.value) return
  if (!selected.value) { await newSession(); if (!selected.value) return }
  busy.value = true; error.value = ''; progress.value = '正在连接'; question.value = ''; controller = new AbortController()
  const message: ChatMessage = { id: crypto.randomUUID(), question: text, answer: '', status: 'generating', citations: [] }
  messages.value.push(message)
  const current = messages.value[messages.value.length - 1]!
  try {
    await chatStream(props.token, selected.value, text, controller.signal, ({ event, data }) => {
      if (event === 'status') progress.value = String(data.message ?? '')
      if (event === 'answer_delta') current.answer += String(data.text ?? '')
      if (event === 'citations') current.citations = data.citations as Citation[]
      if (event === 'refusal') current.answer = String(data.message ?? '')
      if (event === 'done') current.status = String(data.status)
      if (event === 'error') throw new Error(String(data.message ?? '回答未完成'))
    })
    await refresh()
  } catch (reason) {
    // 未验证完成的片段不作为完整答案保留；历史最终状态由服务端决定。
    current.answer = ''; current.citations = []; current.status = 'interrupted'
    error.value = controller.signal.aborted ? '已停止回答，可刷新会话确认状态' : errorText(reason)
  } finally { busy.value = false; progress.value = ''; controller = null }
}
async function getFile(version: string) { try { await download(version, props.token) } catch (reason) { error.value = errorText(reason) } }
onMounted(() => { void refresh().catch(reason => { error.value = errorText(reason) }) })
onBeforeUnmount(() => { controller?.abort(); ++loadGeneration })
const statusNames: Record<string, string> = { completed: '回答完成', partial: '部分回答', clarify: '需要澄清', refused: '资料不足', restricted: '访问权限已变化', interrupted: '回答已中断', error: '回答未完成', generating: '正在生成' }
</script>
<template>
  <div class="chat-layout">
    <aside class="panel">
      <h2>会话</h2><p class="muted small">新会话范围；留空使用默认资料</p>
      <label v-for="kb in knowledge" :key="kb.id" class="check"><input v-model="scope" type="checkbox" :value="kb.id" :disabled="busy">{{ kb.name }} · {{ kb.organization }}</label>
      <button :disabled="busy" @click="newSession">新建会话</button>
      <ul class="session-list"><li v-for="item in sessions" :key="item.id"><button :aria-current="selected === item.id" :disabled="busy" @click="open(item.id)">{{ item.title || '新会话' }}</button></li></ul>
    </aside>
    <section class="panel chat-main">
      <h2>知识问答</h2><p class="muted small">回答依据当前授权资料，切换范围请新建会话。</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="loading" role="status">正在加载历史…</p>
      <div v-if="!messages.length && !loading" class="empty"><h3>从一个具体问题开始</h3><p>涉及学校或组织时，请在问题中明确说明。</p></div>
      <article v-for="item in messages" :key="item.id" class="message"><h3>{{ item.question }}</h3><span class="tag">{{ statusNames[item.status] ?? item.status }}</span><p class="answer">{{ item.answer }}</p>
        <details v-for="citation in item.citations" :key="citation.label"><summary>{{ citation.label }} · {{ citation.organization }} · {{ citation.title }}</summary><blockquote>{{ citation.text }}</blockquote><p class="small">位置：{{ JSON.stringify(citation.locator) }}</p><button @click="getFile(citation.version_id)">下载引用原文</button></details>
      </article>
      <p v-if="busy" role="status" class="muted">{{ progress }}</p>
      <form class="composer" @submit.prevent="send"><label for="question">你的问题</label><textarea id="question" v-model="question" :disabled="busy || loading" maxlength="8000" placeholder="例如：某学校的 VPN 如何登录？" rows="3" required></textarea><div class="actions"><button v-if="busy" type="button" @click="controller?.abort()">停止回答</button><button v-else type="submit" :disabled="loading || !question.trim()">发送问题</button><button type="button" :disabled="busy || !selected" @click="open(selected)">刷新历史</button></div></form>
    </section>
  </div>
</template>
