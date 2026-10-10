CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS users (
 id text PRIMARY KEY, username text UNIQUE NOT NULL, password_hash text NOT NULL,
 role text NOT NULL CHECK(role IN ('admin','user'))
);
CREATE TABLE IF NOT EXISTS tokens (
 token_hash text PRIMARY KEY, user_id text NOT NULL REFERENCES users(id), expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_bases (
 id text PRIMARY KEY, name text NOT NULL, organization text NOT NULL, revision bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS documents (
 id text PRIMARY KEY, kb_id text NOT NULL REFERENCES knowledge_bases(id), title text NOT NULL,
 enabled boolean NOT NULL DEFAULT true, active_version text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS document_versions (
 id text PRIMARY KEY, document_id text NOT NULL REFERENCES documents(id), filename text NOT NULL,
 path text NOT NULL, content_hash text NOT NULL, source text NOT NULL DEFAULT '',
 status text NOT NULL DEFAULT 'queued', error text, parser_version text, embedding_model text,
 dimension integer, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chunks (
 id text PRIMARY KEY, version_id text NOT NULL REFERENCES document_versions(id), ordinal integer NOT NULL,
 text text NOT NULL, locator jsonb NOT NULL, content_hash text NOT NULL, embedding vector NOT NULL,
 UNIQUE(version_id, ordinal)
);
CREATE TABLE IF NOT EXISTS snapshots (
 id text PRIMARY KEY, kb_id text NOT NULL REFERENCES knowledge_bases(id), revision bigint NOT NULL,
 version_ids jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(kb_id, revision)
);
CREATE TABLE IF NOT EXISTS ingestion_jobs (
 id text PRIMARY KEY, version_id text UNIQUE NOT NULL REFERENCES document_versions(id),
 status text NOT NULL DEFAULT 'queued', attempts integer NOT NULL DEFAULT 0,
 lease_until timestamptz, lease_owner text, progress integer NOT NULL DEFAULT 0,
 error text, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessions (
 id text PRIMARY KEY, user_id text NOT NULL REFERENCES users(id), kb_id text NOT NULL REFERENCES knowledge_bases(id),
 title text NOT NULL DEFAULT '新会话', created_at timestamptz NOT NULL DEFAULT now()
);
-- 增量扩展查询范围，旧单库会话仍通过 kb_id 读取，不覆盖历史数据。
ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS default_search boolean NOT NULL DEFAULT true;
ALTER TABLE sessions ALTER COLUMN kb_id DROP NOT NULL;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS kb_ids jsonb NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS messages (
 id text PRIMARY KEY, session_id text NOT NULL REFERENCES sessions(id), request_id text NOT NULL,
 question text NOT NULL, answer text NOT NULL DEFAULT '', status text NOT NULL DEFAULT 'generating',
 snapshot_id text, citations jsonb NOT NULL DEFAULT '[]', trace jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(session_id,request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_generation_per_session ON messages(session_id) WHERE status='generating';
CREATE INDEX IF NOT EXISTS chunks_version ON chunks(version_id);
CREATE INDEX IF NOT EXISTS jobs_queue ON ingestion_jobs(status,lease_until);

-- 一次性迁移旧资料为全员可读；之后新文档默认仅管理员可读。
CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS usage_kind text NOT NULL DEFAULT 'demo';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'all_users';
ALTER TABLE documents ALTER COLUMN visibility SET DEFAULT 'groups';
CREATE TABLE IF NOT EXISTS user_groups(id text PRIMARY KEY, name text UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS user_group_members(
 group_id text NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE,
 user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY(group_id,user_id));
CREATE INDEX IF NOT EXISTS group_members_user ON user_group_members(user_id);
CREATE TABLE IF NOT EXISTS document_group_grants(
 document_id text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 group_id text NOT NULL REFERENCES user_groups(id) ON DELETE CASCADE, PRIMARY KEY(document_id,group_id));
CREATE TABLE IF NOT EXISTS authorization_state(id integer PRIMARY KEY CHECK(id=1), revision bigint NOT NULL DEFAULT 0);
INSERT INTO authorization_state(id) VALUES(1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS audit_events(
 id text PRIMARY KEY, actor_id text REFERENCES users(id), action text NOT NULL,
 target_id text NOT NULL, details jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE messages ADD COLUMN IF NOT EXISTS provenance jsonb NOT NULL DEFAULT '{}';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS usage_kind text NOT NULL DEFAULT 'demo';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS strategy_version text NOT NULL DEFAULT 'legacy';
INSERT INTO audit_events(id,action,target_id,details)
 SELECT 'migration-access-v2','migration','documents','{"existing_visibility":"all_users","new_default":"groups"}'
 WHERE NOT EXISTS(SELECT 1 FROM schema_migrations WHERE version=2) ON CONFLICT DO NOTHING;
INSERT INTO schema_migrations(version) VALUES(2) ON CONFLICT DO NOTHING;
