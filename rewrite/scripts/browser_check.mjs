import { chromium } from '../frontend/node_modules/playwright/index.mjs'
import { mkdir, writeFile } from 'node:fs/promises'

const folder = process.env.HD_QA_ARTIFACTS
if (!folder) throw Error('HD_QA_ARTIFACTS is required')
await mkdir(folder, { recursive: true })
const browser = await chromium.launch({
  executablePath: process.env.HD_BROWSER ?? (process.platform === 'win32' ? 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe' : undefined),
  headless: true,
})
const checks = [], errors = []
let page
try {
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(process.env.HD_WEB_TARGET)
  await page.getByLabel('用户名').fill('qa-admin')
  await page.getByLabel('密码', { exact: true }).fill('wrong')
  await page.getByRole('button', { name: '进入工作台' }).click()
  await page.getByRole('alert').filter({ hasText: '用户名或密码错误' }).waitFor()
  checks.push('invalid login')
  await page.getByLabel('密码', { exact: true }).fill('QaAdminTest123!')
  await page.getByRole('button', { name: '进入工作台' }).click()
  await page.getByRole('heading', { name: '公开知识库' }).waitFor()
  await page.getByRole('heading', { name: '受限知识库' }).waitFor()
  await page.getByText('问答服务暂未开放', { exact: true }).waitFor()
  await page.screenshot({ path: folder + '/admin-desktop.png', fullPage: true })
  checks.push('admin login and real lists')
  await page.reload()
  await page.getByRole('heading', { name: '公开知识库' }).waitFor()
  checks.push('session restore')
  await page.getByRole('button', { name: '退出登录' }).click()
  await page.getByLabel('用户名').fill('qa-user')
  await page.getByLabel('密码', { exact: true }).fill('QaUserTest123!')
  await page.getByRole('button', { name: '进入工作台' }).click()
  await page.getByRole('heading', { name: '公开知识库' }).waitFor()
  if (await page.getByRole('heading', { name: '受限知识库' }).count()) throw Error('Restricted KB leaked')
  checks.push('member scope')
  await page.setViewportSize({ width: 390, height: 844 })
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw Error('Mobile overflow')
  await page.screenshot({ path: folder + '/member-mobile.png', fullPage: true })
  checks.push('mobile 390px')
  await page.route('**/api/v1/knowledge-bases', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: '服务暂时不可用' }) }))
  await page.getByRole('button', { name: '刷新资料' }).click()
  await page.getByRole('alert').filter({ hasText: '服务暂时不可用' }).waitFor()
  if (await page.getByRole('heading', { name: '公开知识库' }).count()) throw Error('Stale metadata retained on failure')
  checks.push('failed refresh clears stale metadata')
  await page.getByRole('button', { name: '退出登录' }).click()
  await page.getByLabel('用户名').waitFor()
  if (await page.evaluate(() => sessionStorage.getItem('helpdesk.token'))) throw Error('Token retained')
  checks.push('logout clears token')
  if (errors.length) throw Error(errors.join('; '))
  await writeFile(folder + '/browser-check.json', JSON.stringify({ checks, page_errors: errors, viewport: [1440,1000], mobile: [390,844] }, null, 2))
  console.log(JSON.stringify({ browser_checks: checks.length, page_errors: errors.length }))
} catch (error) {
  if (page) {
    await page.screenshot({ path: folder + '/failure.png', fullPage: true })
    console.error(JSON.stringify({ page_errors: errors, text: await page.locator('body').innerText(), checks }))
  }
  throw error
} finally { await browser.close() }
