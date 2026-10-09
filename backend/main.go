package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/joho/godotenv"
	"golang.org/x/crypto/bcrypt"
)

type app struct {
	db                        *pgxpool.Pool
	ragURL, internal, uploads string
	client                    *http.Client
}
type user struct{ ID, Role string }

func env(k, fallback string) string {
	if s := os.Getenv(k); s != "" {
		return s
	}
	return fallback
}
func id() string {
	b := make([]byte, 16)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b)
}
func digest(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }
func fail(c *gin.Context, code int, message string) {
	c.AbortWithStatusJSON(code, gin.H{"error": message})
}

func loadLocalEnvironment() {
	// 本地从 backend 启动时读取项目根配置；容器使用已注入的环境变量。
	_ = godotenv.Load("../.env")
}

func main() {
	loadLocalEnvironment()
	ctx := context.Background()
	cfg, e := pgxpool.ParseConfig(os.Getenv("DATABASE_URL"))
	if e != nil {
		log.Fatal("DATABASE_URL 配置错误")
	}
	cfg.MaxConns = 10
	if os.Getenv("DB_SIMPLE_PROTOCOL") == "true" {
		// PGlite 测试服务共用一个底层连接，不支持跨客户端的语句缓存。
		cfg.ConnConfig.DefaultQueryExecMode = pgx.QueryExecModeSimpleProtocol
	}
	db, e := pgxpool.NewWithConfig(ctx, cfg)
	if e != nil {
		log.Fatal(e)
	}
	defer db.Close()
	if e = db.Ping(ctx); e != nil {
		log.Fatal("数据库不可用: ", e)
	}
	schema, e := os.ReadFile(env("SCHEMA_PATH", "../db/schema.sql"))
	if e != nil {
		log.Fatal(e)
	}
	if _, e = db.Exec(ctx, string(schema)); e != nil {
		log.Fatal(e)
	}
	internal := os.Getenv("INTERNAL_TOKEN")
	if len(internal) < 24 {
		log.Fatal("INTERNAL_TOKEN 至少 24 字符")
	}
	a := &app{db: db, ragURL: env("RAG_URL", "http://127.0.0.1:8000"), internal: internal, uploads: env("UPLOAD_DIR", "../data/uploads"), client: &http.Client{Timeout: 10 * time.Minute}}
	if e = os.MkdirAll(a.uploads, 0700); e != nil {
		log.Fatal(e)
	}
	for _, account := range []struct{ name, password, role string }{{os.Getenv("ADMIN_USER"), os.Getenv("ADMIN_PASSWORD"), "admin"}, {os.Getenv("USER_NAME"), os.Getenv("USER_PASSWORD"), "user"}} {
		if account.name == "" || len(account.password) < 12 || len(account.password) > 72 {
			log.Fatal("演示账号名称不能为空，密码为 12–72 字节")
		}
		hash, _ := bcrypt.GenerateFromPassword([]byte(account.password), bcrypt.DefaultCost)
		// 本地配置是演示账号的唯一来源，修改配置后重启即可更新密码。
		_, e = db.Exec(ctx, `INSERT INTO users VALUES($1,$2,$3,$4) ON CONFLICT(username) DO UPDATE SET password_hash=$3,role=$4`, id(), account.name, string(hash), account.role)
		if e != nil {
			log.Fatal(e)
		}
	}
	// 重启时旧进程持有的生成任务已经无法继续，保留其不完整回答。
	_, _ = db.Exec(ctx, `UPDATE messages SET status='interrupted' WHERE status='generating'`)
	r := a.routes()
	srv := &http.Server{Addr: env("HTTP_ADDR", "127.0.0.1:8080"), Handler: r, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second}
	log.Println("业务服务监听 ", srv.Addr)
	log.Fatal(srv.ListenAndServe())
}

func (a *app) routes() *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/health", func(c *gin.Context) {
		if e := a.db.Ping(c); e != nil {
			fail(c, 503, "数据库不可用")
			return
		}
		c.JSON(200, gin.H{"status": "ok"})
	})
	api := r.Group("/api/v1")
	api.POST("/auth/login", a.login)
	api.Use(a.auth)
	api.POST("/auth/logout", func(c *gin.Context) {
		_, _ = a.db.Exec(c, `DELETE FROM tokens WHERE token_hash=$1`, digest(strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")))
		c.Status(204)
	})
	api.GET("/auth/me", func(c *gin.Context) { c.JSON(200, c.MustGet("user")) })
	api.GET("/knowledge-bases", a.listKB)
	api.POST("/knowledge-bases", a.admin, a.createKB)
	api.GET("/documents", a.listDocuments)
	api.POST("/documents", a.admin, a.upload)
	api.POST("/documents/:id/versions", a.admin, a.upload)
	api.GET("/documents/:id/versions", a.versions)
	api.GET("/documents/:id/versions/:version/chunks", a.chunks)
	api.PATCH("/documents/:id/enabled", a.admin, a.enable)
	api.POST("/ingestion-jobs/:id/retry", a.admin, a.retry)
	api.GET("/files/:version", a.file)
	api.GET("/sessions", a.listSessions)
	api.POST("/sessions", a.createSession)
	api.GET("/sessions/:id/messages", a.messages)
	api.POST("/chat", a.chat)
	return r
}
func (a *app) login(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 4096)
	var q struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if c.ShouldBindJSON(&q) != nil {
		fail(c, 400, "请输入账号和密码")
		return
	}
	var uid, hash, role string
	e := a.db.QueryRow(c, `SELECT id,password_hash,role FROM users WHERE username=$1`, q.Username).Scan(&uid, &hash, &role)
	if e != nil || bcrypt.CompareHashAndPassword([]byte(hash), []byte(q.Password)) != nil {
		fail(c, 401, "账号或密码错误")
		return
	}
	token := id() + id()
	_, e = a.db.Exec(c, `INSERT INTO tokens VALUES($1,$2,now()+interval '12 hours')`, digest(token), uid)
	if e != nil {
		fail(c, 500, "登录失败")
		return
	}
	c.JSON(200, gin.H{"token": token, "role": role, "username": q.Username})
}
func (a *app) auth(c *gin.Context) {
	header := c.GetHeader("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		fail(c, 401, "请先登录")
		return
	}
	var u user
	e := a.db.QueryRow(c, `SELECT u.id,u.role FROM tokens t JOIN users u ON u.id=t.user_id WHERE token_hash=$1 AND expires_at>now()`, digest(strings.TrimPrefix(header, "Bearer "))).Scan(&u.ID, &u.Role)
	if e != nil {
		fail(c, 401, "登录已过期")
		return
	}
	c.Set("user", u)
}
func (a *app) admin(c *gin.Context) {
	if c.MustGet("user").(user).Role != "admin" {
		fail(c, 403, "需要管理员权限")
	}
}
func (a *app) query(c *gin.Context, sql string, args ...any) {
	rows, e := a.db.Query(c, sql, args...)
	if e != nil {
		fail(c, 500, "数据读取失败")
		return
	}
	defer rows.Close()
	results := []map[string]any{}
	for rows.Next() {
		vals, e := rows.Values()
		if e != nil {
			fail(c, 500, "数据读取失败")
			return
		}
		m := map[string]any{}
		for i, f := range rows.FieldDescriptions() {
			m[string(f.Name)] = vals[i]
		}
		results = append(results, m)
	}
	if rows.Err() != nil {
		fail(c, 500, "数据读取失败")
		return
	}
	c.JSON(200, results)
}
func (a *app) listKB(c *gin.Context) { a.query(c, `SELECT * FROM knowledge_bases ORDER BY name`) }
func (a *app) createKB(c *gin.Context) {
	var q struct {
		Name         string `json:"name"`
		Organization string `json:"organization"`
	}
	if c.ShouldBindJSON(&q) != nil || strings.TrimSpace(q.Name) == "" || strings.TrimSpace(q.Organization) == "" {
		fail(c, 400, "填写名称和适用组织")
		return
	}
	uid := id()
	if _, e := a.db.Exec(c, `INSERT INTO knowledge_bases(id,name,organization) VALUES($1,$2,$3)`, uid, q.Name, q.Organization); e != nil {
		fail(c, 500, "创建失败")
		return
	}
	c.JSON(201, gin.H{"id": uid})
}
func (a *app) listDocuments(c *gin.Context) {
	a.query(c, `SELECT d.*, v.status AS active_status, j.id AS job_id,j.status AS job_status,j.progress,j.error,
 latest.id AS latest_version,latest.filename,latest.created_at AS version_created_at
 FROM documents d LEFT JOIN document_versions v ON v.id=d.active_version
 LEFT JOIN LATERAL (SELECT * FROM document_versions WHERE document_id=d.id ORDER BY created_at DESC,id DESC LIMIT 1) latest ON true
 LEFT JOIN ingestion_jobs j ON j.version_id=latest.id WHERE d.kb_id=$1 ORDER BY d.created_at DESC`, c.Query("kb_id"))
}
func (a *app) upload(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 21<<20)
	f, e := c.FormFile("file")
	if e != nil || f.Size > 20<<20 {
		fail(c, 400, "请选择不超过 20 MiB 的文件")
		return
	}
	if c.Request.MultipartForm != nil {
		defer c.Request.MultipartForm.RemoveAll()
	}
	ext := strings.ToLower(filepath.Ext(f.Filename))
	if !map[string]bool{".pdf": true, ".docx": true, ".md": true, ".txt": true}[ext] {
		fail(c, 400, "支持 PDF、DOCX、MD、TXT")
		return
	}
	docID, kb := c.Param("id"), c.PostForm("kb_id")
	if docID != "" {
		if a.db.QueryRow(c, `SELECT kb_id FROM documents WHERE id=$1`, docID).Scan(&kb) != nil {
			fail(c, 404, "文档不存在")
			return
		}
	}
	var exists bool
	_ = a.db.QueryRow(c, `SELECT EXISTS(SELECT 1 FROM knowledge_bases WHERE id=$1)`, kb).Scan(&exists)
	if !exists {
		fail(c, 404, "知识库不存在")
		return
	}
	src, e := f.Open()
	if e != nil {
		fail(c, 400, "无法读取文件")
		return
	}
	defer src.Close()
	version := id()
	path, e := filepath.Abs(filepath.Join(a.uploads, version+ext))
	if e != nil {
		fail(c, 500, "存储路径错误")
		return
	}
	out, e := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		fail(c, 500, "无法保存文件")
		return
	}
	h := sha256.New()
	_, e = io.Copy(io.MultiWriter(out, h), src)
	closeErr := out.Close()
	if e != nil || closeErr != nil {
		_ = os.Remove(path)
		fail(c, 500, "保存失败")
		return
	}
	keep := false
	defer func() {
		if !keep {
			_ = os.Remove(path)
		}
	}()
	checksum := hex.EncodeToString(h.Sum(nil))
	var duplicate bool
	_ = a.db.QueryRow(c, `SELECT EXISTS(SELECT 1 FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.kb_id=$1 AND v.content_hash=$2)`, kb, checksum).Scan(&duplicate)
	if duplicate && c.PostForm("confirm_duplicate") != "true" {
		fail(c, 409, "此知识库已有相同文件；确认后可继续上传")
		return
	}
	tx, e := a.db.Begin(c)
	if e != nil {
		fail(c, 500, "创建任务失败")
		return
	}
	defer tx.Rollback(c)
	if docID == "" {
		docID = id()
		title := strings.TrimSpace(c.PostForm("title"))
		if title == "" {
			title = filepath.Base(f.Filename)
		}
		_, e = tx.Exec(c, `INSERT INTO documents(id,kb_id,title) VALUES($1,$2,$3)`, docID, kb, title)
	}
	if e == nil {
		_, e = tx.Exec(c, `INSERT INTO document_versions(id,document_id,filename,path,content_hash,source) VALUES($1,$2,$3,$4,$5,$6)`, version, docID, filepath.Base(f.Filename), path, checksum, c.PostForm("source"))
	}
	job := id()
	if e == nil {
		_, e = tx.Exec(c, `INSERT INTO ingestion_jobs(id,version_id) VALUES($1,$2)`, job, version)
	}
	if e == nil {
		e = tx.Commit(c)
	}
	if e != nil {
		fail(c, 500, "创建任务失败")
		return
	}
	keep = true
	c.JSON(202, gin.H{"document_id": docID, "version_id": version, "job_id": job})
}
func (a *app) versions(c *gin.Context) {
	a.query(c, `SELECT v.id,v.filename,v.source,v.status,v.error,v.created_at,v.embedding_model,v.dimension,j.id AS job_id,j.progress FROM document_versions v LEFT JOIN ingestion_jobs j ON j.version_id=v.id WHERE document_id=$1 ORDER BY v.created_at DESC`, c.Param("id"))
}
func (a *app) chunks(c *gin.Context) {
	a.query(c, `SELECT c.id,c.text,c.locator,c.ordinal FROM chunks c JOIN document_versions v ON v.id=c.version_id WHERE v.document_id=$1 AND v.id=$2 ORDER BY ordinal`, c.Param("id"), c.Param("version"))
}

// 同一知识库的发布使用数据库行锁串行化，快照始终对应一次完整发布。
func publish(ctx context.Context, tx pgx.Tx, kb string) error {
	var rev int64
	if e := tx.QueryRow(ctx, `UPDATE knowledge_bases SET revision=revision+1 WHERE id=$1 RETURNING revision`, kb).Scan(&rev); e != nil {
		return e
	}
	_, e := tx.Exec(ctx, `INSERT INTO snapshots(id,kb_id,revision,version_ids) SELECT $1,$2,$3,COALESCE(jsonb_agg(active_version ORDER BY id),'[]') FROM documents WHERE kb_id=$2 AND enabled AND active_version IS NOT NULL`, id(), kb, rev)
	return e
}
func (a *app) enable(c *gin.Context) {
	var q struct {
		Enabled *bool `json:"enabled"`
	}
	if c.ShouldBindJSON(&q) != nil || q.Enabled == nil {
		fail(c, 400, "缺少 enabled")
		return
	}
	tx, e := a.db.Begin(c)
	if e != nil {
		fail(c, 500, "更新失败")
		return
	}
	defer tx.Rollback(c)
	var kb string
	e = tx.QueryRow(c, `SELECT kb_id FROM documents WHERE id=$1`, c.Param("id")).Scan(&kb)
	if errors.Is(e, pgx.ErrNoRows) {
		fail(c, 404, "文档不存在")
		return
	}
	if e == nil {
		_, e = tx.Exec(c, `SELECT id FROM knowledge_bases WHERE id=$1 FOR UPDATE`, kb)
	}
	if e == nil {
		_, e = tx.Exec(c, `UPDATE documents SET enabled=$2 WHERE id=$1`, c.Param("id"), *q.Enabled)
	}
	if e == nil {
		e = publish(c, tx, kb)
	}
	if e == nil {
		e = tx.Commit(c)
	}
	if e != nil {
		fail(c, 500, "更新失败")
		return
	}
	c.Status(204)
}
func (a *app) retry(c *gin.Context) {
	tx, e := a.db.Begin(c)
	if e != nil {
		fail(c, 500, "重试失败")
		return
	}
	defer tx.Rollback(c)
	var v string
	e = tx.QueryRow(c, `UPDATE ingestion_jobs SET status='queued',attempts=0,error=NULL,progress=0,lease_owner=NULL,lease_until=NULL WHERE id=$1 AND status='failed' RETURNING version_id`, c.Param("id")).Scan(&v)
	if e != nil {
		fail(c, 409, "仅失败任务可重试")
		return
	}
	_, e = tx.Exec(c, `UPDATE document_versions SET status='queued',error=NULL WHERE id=$1`, v)
	if e == nil {
		e = tx.Commit(c)
	}
	if e != nil {
		fail(c, 500, "重试失败")
		return
	}
	c.Status(202)
}
func (a *app) file(c *gin.Context) {
	var path, name string
	if a.db.QueryRow(c, `SELECT path,filename FROM document_versions WHERE id=$1`, c.Param("version")).Scan(&path, &name) != nil {
		fail(c, 404, "文件不存在")
		return
	}
	c.Header("X-Content-Type-Options", "nosniff")
	c.FileAttachment(path, name)
}
func (a *app) listSessions(c *gin.Context) {
	a.query(c, `SELECT * FROM sessions WHERE user_id=$1 ORDER BY created_at DESC`, c.MustGet("user").(user).ID)
}
func (a *app) createSession(c *gin.Context) {
	var q struct {
		KB string `json:"kb_id"`
	}
	if c.ShouldBindJSON(&q) != nil {
		fail(c, 400, "缺少知识库")
		return
	}
	uid := id()
	_, e := a.db.Exec(c, `INSERT INTO sessions(id,user_id,kb_id) VALUES($1,$2,$3)`, uid, c.MustGet("user").(user).ID, q.KB)
	if e != nil {
		fail(c, 400, "知识库不存在")
		return
	}
	c.JSON(201, gin.H{"id": uid})
}
func (a *app) owned(c *gin.Context, sid string) (string, bool) {
	var kb string
	e := a.db.QueryRow(c, `SELECT kb_id FROM sessions WHERE id=$1 AND user_id=$2`, sid, c.MustGet("user").(user).ID).Scan(&kb)
	if e != nil {
		fail(c, 404, "会话不存在")
		return "", false
	}
	return kb, true
}
func (a *app) messages(c *gin.Context) {
	if _, ok := a.owned(c, c.Param("id")); !ok {
		return
	}
	a.query(c, `SELECT * FROM messages WHERE session_id=$1 ORDER BY created_at,id`, c.Param("id"))
}

func (a *app) chat(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 32<<10)
	var q struct {
		Session  string `json:"session_id"`
		Question string `json:"question"`
		Request  string `json:"request_id"`
	}
	if c.ShouldBindJSON(&q) != nil || q.Request == "" || len(q.Request) > 128 || strings.TrimSpace(q.Question) == "" || len(q.Question) > 12000 {
		fail(c, 400, "问题或请求标识无效")
		return
	}
	kb, ok := a.owned(c, q.Session)
	if !ok {
		return
	}
	mid := id()
	_, e := a.db.Exec(c, `INSERT INTO messages(id,session_id,request_id,question) VALUES($1,$2,$3,$4)`, mid, q.Session, q.Request, q.Question)
	if e != nil {
		fail(c, 409, "该请求已保存，或会话正在生成；请刷新历史后重试")
		return
	}
	answer, status, snapshot := "", "interrupted", ""
	var citations any = []any{}
	var trace any = map[string]any{}
	// 客户端断开时使用独立的短上下文记录终态，避免取消信号丢失持久化。
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		citationsJSON, _ := json.Marshal(citations)
		traceJSON, _ := json.Marshal(trace)
		_, err := a.db.Exec(ctx, `UPDATE messages SET answer=$2,status=$3,snapshot_id=$4,citations=$5::jsonb,trace=$6::jsonb WHERE id=$1`, mid, answer, status, snapshot, string(citationsJSON), string(traceJSON))
		if err != nil {
			log.Println("消息保存失败 ", mid)
		}
	}()
	_, _ = a.db.Exec(c, `UPDATE sessions SET title=$2 WHERE id=$1 AND title='新会话'`, q.Session, string([]rune(q.Question)[:min(30, len([]rune(q.Question)))]))
	history := []map[string]string{}
	rows, e := a.db.Query(c, `SELECT question,answer FROM (SELECT question,answer,created_at FROM messages WHERE session_id=$1 AND status='completed' ORDER BY created_at DESC LIMIT 4) h ORDER BY created_at`, q.Session)
	if e != nil {
		status = "error"
		fail(c, 500, "历史读取失败")
		return
	}
	for rows.Next() {
		var question, ans string
		_ = rows.Scan(&question, &ans)
		history = append(history, map[string]string{"role": "user", "content": question}, map[string]string{"role": "assistant", "content": ans})
	}
	rows.Close()
	body, _ := json.Marshal(gin.H{"kb_id": kb, "question": q.Question, "history": history, "request_id": q.Request})
	req, e := http.NewRequestWithContext(c.Request.Context(), "POST", a.ragURL+"/internal/chat", bytes.NewReader(body))
	if e != nil {
		status = "error"
		fail(c, 500, "服务请求失败")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", a.internal)
	resp, e := a.client.Do(req)
	if e != nil {
		status = "error"
		fail(c, 502, "RAG 服务不可用")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		status = "error"
		fail(c, 502, "RAG 服务拒绝请求")
		return
	}
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("X-Accel-Buffering", "no")
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 4096), 2<<20)
	event := ""
	terminal := false
	seq := 0
	send := func(kind string, data map[string]any) {
		seq++
		data["request_id"] = q.Request
		data["seq"] = seq
		data["message_id"] = mid
		b, _ := json.Marshal(data)
		_, _ = fmt.Fprintf(c.Writer, "event: %s\ndata: %s\n\n", kind, b)
		c.Writer.Flush()
	}
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "event: ") {
			event = strings.TrimPrefix(line, "event: ")
		}
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		var data map[string]any
		if json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &data) != nil {
			status = "error"
			break
		}
		switch event {
		case "answer_delta":
			v, _ := data["text"].(string)
			answer += v
		case "refusal":
			answer, _ = data["text"].(string)
		case "evidence":
			snapshot, _ = data["snapshot_id"].(string)
		case "citations":
			citations = data["citations"]
		case "done":
			terminal = true
			status, _ = data["status"].(string)
			if status != "completed" && status != "refused" && status != "clarify" {
				status = "error"
			}
			trace = data["trace"]
		case "error":
			terminal = true
			status = "error"
			trace = data
		}
		send(event, data)
		if terminal {
			break
		}
	}
	if !terminal && c.Request.Context().Err() == nil {
		status = "error"
		send("error", map[string]any{"error": "上游流未正常结束，回答未验证"})
	}
}
