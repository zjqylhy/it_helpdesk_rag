import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const encode = code => 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const apiUrl = encode(compile(await readFile(new URL('../src/shared/api.ts', import.meta.url), 'utf8')))
const { ApiError } = await import(apiUrl)
const pollingSource = compile(await readFile(new URL('../src/shared/polling.ts', import.meta.url), 'utf8'))
const { conversationPolling } = await import(encode(pollingSource.replace("'./api'", JSON.stringify(apiUrl))))
const state = await import(encode(compile(await readFile(new URL('../src/shared/chat-state.ts', import.meta.url), 'utf8'))))
const message = (answer, status = 'generating') => [{ id: 'm', request_id: 'r', question: 'q', answer, status, citations: [] }]
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(predicate) {
  const end = Date.now() + 1000
  while (!predicate()) { assert.ok(Date.now() < end, 'condition timed out'); await delay(5) }
}

test('全文替换，终态停止查询', async t => {
  const seen = [], rows = [message('中'), message('中文'), message('中文答案', 'completed')]
  let calls = 0
  const poll = conversationPolling(async () => rows[calls++], m => seen.push(m[0].answer), assert.fail, 10)
  t.after(() => poll.stop()); poll.open('s')
  await until(() => seen.length === 3)
  await delay(30)
  assert.deepEqual(seen, ['中', '中文', '中文答案']); assert.equal(calls, 3)
})
test('刷新发生在查询中时串行补查，不遗漏刚受理的任务', async t => {
  let release, calls = 0, active = 0, maximum = 0
  const poll = conversationPolling(async () => {
    active++; maximum = Math.max(maximum, active); calls++
    const result = calls === 1 ? await new Promise(resolve => { release = resolve }) : message('完成', 'completed')
    active--; return result
  }, () => {}, assert.fail, 10)
  t.after(() => poll.stop()); poll.open('s'); poll.refresh(); poll.refresh()
  release([])
  await until(() => calls === 2)
  assert.equal(maximum, 1)
})
test('切换会话丢弃旧响应，卸载后不再查询', async () => {
  let old, calls = 0
  const seen = []
  const poll = conversationPolling(async id => {
    calls++
    return id === 'old' ? await new Promise(resolve => { old = resolve }) : message('新会话', 'completed')
  }, m => seen.push(m[0].answer), assert.fail, 10)
  poll.open('old'); poll.open('new'); old(message('旧会话'))
  await until(() => seen.length === 1)
  poll.stop(); await delay(30)
  assert.deepEqual(seen, ['新会话']); assert.equal(calls, 2)
})
test('临时查询失败自动重查，保留已有展示', async t => {
  let calls = 0, failures = 0
  const seen = []
  const poll = conversationPolling(async () => {
    calls++
    if (calls === 2) throw new TypeError('network disconnected')
    return message(calls === 1 ? '草稿' : '完整答案', calls >= 3 ? 'completed' : 'generating')
  }, m => seen.push(m[0].answer), () => failures++, 10)
  t.after(() => poll.stop()); poll.open('s')
  await until(() => seen.length === 2)
  assert.deepEqual(seen, ['草稿', '完整答案']); assert.equal(failures, 1)
})
test('登录失效停止自动重试', async t => {
  let calls = 0, failures = 0
  const poll = conversationPolling(async () => { calls++; throw new ApiError(401, 'unauthorized', '登录已失效') }, assert.fail, () => failures++, 10)
  t.after(() => poll.stop()); poll.open('s'); await until(() => failures === 1); await delay(40)
  assert.equal(calls, 1)
})
test('待确认提交按用户隔离，确认与退出只清理对应用户', () => {
  const storage = {}
  Object.defineProperties(storage, {
    getItem: { value(key) { return this[key] ?? null } },
    setItem: { value(key, value) { this[key] = String(value) } },
    removeItem: { value(key) { delete this[key] } },
  })
  globalThis.localStorage = storage
  const pending = { session_id: 's', request_id: 'r', question: '原问题' }
  state.savePending('u1', pending); state.savePending('u2', { ...pending, request_id: 'r2' })
  state.selectSession('u1', 's')
  assert.deepEqual(state.pendingChats('u1'), [pending])
  state.forgetPending('u1', 'r'); assert.deepEqual(state.pendingChats('u1'), [])
  state.clearChatState('u1'); assert.equal(state.lastSession('u1'), '')
  assert.equal(state.pendingChats('u2').length, 1)
})
