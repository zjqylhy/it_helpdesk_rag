import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/shared/sse.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const { consumeSSE } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))
function response(text, oneByte = false) {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({ start(controller) {
    if (oneByte) for (const value of bytes) controller.enqueue(Uint8Array.of(value))
    else controller.enqueue(bytes)
    controller.close()
  } }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } })
}
const frame = (event, seq, extra = {}) => `event: ${event}\r\ndata: ${JSON.stringify({ request_id: 'r', message_id: 'm', seq, ...extra })}\r\n\r\n`
test('UTF-8 single byte chunks and CRLF preserve Chinese text', async () => {
  const events = []
  await consumeSSE(response(': ping\r\n\r\n' + frame('answer_delta', 1, { text: '中文' }) + frame('done', 2, { status: 'completed' }), true), 'r', e => events.push(e))
  assert.equal(events[0].data.text, '中文'); assert.equal(events.length, 2)
})
test('multi-data event and LF parsing', async () => {
  const events = []
  await consumeSSE(response('event: done\ndata: {"request_id":"r",\ndata: "message_id":"m","seq":1,"status":"refused"}\n\n'), 'r', e => events.push(e))
  assert.equal(events[0].data.status, 'refused')
})
for (const [name, text] of [
  ['missing terminal', frame('answer_delta', 1, { text: 'partial' })],
  ['duplicate terminal', frame('done', 1) + frame('done', 2)],
  ['sequence gap', frame('status', 2)],
  ['request mismatch', frame('done', 1, { request_id: 'other' })],
  ['message mismatch', frame('status', 1) + frame('done', 2, { message_id: 'other' })],
  ['unknown graph state', frame('updates', 1)],
  ['partial final event', frame('done', 1).trimEnd()],
]) test(name, async () => { await assert.rejects(() => consumeSSE(response(text), 'r', () => {})) })
