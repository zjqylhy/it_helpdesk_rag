<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { api, auth, chat, download } from './api'

type Row = Record<string, any>
const logged = ref(false), role = ref(''), username = ref(''), password = ref(''), error = ref(''), busy = ref(false)
const page = ref('chat'), kbs = ref<Row[]>([]), kb = ref(''), sessions = ref<Row[]>([]), session = ref(''), messages = ref<Row[]>([])
const docs = ref<Row[]>([]), versions = ref<Row[]>([]), chunks = ref<Row[]>([]), selectedDoc = ref<Row | null>(null)
const question = ref(''), sending = ref(false), stage = ref(''), list = ref<HTMLElement>(), chosenFile = ref<File | null>(null)
const title = ref(''), source = ref(''), kbName = ref(''), organization = ref(''), newKB = ref(false), duplicate = ref(false)
let controller: AbortController | undefined, timer: ReturnType<typeof setInterval> | undefined

async function run(fn: () => Promise<void>) { error.value = ''; try { await fn() } catch (e) { error.value = (e as Error).message } }
async function refresh() {
  kbs.value = await api('/knowledge-bases'); if (!kb.value && kbs.value.length) kb.value = kbs.value[0]!.id
  sessions.value = await api('/sessions'); if (kb.value) await loadDocs()
}
async function login() { await run(async () => { busy.value = true; try { const data = await api('/auth/login', 'POST', {username: username.value, password: password.value}); auth.token = data.token; sessionStorage.setItem('token', data.token); role.value = data.role; logged.value = true; password.value = ''; await refresh() } finally { busy.value = false } }) }
async function logout() { controller?.abort(); await api('/auth/logout', 'POST').catch(() => {}); auth.token = ''; sessionStorage.removeItem('token'); logged.value = false; messages.value = []; session.value = '' }
async function loadDocs() { docs.value = await api('/documents?kb_id=' + kb.value) }
function resetChat() { if (sending.value) return; session.value = ''; messages.value = []; page.value = 'chat' }
async function switchKB() { resetChat(); selectedDoc.value = null; await run(loadDocs) }
async function selectSession(s: Row) { if (sending.value) return; await run(async () => { session.value = s.id; kb.value = s.kb_id; page.value = 'chat'; messages.value = await api(`/sessions/${s.id}/messages`); await loadDocs(); await scroll() }) }
async function scroll() { await nextTick(); list.value?.scrollTo({top: list.value.scrollHeight, behavior: 'smooth'}) }
async function send() {
  if (sending.value || !question.value.trim() || !kb.value) return
  error.value = ''; sending.value = true; controller = new AbortController()
  const text = question.value.trim(); question.value = ''; const requestId = crypto.randomUUID(); const started = performance.now()
  let first = false
  try {
    if (!session.value) { const data = await api('/sessions', 'POST', {kb_id: kb.value}); session.value = data.id }
    const message: Row = {question: text, answer: '', status: 'generating', citations: [], request_id: requestId}
    messages.value.push(message)
    // 操作响应式数组中的对象，保证流式增量立即渲染。
    const current = messages.value[messages.value.length - 1]!
    await chat({session_id: session.value, question: text, request_id: requestId}, controller.signal, (kind, data) => {
      if (kind === 'status') stage.value = data.text
      if (kind === 'answer_delta') { if (!first) { current.browser_ttft_ms = Math.round(performance.now() - started); first = true }; current.answer += data.text }
      if (kind === 'refusal') current.answer = data.text
      if (kind === 'citations') current.citations = data.citations
      if (kind === 'evidence') { current.snapshot_id = data.snapshot_id; current.decision = data.decision }
      if (kind === 'done') { current.status = data.status; current.trace = data.trace; current.browser_total_ms = Math.round(performance.now() - started) }
      if (kind === 'error') { current.status = 'error'; current.error = data.error; error.value = data.error }
      void scroll()
    })
    sessions.value = await api('/sessions')
  } catch (e) {
    const last = messages.value[messages.value.length - 1]
    if (last?.status === 'generating') last.status = 'interrupted'
    error.value = (e as Error).name === 'AbortError' ? '已停止生成，当前回答未完成。' : (e as Error).message
  } finally { sending.value = false; stage.value = '' }
}
async function upload() { await run(async () => { if (!chosenFile.value) throw new Error('请选择文件'); busy.value = true; try {
  const form = new FormData(); form.append('file', chosenFile.value); form.append('kb_id', kb.value); form.append('title', title.value); form.append('source', source.value); form.append('confirm_duplicate', String(duplicate.value))
  await api(selectedDoc.value ? `/documents/${selectedDoc.value.id}/versions` : '/documents', 'POST', form)
  chosenFile.value = null; title.value = ''; source.value = ''; duplicate.value = false; await loadDocs(); if (selectedDoc.value) await viewDoc(selectedDoc.value)
} finally { busy.value = false } }) }
async function viewDoc(doc: Row) { selectedDoc.value = doc; versions.value = await api(`/documents/${doc.id}/versions`); chunks.value = []; if (versions.value[0]?.status === 'completed') await viewChunks(versions.value[0]!.id) }
async function viewChunks(version: string) { chunks.value = await api(`/documents/${selectedDoc.value!.id}/versions/${version}/chunks`) }
async function toggle(doc: Row) { await run(async () => { await api(`/documents/${doc.id}/enabled`, 'PATCH', {enabled: !doc.enabled}); await loadDocs() }) }
async function retry(job: string) { await run(async () => { await api(`/ingestion-jobs/${job}/retry`, 'POST'); await loadDocs() }) }
async function createKB() { await run(async () => { await api('/knowledge-bases', 'POST', {name: kbName.value, organization: organization.value}); newKB.value = false; kbName.value = ''; organization.value = ''; await refresh() }) }
const labels: Record<string,string> = {queued:'等待处理',processing:'处理中',completed:'已完成',failed:'失败',generating:'生成中',refused:'资料不足',clarify:'待澄清',error:'未验证',interrupted:'已中断'}
onMounted(async () => { if (auth.token) await run(async () => { const me = await api('/auth/me'); role.value = me.Role; logged.value = true; await refresh() }); timer = setInterval(() => { if (logged.value && kb.value && page.value === 'knowledge') void run(loadDocs) }, 4000) })
onUnmounted(() => { clearInterval(timer); controller?.abort() })
</script>

<template>
  <div v-if="!logged" class="login-wrap">
    <section class="login-card"><div class="logo">知</div><p class="eyebrow">IT KNOWLEDGE ASSISTANT</p><h1>让知识成为答案。</h1><p class="muted">从团队资料中寻找依据，让每一次解答都有出处。</p>
      <form @submit.prevent="login"><label>账号<input v-model="username" autocomplete="username" required placeholder="输入本地配置的账号"></label><label>密码<input v-model="password" type="password" autocomplete="current-password" required></label><button class="primary" :disabled="busy">{{busy ? '登录中…' : '登录工作台 →'}}</button></form><p v-if="error" class="error" role="alert">{{error}}</p><small>账号由管理员配置 · 仅在当前浏览器会话保存登录状态</small>
    </section><aside class="login-art"><span class="art-orbit"></span><div class="art-text">KNOWLEDGE<br>WITH<br><em>EVIDENCE.</em></div><p>知识管理 / 智能问答 / 来源追溯</p></aside>
  </div>
  <div v-else class="workspace">
    <aside class="sidebar"><a class="brand" @click="resetChat"><span class="logo">知</span><b>知答<span>IT 知识助手</span></b></a>
      <p class="nav-label">工作空间</p><nav><button :class="{active:page==='chat'}" @click="page='chat'">◈ <span>知识问答</span></button><button :class="{active:page==='knowledge'}" @click="page='knowledge'">▤ <span>知识管理</span></button></nav>
      <p class="nav-label">适用知识库</p><select v-model="kb" :disabled="sending" @change="switchKB"><option value="" disabled>选择知识库</option><option v-for="k in kbs" :key="k.id" :value="k.id">{{k.name}} · {{k.organization}}</option></select>
      <button class="new-chat" :disabled="sending" @click="resetChat">＋ 新建会话</button><p class="nav-label">最近会话</p><div class="session-list"><button v-for="s in sessions.filter(s=>s.kb_id===kb)" :key="s.id" :class="{selected:s.id===session}" :disabled="sending" @click="selectSession(s)">{{s.title}}</button><p v-if="!sessions.length" class="empty-small">还没有会话</p></div>
      <div class="account"><span class="avatar">{{role==='admin'?'管':'用'}}</span><span>{{role==='admin'?'管理员':'普通用户'}}<small>本地演示工作台</small></span><button @click="logout" aria-label="退出登录">↗</button></div>
    </aside>
    <main><header><div><p class="eyebrow">{{page==='chat'?'ASK YOUR KNOWLEDGE':'MANAGE YOUR SOURCES'}}</p><h2>{{page==='chat'?'知识问答':'知识管理'}}</h2></div><span class="scope">{{kbs.find(k=>k.id===kb)?.organization || '尚未选择知识库'}}</span></header>
      <div v-if="error" class="notice error" role="alert">{{error}}<button @click="error=''">×</button></div>
      <template v-if="page==='chat'">
        <div ref="list" class="conversation">
          <section v-if="!messages.length" class="welcome"><span class="welcome-icon">✳</span><p class="eyebrow">从资料出发，找到可信的解答</p><h1>今天有什么需要解决？</h1><p>选择适用知识库，再描述你的问题。<br>答案附原文引用；资料不足时会说明原因。</p><div class="suggestions"><button @click="question='如何连接 VPN？'">↗ 如何连接 VPN？</button><button @click="question='网络无法连接时应如何排查？'">↗ 网络连接排查步骤</button><button @click="question='邮箱配置有哪些注意事项？'">↗ 邮箱配置注意事项</button></div><div v-if="!kbs.length" class="notice">尚无知识库。请由管理员先创建知识库并上传资料。</div></section>
          <article v-for="(m,i) in messages" :key="m.id || i" class="exchange"><div class="question-bubble">{{m.question}}</div><div class="answer-row"><span class="answer-avatar">知</span><div class="answer-content"><div class="answer-meta">知答 <span class="badge" :class="m.status">{{labels[m.status] || m.status}}</span></div><p v-if="!m.answer && m.status==='generating'" class="loading">{{stage || '正在连接…'}}<span>•••</span></p><div class="answer-text">{{m.answer}}</div><p v-if="['error','interrupted'].includes(m.status)" class="error">{{m.error || '回答未完成，请核对后重试。'}}</p>
            <div v-if="m.citations?.length" class="citations"><p class="nav-label">回答依据 · {{m.citations.length}} 个来源</p><details v-for="c in m.citations" :key="c.label"><summary><span>{{c.label}}</span>{{c.title}}<small>{{c.locator.page ? '第 '+c.locator.page+' 页' : '段落 '+c.locator.paragraph}}</small></summary><p>{{c.text}}</p><button @click="run(()=>download(c.version_id,c.filename))">下载原文件 ↗</button></details></div><p v-if="m.trace" class="trace">服务耗时 {{m.trace.total_ms}} ms<span v-if="m.browser_ttft_ms"> · 浏览器首字 {{m.browser_ttft_ms}} ms</span> · 快照 {{m.snapshot_id?.slice(0,8)}}</p>
          </div></div></article>
        </div>
        <footer class="composer"><form @submit.prevent="send"><textarea v-model="question" :disabled="sending" placeholder="描述问题，Shift + Enter 换行…" rows="2" @keydown.enter.exact.prevent="send"></textarea><button v-if="sending" type="button" @click="controller?.abort()" class="stop">停止</button><button v-else class="primary send" :disabled="!question.trim() || !kb" aria-label="发送问题">↑</button></form><p>答案仅依据所选知识库 · 请核对引用中的适用条件</p></footer>
      </template>
      <section v-else class="knowledge"><div class="section-heading"><div><h3>文档与版本</h3><p class="muted">成功处理后发布新版本，原有引用保留历史来源。</p></div><button v-if="role==='admin'" class="outline" @click="newKB=!newKB">＋ 创建知识库</button></div>
        <form v-if="newKB" class="kb-form" @submit.prevent="createKB"><input v-model="kbName" placeholder="知识库名称" required><input v-model="organization" placeholder="适用组织，例如某大学" required><button class="primary">创建</button></form>
        <div v-if="role==='admin' && kb" class="upload-panel"><div><span class="eyebrow">UPLOAD SOURCE</span><h3>{{selectedDoc ? '更新「'+selectedDoc.title+'」' : '添加知识文档'}}</h3><p class="muted">PDF / DOCX / Markdown / TXT · 最大 20 MiB · 文本内容</p></div><form @submit.prevent="upload"><input type="file" accept=".pdf,.docx,.md,.txt" @change="chosenFile=($event.target as HTMLInputElement).files?.[0] || null"><input v-if="!selectedDoc" v-model="title" placeholder="文档标题（可选）"><input v-model="source" placeholder="原文链接或来源说明（可选）"><label class="check"><input v-model="duplicate" type="checkbox">确认允许重复内容</label><button class="primary" :disabled="busy || !chosenFile">{{busy?'上传中…':'上传并处理'}}</button><button v-if="selectedDoc" type="button" @click="selectedDoc=null;versions=[];chunks=[]">取消更新</button></form></div>
        <div class="documents"><div v-for="d in docs" :key="d.id" class="document"><span class="file-icon">▤</span><div class="document-info"><h4>{{d.title}} <span v-if="!d.enabled" class="badge">已停用</span></h4><p>{{d.filename}} · {{labels[d.job_status] || '等待上传'}}<span v-if="d.job_status==='processing'"> {{d.progress}}%</span><span v-if="d.active_version"> · 已有可用版本</span></p><p v-if="d.error" class="error">{{d.error}}</p></div><button @click="run(()=>viewDoc(d))">版本与切片</button><button v-if="role==='admin'" @click="toggle(d)">{{d.enabled?'停用':'启用'}}</button><button v-if="role==='admin' && d.job_status==='failed'" @click="retry(d.job_id)">重试</button></div><div v-if="!docs.length" class="empty-docs">暂无文档，上传第一份资料开始使用。</div></div>
        <section v-if="selectedDoc" class="version-panel"><h3>{{selectedDoc.title}} · 版本记录</h3><div v-for="v in versions" :key="v.id" class="version"><span class="badge" :class="v.status">{{labels[v.status]}}</span><code>{{v.id.slice(0,8)}}</code><span>{{new Date(v.created_at).toLocaleString()}}</span><button v-if="v.status==='completed'" @click="run(()=>viewChunks(v.id))">查看切片</button><button @click="run(()=>download(v.id,v.filename))">原文件 ↗</button></div><details v-for="c in chunks" :key="c.id" class="chunk"><summary>切片 {{c.ordinal+1}} · {{c.locator.page?'第 '+c.locator.page+' 页':'段落 '+c.locator.paragraph}} · {{c.text.length}} 字符</summary><pre>{{c.text}}</pre></details></section>
      </section>
    </main>
  </div>
</template>
