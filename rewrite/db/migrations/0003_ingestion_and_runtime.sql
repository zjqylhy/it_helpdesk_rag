-- 用显式顺序而非随机 ID 决定版本新旧，防止晚完成的旧任务覆盖新版本。
ALTER TABLE document_versions ADD COLUMN ingest_order bigint;
WITH numbered AS (
 SELECT id,row_number() OVER(ORDER BY created_at,id) AS n FROM document_versions
) UPDATE document_versions v SET ingest_order=n.n FROM numbered n WHERE n.id=v.id;
CREATE SEQUENCE app_version_order;
SELECT setval('app_version_order',GREATEST(COALESCE(max(ingest_order),0),1),count(*)>0) FROM document_versions;
ALTER TABLE document_versions ALTER COLUMN ingest_order SET DEFAULT nextval('app_version_order');
ALTER TABLE document_versions ALTER COLUMN ingest_order SET NOT NULL;
CREATE UNIQUE INDEX versions_ingest_order ON document_versions(ingest_order);
ALTER TABLE messages ADD COLUMN completion_kind text;
ALTER TABLE model_attempts ADD COLUMN finished_at timestamptz;
