<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api } from './api'

const props = defineProps<{ documentId?: string }>()
const emit = defineEmits<{ updated: [] }>()
type Row = Record<string, any>
const users = ref<Row[]>([]), groups = ref<Row[]>([]), error = ref(''), busy = ref(false)
const name = ref(''), password = ref(''), usage = ref('demo'), groupName = ref('')
const visibility = ref('groups'), grants = ref<string[]>([])
const kinds: Record<string, string> = { demo: '演示', self_test: '开发自测', engineering_test: '工程测试', real_usage: '真实参与者' }
async function load() {
  groups.value = await api('/admin/groups')
  if (props.documentId) {
    const result = await api(`/admin/documents/${props.documentId}/access`)
    if (!result.length) throw new Error('文档不存在')
    visibility.value = result[0].visibility; grants.value = result[0].group_ids
  } else users.value = await api('/admin/users')
}
async function run(fn: () => Promise<void>) {
  error.value = ''; busy.value = true
  try { await fn(); await load(); emit('updated') } catch (e) { error.value = (e as Error).message } finally { busy.value = false }
}
async function createUser() {
  await run(async () => { await api('/admin/users', 'POST', { username: name.value, password: password.value, usage_kind: usage.value }); name.value = ''; password.value = '' })
}
async function createGroup() { await run(async () => { await api('/admin/groups', 'POST', { name: groupName.value }); groupName.value = '' }) }
async function saveMembers(group: Row) { await run(async () => { await api(`/admin/groups/${group.id}/members`, 'PUT', { user_ids: group.user_ids }) }) }
async function saveAccess() { await run(async () => { await api(`/admin/documents/${props.documentId}/access`, 'PUT', { visibility: visibility.value, group_ids: visibility.value === 'groups' ? grants.value : [] }) }) }
async function updateUser(user: Row, value: Row) { await run(async () => { await api(`/admin/users/${user.id}`, 'PATCH', value) }) }
onMounted(() => { void run(load) })
watch(() => props.documentId, () => { void run(load) })
</script>

<template>
  <section class="access-panel">
    <p v-if="error" class="notice error" role="alert">{{ error }}</p>
    <template v-if="documentId">
      <h3>文档访问权限</h3><p class="muted">权限适用于全部版本、问答引用和原文件。撤销权限会中止在途生成，并隐藏依赖该文档的旧回答。</p>
      <label>可见范围 <select v-model="visibility" :disabled="busy"><option value="groups">指定用户组</option><option value="all_users">所有已登录用户</option></select></label>
      <div v-if="visibility === 'groups'" class="access-options"><label v-for="group in groups" :key="group.id" class="check"><input v-model="grants" type="checkbox" :value="group.id" :disabled="busy">{{ group.name }}</label><p v-if="!grants.length" class="muted">未授权任何用户组，仅管理员可以读取。</p></div>
      <button class="primary" :disabled="busy" @click="saveAccess">保存文档权限</button>
    </template>
    <template v-else>
      <h3>用户与数据来源</h3><p class="muted">只将实际参与者标为真实使用；来源变更仅影响后续问答，旧记录不会重新分类。</p>
      <form class="access-form" @submit.prevent="createUser"><input v-model="name" placeholder="新用户账号" maxlength="128" required><input v-model="password" type="password" autocomplete="new-password" placeholder="密码至少12字节" required><select v-model="usage"><option v-for="(label,key) in kinds" :key="key" :value="key">{{ label }}</option></select><button class="primary" :disabled="busy">创建普通用户</button></form>
      <div v-for="user in users" :key="user.id" class="access-row"><b>{{ user.username }}</b><span>{{ user.role === 'admin' ? '管理员' : user.disabled ? '已停用' : '普通用户' }}</span><select :value="user.usage_kind" :disabled="busy || user.role === 'admin'" @change="updateUser(user,{usage_kind: ($event.target as HTMLSelectElement).value})"><option v-for="(label,key) in kinds" :key="key" :value="key">{{ label }}</option></select><button v-if="user.role !== 'admin'" class="outline" :disabled="busy" @click="updateUser(user,{disabled: !user.disabled})">{{ user.disabled ? '启用' : '停用账号' }}</button></div>
      <h3>用户组</h3><form class="access-form" @submit.prevent="createGroup"><input v-model="groupName" placeholder="用户组名称" maxlength="128" required><button class="primary" :disabled="busy">创建用户组</button></form>
      <section v-for="group in groups" :key="group.id" class="access-group"><h4>{{ group.name }}</h4><div class="access-options"><label v-for="user in users.filter(u => u.role !== 'admin')" :key="user.id" class="check"><input v-model="group.user_ids" type="checkbox" :value="user.id" :disabled="busy">{{ user.username }}{{ user.disabled ? '（已停用）' : '' }}</label></div><button class="outline" :disabled="busy" @click="saveMembers(group)">保存成员</button></section>
      <p class="muted">文档授权在「知识管理 → 版本与切片 → 文档访问权限」配置。多个用户组取并集。</p>
    </template>
  </section>
</template>
