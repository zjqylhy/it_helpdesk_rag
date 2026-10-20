<template>
  <!-- 备案信息放在身份分支之外，保证访客和已登录用户都能查看。 -->
  <footer class="site-footer">
    <span>知答 · 有据可查，依权可见</span>
    <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">粤ICP备2026161349号</a>
  </footer>
</template>

<style scoped>
.site-footer{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:8px 20px;padding:24px;color:#65796b;font-size:12px;text-align:center}
.site-footer a{color:inherit;text-decoration:none}
.site-footer a:hover{text-decoration:underline}
</style>
