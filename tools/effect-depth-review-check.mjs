import { chromium } from 'playwright'
import assert from 'node:assert/strict'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdir, writeFile } from 'node:fs/promises'

// 只打开显式指定的本地审核页，使用独立无头浏览器验证筛选和脚本安全。
const browser = await chromium.launch({channel:'msedge',headless:true})
const page = await browser.newPage({viewport:{width:1440,height:960}})
const errors=[]
page.on('pageerror',e=>errors.push(e.message))
const output=resolve('data/screenshots');await mkdir(output,{recursive:true})
try {
  await page.goto(pathToFileURL(resolve(process.argv[2])).href)
  assert.equal(await page.locator('details').count(),40)
  assert.equal(await page.locator('#count').textContent(),'显示 40 / 40 题')
  await page.locator('#scene').selectOption('comparison')
  assert.equal(await page.locator('details:visible').count(),12)
  await page.locator('#scene').selectOption('')
  await page.locator('#split').selectOption('dev')
  assert.equal(await page.locator('details:visible').count(),10)
  await page.locator('#split').selectOption('')
  await page.locator('#search').fill('scope-test-001')
  assert.equal(await page.locator('details:visible').count(),1)
  await page.locator('#expand').click()
  assert.equal(await page.locator('details[open]:visible').count(),1)
  await page.screenshot({path:resolve(output,'scope-candidate-detail.png'),fullPage:true})
  await page.locator('#search').fill('')
  await page.locator('details[open]').evaluateAll(cards=>cards.forEach(c=>c.open=false))
  await page.screenshot({path:resolve(output,'scope-candidate-review.png')})
  assert.deepEqual(errors,[])
  await writeFile(resolve(output,'scope-candidate-check.json'),JSON.stringify({total:40,comparison:12,dev:10,search:1,errors},null,2))
  console.log('审核页通过：40题、场景/划分/搜索/展开、脚本异常0。')
} finally {await browser.close()}
