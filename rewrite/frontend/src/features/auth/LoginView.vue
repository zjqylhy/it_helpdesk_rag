<script setup lang="ts">
import { ref } from 'vue'
import { useAuth } from './useAuth'

const username = ref(''), password = ref(''), error = ref(''), busy = ref(false)
const auth = useAuth()
async function submit() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await auth.login(username.value, password.value); password.value = '' }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '登录失败' }
  finally { busy.value = false }
}
</script>

<template>
  <main class="login-layout">
    <section class="intro"><span class="eyebrow">知答 / KNOWLEDGE ASSISTANT</span><h1>让知识，<br>成为可靠的答案。</h1><p>在授权的资料范围内查找信息，<br>让每个回答都有出处。</p></section>
    <form class="login-card" @submit.prevent="submit">
      <span class="eyebrow">欢迎回来</span><h2>登录知识空间</h2>
      <label for="username">用户名</label><input id="username" v-model="username" autocomplete="username" required maxlength="100">
      <label for="password">密码</label><input id="password" v-model="password" type="password" autocomplete="current-password" required>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="primary" :disabled="busy">{{ busy ? '正在登录…' : '进入工作台 →' }}</button>
      <p class="muted small">使用管理员为你创建的账号登录。</p>
    </form>
  </main>
</template>
