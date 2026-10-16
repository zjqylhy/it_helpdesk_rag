<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import LoginView from './features/auth/LoginView.vue'
import { useAuth } from './features/auth/useAuth'
import KnowledgeList from './features/knowledge/KnowledgeList.vue'
import ConversationList from './features/chat/ConversationList.vue'
import { api, ApiError, type KnowledgeBase, type Session } from './shared/api'

const auth = useAuth(), ready = ref(false), busy = ref(false), error = ref('')
const knowledge = ref<KnowledgeBase[]>([]), sessions = ref<Session[]>([])
let generation = 0
// 登录成功会卸载登录页，依赖身份状态加载数据，避免丢失子组件卸载后的通知。
watch(auth.principal, principal => { if (principal) void load() })
async function load() {
  const current = ++generation
  const currentToken = auth.token.value
  error.value = ''; busy.value = true
  try {
    const [k, s] = await Promise.all([api<KnowledgeBase[]>('/knowledge-bases', currentToken), api<Session[]>('/sessions', currentToken)])
    if (current === generation) { knowledge.value = k; sessions.value = s }
  } catch (reason) {
    if (current !== generation) return
    // 权限或网络变化时清空旧列表，不继续显示可能失权的元数据。
    knowledge.value = []; sessions.value = []
    if (reason instanceof ApiError && reason.status === 401) auth.clear()
    error.value = reason instanceof Error ? reason.message : '加载失败'
  } finally { if (current === generation) busy.value = false }
}
async function logout() {
  ++generation; knowledge.value = []; sessions.value = []; error.value = ''
  try { await auth.logout() } catch { /* 本地登录状态已清理。 */ }
}
onMounted(async () => {
  try { await auth.restore() }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '登录失效' }
  finally { ready.value = true }
})
</script>

<template>
  <div v-if="!ready" class="loading" role="status">正在打开知识空间…</div>
  <LoginView v-else-if="!auth.principal.value" />
  <div v-else class="workspace">
    <header><a class="brand" href="/">知答<span>知识助手</span></a><div class="account"><span>{{ auth.principal.value.Role === 'admin' ? '管理员' : '成员' }}</span><button @click="logout">退出登录</button></div></header>
    <main class="dashboard"><div class="welcome"><div><span class="eyebrow">你的知识空间</span><h1>从可信资料开始。</h1><p class="muted">查看可访问的知识库与最近会话。</p></div><button :disabled="busy" @click="load">{{ busy ? '正在刷新…' : '刷新资料' }}</button></div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <div class="notice"><span>◌</span><div><strong>问答服务暂未开放</strong><p>当前可以登录并查看知识库、会话列表。服务开放后即可发起提问。</p></div></div>
      <KnowledgeList :items="knowledge" /><ConversationList :items="sessions" />
    </main><footer>知答 · 有据可查，依权可见</footer>
  </div>
</template>
