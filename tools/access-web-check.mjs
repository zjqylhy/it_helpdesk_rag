import { chromium } from 'playwright'
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'

// 独立无头浏览器只使用契约测试账号，不读取用户浏览器登录信息。
const browser = await chromium.launch({ channel: 'msedge', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } })
const errors = []
page.on('pageerror', e => errors.push(e.message))
try {
  await page.goto(process.env.TEST_WEB_URL || 'http://127.0.0.1:18173')
  await page.getByLabel('账号', { exact: true }).fill('admin')
  await page.getByLabel('密码', { exact: true }).fill('test-admin-password')
  await page.getByRole('button', { name: '登录工作台 →' }).click()
  await page.getByRole('heading', { name: '今天有什么需要解决？' }).waitFor()
  await page.getByRole('button', { name: '用户与权限', exact: false }).click()
  await page.getByRole('heading', { name: '用户与数据来源' }).waitFor()
  await page.getByText('受限资料成员', { exact: true }).waitFor()
  assert.equal(await page.locator('.access-row').count(), 2)
  await page.getByPlaceholder('新用户账号').fill('web-contract-user')
  await page.getByPlaceholder('密码至少12字节').fill('web-contract-password')
  await page.getByRole('button', { name: '创建普通用户', exact: true }).click()
  await page.locator('.access-row').filter({ hasText: 'web-contract-user' }).waitFor()
  await page.getByRole('button', { name: '知识管理', exact: false }).click()
  await page.locator('.sidebar select').selectOption({ label: '权限契约库 · 权限测试组织' })
  await page.getByRole('button', { name: '版本与切片' }).first().click()
  await page.getByRole('heading', { name: '文档访问权限' }).waitFor()
  await page.getByLabel('可见范围', { exact: false }).selectOption('groups')
  for (const checkbox of await page.locator('.access-panel input[type=checkbox]').all()) await checkbox.uncheck()
  await page.getByRole('button', { name: '保存文档权限' }).click()
  await page.getByText('未授权任何用户组，仅管理员可以读取。').waitFor()
  await mkdir('../data/screenshots', { recursive: true })
  await page.screenshot({ path: '../data/screenshots/document-access.png', fullPage: true })
  assert.deepEqual(errors, [])
  console.log('权限页面通过：用户与用户组加载、创建用户、文档范围保存及受限提示，无脚本错误。')
} finally { await browser.close() }
