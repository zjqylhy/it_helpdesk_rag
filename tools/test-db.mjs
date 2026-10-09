// 仅用于无 Docker 环境中的协议/SQL 验证，正式运行使用原生 PostgreSQL。
import { PGlite } from '@electric-sql/pglite'
import { vector } from '@electric-sql/pglite-pgvector'
import { PGLiteSocketServer } from '@electric-sql/pglite-socket'
import { readFile } from 'node:fs/promises'
const db = await PGlite.create({extensions: {vector}})
await db.exec(await readFile(new URL('../db/schema.sql',import.meta.url),'utf8'))
const server = new PGLiteSocketServer({db,host:'127.0.0.1',port:55432,maxConnections:20})
await server.start()
console.log('测试 PostgreSQL 协议服务监听 127.0.0.1:55432（PGlite）')
async function stop(){await server.stop();await db.close();process.exit(0)}
process.on('SIGINT',stop);process.on('SIGTERM',stop)
