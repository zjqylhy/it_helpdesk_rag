<script setup lang="ts">
import type { KnowledgeBase } from '../../shared/api'
defineProps<{ items: KnowledgeBase[] }>()
</script>
<template>
  <section class="panel"><div class="section-heading"><h2>我的知识库</h2><span class="count">{{ items.length }}</span></div>
    <div v-if="!items.length" class="empty"><span class="empty-icon">▤</span><h3>还没有可访问的资料</h3><p>请联系管理员添加资料或分配访问权限。</p></div>
    <ul v-else class="kb-grid"><li v-for="item in items" :key="item.id"><span class="doc-icon">▤</span><h3>{{ item.name }}</h3><p>{{ item.organization }}</p><span class="tag">{{ item.default_search ? '默认问答范围' : '指定后可查询' }}</span></li></ul>
  </section>
</template>
