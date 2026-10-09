import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'
import assert from 'node:assert/strict'

// 使用独立无头浏览器，不读取用户浏览器的账号或历史。
const browser = await chromium.launch({channel:'msedge',headless:true})
const page = await browser.newPage({viewport:{width:1440,height:960}})
const errors=[]
page.on('pageerror',error=>errors.push(error.message))
await mkdir('../data/screenshots',{recursive:true})
try {
 await page.goto('http://127.0.0.1:5173')
 await page.screenshot({path:'../data/screenshots/login.png',fullPage:true})
 await page.getByLabel('账号',{exact:true}).fill('admin')
 await page.getByLabel('密码',{exact:true}).fill('test-admin-password')
 await page.getByRole('button',{name:'登录工作台 →'}).click()
 await page.getByRole('heading',{name:'今天有什么需要解决？'}).waitFor()
 await page.screenshot({path:'../data/screenshots/chat-empty.png',fullPage:true})
 await page.getByPlaceholder('描述问题，Shift + Enter 换行…').fill('如何连接？')
 await page.getByRole('button',{name:'发送问题'}).click()
 await page.getByText('已完成',{exact:true}).waitFor()
 await page.getByText('回答依据 · 1 个来源').waitFor()
 await page.locator('.citations summary').click()
 await page.screenshot({path:'../data/screenshots/chat-answer.png',fullPage:true})
 await page.getByRole('button',{name:'知识管理',exact:false}).click()
 await page.getByRole('heading',{name:'文档与版本'}).waitFor()
 await page.getByRole('button',{name:'版本与切片'}).first().click()
 await page.getByRole('heading',{name:/版本记录/}).waitFor()
 await page.screenshot({path:'../data/screenshots/knowledge.png',fullPage:true})
 await page.reload()
 await page.getByRole('heading',{name:'今天有什么需要解决？'}).waitFor()
 assert.equal(errors.length,0,errors.join('\n'))
 console.log('Web 验证通过：登录、知识库加载、流式回答、引用展开、知识管理、版本切片和登录恢复。')
} finally {await browser.close()}
