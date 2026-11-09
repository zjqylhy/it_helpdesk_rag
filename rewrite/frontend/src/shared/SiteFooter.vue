<template>
  <!-- 备案信息放在身份分支之外，保证访客和已登录用户都能查看。 -->
  <footer class="site-footer">
    <span>知答 · 有据可查，依权可见</span>
    <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">粤ICP备2026161349号</a>
    <a class="public-security-filing" href="https://beian.mps.gov.cn/#/query/webSearch?code=44030002019027" target="_blank" rel="noopener noreferrer">
      <img src="/beian.png" width="18" height="20" alt="">
      <span>粤公网安备44030002019027号</span>
    </a>
  </footer>
</template>

<style scoped>
.site-footer{display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:8px 20px;padding:24px;color:#65796b;font-size:12px;text-align:center}
.site-footer a{color:inherit;text-decoration:none;white-space:nowrap}
.public-security-filing{display:inline-flex;align-items:center;gap:6px}
.public-security-filing img{display:block;flex-shrink:0}
.site-footer a:hover{text-decoration:underline}
</style>
