-- 旧绝对路径仅保留作迁移依据；映射并核对文件哈希之前不开放文件读取。
ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS storage_key text;
CREATE UNIQUE INDEX IF NOT EXISTS versions_storage_key ON document_versions(storage_key) WHERE storage_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS model_runs (
 id text PRIMARY KEY, message_id text REFERENCES messages(id), manifest jsonb NOT NULL,
 status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS model_attempts (
 id text PRIMARY KEY, run_id text NOT NULL REFERENCES model_runs(id), stage text NOT NULL,
 reserved_tokens integer NOT NULL CHECK(reserved_tokens>=0), input_tokens integer, output_tokens integer,
 status text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
