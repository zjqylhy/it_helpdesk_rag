<script setup lang="ts">
import type { Session } from '../../shared/api'
defineProps<{ items: Session[] }>()
</script>
<template>
  <section class="panel"><div class="section-heading"><h2>最近会话</h2><span class="count">{{ items.length }}</span></div>
    <p v-if="!items.length" class="muted">暂无会话。问答服务开放后，可以在这里查看历史记录。</p>
    <ul v-else class="session-list"><li v-for="item in items" :key="item.id"><span>{{ item.title }}</span><time>{{ new Date(item.created_at).toLocaleDateString('zh-CN') }}</time></li></ul>
  </section>
</template>
