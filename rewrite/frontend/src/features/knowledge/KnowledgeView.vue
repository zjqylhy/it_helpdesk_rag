<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, ApiError, download, errorText, type Document, type KnowledgeBase, type Version } from '../../shared/api'
const props = defineProps<{ token: string; admin: boolean; knowledge: KnowledgeBase[] }>()
const emit = defineEmits<{ changed: [] }>()
const kbId = ref(''), docs = ref<Document[]>([]), selected = ref<Document | null>(null), versions = ref<Version[]>([])
const chunks = ref<{ id: string; text: string; locator: Record<string, unknown> }[]>([]), error = ref(''), busy = ref(false)
const name = ref(''), organization = ref(''), title = ref(''), source = ref(''), file = ref<File | null>(null), fileInput = ref<HTMLInputElement | null>(null), duplicate = ref(false)
let generation = 0, timer: ReturnType<typeof setTimeout> | undefined, stopped = false
async function load() {
  const current = ++generation
  docs.value = []; versions.value = []; chunks.value = []; selected.value = null; error.value = ''
  if (!kbId.value) return
  try { const rows = await api<Document[]>(`/documents?kb_id=${encodeURIComponent(kbId.value)}`, props.token); if (current === generation) docs.value = rows }
  catch (reason) { if (current === generation) error.value = errorText(reason) }
}
async function poll() {
  const current = generation, id = kbId.value
  if (id && !busy.value) {
    try {
      const rows = await api<Document[]>(`/documents?kb_id=${encodeURIComponent(id)}`, props.token)
      if (current === generation) { docs.value = rows; if (selected.value) await showVersions(selected.value, true) }
    } catch (reason) { if (current === generation) { docs.value = []; versions.value = []; chunks.value = []; selected.value = null; error.value = errorText(reason) } }
  }
  if (!stopped) timer = setTimeout(() => { void poll() }, 3000)
}
async function action(work: () => Promise<unknown>) {
  busy.value = true; error.value = ''
  try { await work() } catch (reason) { error.value = errorText(reason) } finally { busy.value = false }
}
async function createKB() { await action(async () => { await api('/knowledge-bases', props.token, { method: 'POST', body: JSON.stringify({ name: name.value, organization: organization.value }) }); name.value = ''; organization.value = ''; emit('changed') }) }
async function defaultSearch() {
  const kb = props.knowledge.find(k => k.id === kbId.value); if (!kb) return
  await action(async () => { await api(`/knowledge-bases/${kb.id}/default-search`, props.token, { method: 'PATCH', body: JSON.stringify({ enabled: !kb.default_search }) }); emit('changed') })
}
async function upload(confirm = false) {
  if (!file.value || !kbId.value) return
  if (file.value.size > 20 * 1024 * 1024) { error.value = '文件不能超过 20 MiB'; return }
  busy.value = true; error.value = ''
  const body = new FormData(); body.append('file', file.value); body.append('title', title.value || file.value.name); body.append('source', source.value); body.append('confirm_duplicate', String(confirm)); body.append('kb_id', kbId.value)
  // 版本接口不接受 kb_id，防止前端把一个文档写入另一知识库。
  const path = selected.value ? `/documents/${selected.value.id}/versions` : '/documents'
  if (selected.value) body.delete('kb_id')
  try {
    await api(path, props.token, { method: 'POST', body }); duplicate.value = false; file.value = null; title.value = ''; source.value = ''; if (fileInput.value) fileInput.value.value = ''; await load()
  } catch (reason) { duplicate.value = reason instanceof ApiError && reason.code === 'duplicate_content'; error.value = errorText(reason) } finally { busy.value = false }
}
async function showVersions(doc: Document, keep = false) {
  const current = generation
  if (!keep) { versions.value = []; chunks.value = []; duplicate.value = false }
  selected.value = doc
  const rows = await api<Version[]>(`/documents/${doc.id}/versions`, props.token)
  if (current === generation && selected.value?.id === doc.id) versions.value = rows
}
async function toggle(doc: Document) { await action(async () => { await api(`/documents/${doc.id}/enabled`, props.token, { method: 'PATCH', body: JSON.stringify({ enabled: !doc.enabled }) }); await load(); emit('changed') }) }
async function preview(version: Version) { await action(async () => { chunks.value = []; chunks.value = await api(`/documents/${selected.value!.id}/versions/${version.id}/chunks`, props.token) }) }
async function retry(version: Version) { await action(async () => { await api(`/ingestion-jobs/${version.job_id}/retry`, props.token, { method: 'POST' }); if (selected.value) await showVersions(selected.value) }) }
onMounted(() => { void poll() })
onBeforeUnmount(() => { stopped = true; ++generation; clearTimeout(timer) })
</script>
<template>
  <section class="panel"><div class="section-heading"><h2>知识资料</h2><span class="count">{{ knowledge.length }}</span></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <form v-if="admin" class="inline-form" @submit.prevent="createKB"><input v-model="name" aria-label="知识库名称" placeholder="知识库名称" required maxlength="200"><input v-model="organization" aria-label="所属组织" placeholder="所属组织 / 学校" required maxlength="200"><button :disabled="busy">新建知识库</button></form>
    <label for="knowledge">选择知识库</label><select id="knowledge" v-model="kbId" :disabled="busy" @change="load"><option value="">请选择</option><option v-for="kb in knowledge" :key="kb.id" :value="kb.id">{{ kb.name }} · {{ kb.organization }}</option></select>
    <p v-if="!knowledge.length" class="empty">还没有可访问的资料，请联系管理员分配权限。</p>
    <div v-if="admin && kbId" class="actions"><button :disabled="busy" @click="defaultSearch">{{ knowledge.find(k => k.id === kbId)?.default_search ? '移出默认问答范围' : '加入默认问答范围' }}</button></div>
    <ul class="document-list"><li v-for="doc in docs" :key="doc.id"><div><strong>{{ doc.title }}</strong><p class="muted small">{{ doc.enabled ? '已启用' : '已停用' }} · 最新任务 {{ doc.job_status }} {{ doc.progress ?? 0 }}%{{ doc.active_version ? ' · 已有可用版本' : '' }}</p><p v-if="doc.error" class="error">{{ doc.error }}</p></div><div class="actions"><button :disabled="busy" @click="action(() => showVersions(doc))">查看版本</button><button v-if="admin" :disabled="busy" @click="toggle(doc)">{{ doc.enabled ? '停用' : '启用' }}</button></div></li></ul>
    <div v-if="selected" class="version-panel"><h3>{{ selected.title }} · 历史版本</h3><ul class="document-list"><li v-for="version in versions" :key="version.id"><span>{{ version.filename }} · {{ version.status }} · {{ version.progress ?? 0 }}%</span><div class="actions"><button @click="action(() => download(version.id, token))">下载</button><button v-if="version.status === 'completed'" @click="preview(version)">查看切片</button><button v-if="admin && version.status === 'failed'" @click="retry(version)">重试入库</button></div><p v-if="version.error" class="error">{{ version.error }}</p></li></ul><details v-for="chunk in chunks" :key="chunk.id"><summary>{{ chunk.id }} · {{ JSON.stringify(chunk.locator) }}</summary><pre>{{ chunk.text }}</pre></details></div>
    <form v-if="admin && kbId" @submit.prevent="upload(false)" class="upload-panel"><h3>{{ selected ? `追加版本：${selected.title}` : '上传新文档' }}</h3><button v-if="selected" type="button" @click="selected = null; duplicate = false">改为新文档</button><label>文件（PDF / DOCX / MD / TXT，最多 20 MiB）<input ref="fileInput" type="file" accept=".pdf,.docx,.md,.txt" required @change="file = ($event.target as HTMLInputElement).files?.[0] ?? null; duplicate = false"></label><label>文档标题<input v-model="title" maxlength="300" :required="!selected"></label><label>来源说明<input v-model="source" maxlength="1000"></label><button :disabled="busy || !file">上传并入库</button><div v-if="duplicate" class="notice"><p>检测到相同内容。确认后仍会保留已有文档与版本。</p><button type="button" :disabled="busy" @click="upload(true)">确认重复上传</button></div><p class="muted small">新文档默认仅管理员可见，请在权限管理中分配访问范围。</p></form>
  </section>
</template>
