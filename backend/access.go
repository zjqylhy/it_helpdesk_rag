package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

// 所有内容入口共用文档授权条件，组授权取并集。
func documentAccess(uid, admin string) string {
	return `(` + admin + `::boolean OR d.visibility='all_users' OR EXISTS(
 SELECT 1 FROM document_group_grants g JOIN user_group_members m ON m.group_id=g.group_id
 WHERE g.document_id=d.id AND m.user_id=` + uid + `))`
}

func (a *app) policyRead(c *gin.Context) {
	// 流式问答逐片段加锁；授权写入独占边界，不能在等待模型时阻止撤销。
	if c.Request.URL.Path == "/api/v1/chat" || (strings.HasPrefix(c.Request.URL.Path, "/api/v1/admin/") && c.Request.Method != "GET") {
		c.Next()
		return
	}
	a.policy.RLock()
	defer a.policy.RUnlock()
	var active bool
	if a.db.QueryRow(c, `SELECT NOT disabled FROM users WHERE id=$1`, c.MustGet("user").(user).ID).Scan(&active) != nil || !active {
		fail(c, 401, "登录已失效")
		return
	}
	c.Next()
}

func (a *app) requireDocument(c *gin.Context, doc string) bool {
	u := c.MustGet("user").(user)
	var allowed bool
	err := a.db.QueryRow(c, `SELECT true FROM documents d WHERE d.id=$1 AND `+documentAccess("$2", "$3"), doc, u.ID, u.Role == "admin").Scan(&allowed)
	if err != nil {
		fail(c, 404, "资料不存在或不可访问")
		return false
	}
	return allowed
}

func (a *app) accessRoutes(api *gin.RouterGroup) {
	admin := api.Group("/admin", a.admin)
	admin.GET("/users", func(c *gin.Context) {
		a.query(c, `SELECT id,username,role,disabled,usage_kind FROM users ORDER BY username`)
	})
	admin.POST("/users", a.createUser)
	admin.PATCH("/users/:id", a.updateUser)
	admin.GET("/groups", func(c *gin.Context) {
		a.query(c, `SELECT g.id,g.name,COALESCE((SELECT jsonb_agg(user_id ORDER BY user_id) FROM user_group_members WHERE group_id=g.id),'[]') AS user_ids FROM user_groups g ORDER BY name`)
	})
	admin.POST("/groups", a.createGroup)
	admin.PUT("/groups/:id/members", a.groupMembers)
	admin.DELETE("/groups/:id", a.deleteGroup)
	admin.GET("/documents/:id/access", func(c *gin.Context) {
		a.query(c, `SELECT d.id,d.visibility,COALESCE((SELECT jsonb_agg(group_id ORDER BY group_id) FROM document_group_grants WHERE document_id=d.id),'[]') AS group_ids FROM documents d WHERE id=$1`, c.Param("id"))
	})
	admin.PUT("/documents/:id/access", a.documentGrants)
	admin.GET("/audit", func(c *gin.Context) {
		a.query(c, `SELECT id,actor_id,action,target_id,details,created_at FROM audit_events ORDER BY created_at DESC,id DESC LIMIT 200`)
	})
}

// 权限变化、审计与版本一起提交；确认撤销前取消旧生成，后续片段不能越过边界。
func (a *app) changeAccess(c *gin.Context, action, target string, details any, change func(pgx.Tx) error) {
	a.policy.Lock()
	defer a.policy.Unlock()
	tx, err := a.db.Begin(c)
	if err != nil {
		fail(c, 503, "数据库暂不可用")
		return
	}
	defer tx.Rollback(context.Background())
	var admin bool
	err = tx.QueryRow(c, `SELECT role='admin' AND NOT disabled FROM users WHERE id=$1`, c.MustGet("user").(user).ID).Scan(&admin)
	if err != nil || !admin {
		fail(c, 403, "需要有效管理员权限")
		return
	}
	if err = change(tx); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			fail(c, 404, "对象不存在")
		} else {
			fail(c, 409, "设置无效或名称重复")
		}
		return
	}
	if _, err = tx.Exec(c, `UPDATE authorization_state SET revision=revision+1 WHERE id=1`); err != nil {
		fail(c, 500, "授权更新失败")
		return
	}
	raw, _ := json.Marshal(details)
	if _, err = tx.Exec(c, `INSERT INTO audit_events(id,actor_id,action,target_id,details) VALUES($1,$2,$3,$4,$5)`, id(), c.MustGet("user").(user).ID, action, target, string(raw)); err == nil {
		err = tx.Commit(c)
	}
	if err != nil {
		fail(c, 500, "授权更新失败")
		return
	}
	a.flights.Range(func(_, v any) bool { v.(context.CancelFunc)(); return true })
	c.Status(204)
}

func boundedBody(c *gin.Context, q any) bool {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 16<<10)
	if c.ShouldBindJSON(q) != nil {
		fail(c, 400, "配置格式无效")
		return false
	}
	return true
}

func (a *app) createUser(c *gin.Context) {
	var q struct {
		Username string `json:"username"`
		Password string `json:"password"`
		Usage    string `json:"usage_kind"`
	}
	if !boundedBody(c, &q) {
		return
	}
	q.Username = strings.TrimSpace(q.Username)
	if q.Usage == "" {
		q.Usage = "demo"
	}
	if q.Username == "" || len(q.Username) > 128 || len(q.Password) < 12 || len(q.Password) > 72 || !validUsage(q.Usage) {
		fail(c, 400, "账号或密码无效；密码需为12–72字节")
		return
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(q.Password), bcrypt.DefaultCost)
	if err != nil {
		fail(c, 400, "密码无效")
		return
	}
	uid := id()
	a.changeAccess(c, "user.create", uid, map[string]string{"username": q.Username, "usage_kind": q.Usage}, func(tx pgx.Tx) error {
		_, err := tx.Exec(c, `INSERT INTO users(id,username,password_hash,role,usage_kind) VALUES($1,$2,$3,'user',$4)`, uid, q.Username, string(hash), q.Usage)
		return err
	})
}
func validUsage(s string) bool {
	return s == "demo" || s == "self_test" || s == "engineering_test" || s == "real_usage"
}

func (a *app) updateUser(c *gin.Context) {
	var q struct {
		Disabled *bool   `json:"disabled"`
		Usage    *string `json:"usage_kind"`
	}
	if !boundedBody(c, &q) {
		return
	}
	if (q.Disabled == nil && q.Usage == nil) || (q.Usage != nil && !validUsage(*q.Usage)) {
		fail(c, 400, "账号设置无效")
		return
	}
	a.changeAccess(c, "user.update", c.Param("id"), q, func(tx pgx.Tx) error {
		var role string
		if err := tx.QueryRow(c, `SELECT role FROM users WHERE id=$1`, c.Param("id")).Scan(&role); err != nil {
			return err
		}
		// 引导管理员由环境配置维护，避免通过页面锁死管理入口。
		if role == "admin" {
			return errors.New("bootstrap administrator")
		}
		if _, err := tx.Exec(c, `UPDATE users SET disabled=COALESCE($2,disabled),usage_kind=COALESCE($3,usage_kind) WHERE id=$1`, c.Param("id"), q.Disabled, q.Usage); err != nil {
			return err
		}
		if q.Disabled != nil && *q.Disabled {
			_, err := tx.Exec(c, `DELETE FROM tokens WHERE user_id=$1`, c.Param("id"))
			return err
		}
		return nil
	})
}
func (a *app) createGroup(c *gin.Context) {
	var q struct {
		Name string `json:"name"`
	}
	if !boundedBody(c, &q) {
		return
	}
	q.Name = strings.TrimSpace(q.Name)
	if q.Name == "" || len(q.Name) > 128 {
		fail(c, 400, "填写用户组名称")
		return
	}
	gid := id()
	a.changeAccess(c, "group.create", gid, q, func(tx pgx.Tx) error {
		_, err := tx.Exec(c, `INSERT INTO user_groups(id,name) VALUES($1,$2)`, gid, q.Name)
		return err
	})
}
func (a *app) groupMembers(c *gin.Context) {
	var q struct {
		IDs []string `json:"user_ids"`
	}
	if !boundedBody(c, &q) {
		return
	}
	if len(q.IDs) > 100 {
		fail(c, 400, "每组最多100个成员")
		return
	}
	a.changeAccess(c, "group.members", c.Param("id"), q, func(tx pgx.Tx) error {
		var gid string
		if err := tx.QueryRow(c, `SELECT id FROM user_groups WHERE id=$1`, c.Param("id")).Scan(&gid); err != nil {
			return err
		}
		if _, err := tx.Exec(c, `DELETE FROM user_group_members WHERE group_id=$1`, gid); err != nil {
			return err
		}
		for _, uid := range q.IDs {
			if _, err := tx.Exec(c, `INSERT INTO user_group_members(group_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING`, gid, uid); err != nil {
				return err
			}
		}
		return nil
	})
}
func (a *app) deleteGroup(c *gin.Context) {
	a.changeAccess(c, "group.delete", c.Param("id"), nil, func(tx pgx.Tx) error {
		tag, err := tx.Exec(c, `DELETE FROM user_groups WHERE id=$1`, c.Param("id"))
		if err == nil && tag.RowsAffected() == 0 {
			return pgx.ErrNoRows
		}
		return err
	})
}
func (a *app) documentGrants(c *gin.Context) {
	var q struct {
		Visibility string   `json:"visibility"`
		IDs        []string `json:"group_ids"`
	}
	if !boundedBody(c, &q) {
		return
	}
	if (q.Visibility != "all_users" && q.Visibility != "groups") || len(q.IDs) > 100 || (q.Visibility == "all_users" && len(q.IDs) > 0) {
		fail(c, 400, "文档权限无效")
		return
	}
	a.changeAccess(c, "document.access", c.Param("id"), q, func(tx pgx.Tx) error {
		tag, err := tx.Exec(c, `UPDATE documents SET visibility=$2 WHERE id=$1`, c.Param("id"), q.Visibility)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			return pgx.ErrNoRows
		}
		if _, err = tx.Exec(c, `DELETE FROM document_group_grants WHERE document_id=$1`, c.Param("id")); err != nil {
			return err
		}
		for _, gid := range q.IDs {
			if _, err = tx.Exec(c, `INSERT INTO document_group_grants(document_id,group_id) VALUES($1,$2) ON CONFLICT DO NOTHING`, c.Param("id"), gid); err != nil {
				return err
			}
		}
		return nil
	})
}

type provenance struct {
	Documents []string `json:"document_ids"`
	Verified  bool     `json:"verified"`
}

func (a *app) visibleDocuments(ctx context.Context, u user, kbs []string) ([]string, error) {
	if kbs == nil {
		kbs = []string{}
	}
	rows, err := a.db.Query(ctx, `SELECT d.id FROM documents d JOIN knowledge_bases k ON k.id=d.kb_id WHERE (k.id=ANY($1::text[]) OR (cardinality($1::text[])=0 AND k.default_search)) AND `+documentAccess("$2", "$3")+` ORDER BY d.id`, kbs, u.ID, u.Role == "admin")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	docs := []string{}
	for rows.Next() {
		var doc string
		if err = rows.Scan(&doc); err != nil {
			return nil, err
		}
		docs = append(docs, doc)
	}
	return docs, rows.Err()
}

func (a *app) readMessages(c *gin.Context, sid string) ([]map[string]any, error) {
	rows, err := a.db.Query(c, `SELECT to_jsonb(m) FROM messages m WHERE session_id=$1 ORDER BY created_at,id`, sid)
	if err != nil {
		return nil, err
	}
	// 先关闭游标再执行授权查询，避免受连接池容量影响。
	items := []map[string]any{}
	for rows.Next() {
		var raw []byte
		var m map[string]any
		if err = rows.Scan(&raw); err == nil {
			err = json.Unmarshal(raw, &m)
		}
		if err != nil {
			rows.Close()
			return nil, err
		}
		items = append(items, m)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	u := c.MustGet("user").(user)
	for _, m := range items {
		allowed, err := a.messageAllowed(c, u, m)
		if err != nil {
			return nil, err
		}
		if !allowed {
			m["answer"] = "来源权限已变更，回答已隐藏。"
			m["status"] = "restricted"
			m["citations"] = []any{}
			m["trace"] = nil
			m["snapshot_id"] = nil
			m["provenance"] = nil
		}
	}
	return items, nil
}

func (a *app) messageAllowed(ctx context.Context, u user, m map[string]any) (bool, error) {
	raw, _ := json.Marshal(m["provenance"])
	var p provenance
	_ = json.Unmarshal(raw, &p)
	if !p.Verified {
		// 旧回答没有来源依赖时按整个冻结范围检查，无法恢复来源则遮蔽。
		snapshot, _ := m["snapshot_id"].(string)
		versions := []string{}
		if trace, ok := m["trace"].(map[string]any); ok {
			if members, ok := trace["available_snapshots"].([]any); ok {
				for _, value := range members {
					if member, ok := value.(map[string]any); ok {
						if ids, ok := member["version_ids"].([]any); ok {
							for _, value := range ids {
								if vid, ok := value.(string); ok {
									versions = append(versions, vid)
								}
							}
						}
					}
				}
			}
		}
		rows, err := a.db.Query(ctx, `SELECT DISTINCT v.document_id FROM document_versions v WHERE v.id=ANY($2::text[]) OR v.id IN(SELECT j.version_id FROM snapshots s CROSS JOIN LATERAL jsonb_array_elements_text(s.version_ids) j(version_id) WHERE s.id=$1)`, snapshot, versions)
		if err != nil {
			return false, err
		}
		for rows.Next() {
			var doc string
			if err = rows.Scan(&doc); err != nil {
				rows.Close()
				return false, err
			}
			p.Documents = append(p.Documents, doc)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return false, err
		}
		if len(p.Documents) == 0 {
			return false, nil
		}
	}
	if len(p.Documents) == 0 {
		return true, nil
	}
	var count int
	err := a.db.QueryRow(ctx, `SELECT count(*) FROM documents d WHERE d.id=ANY($1::text[]) AND `+documentAccess("$2", "$3"), p.Documents, u.ID, u.Role == "admin").Scan(&count)
	return count == len(p.Documents), err
}

func (a *app) prepareChat(c *gin.Context, kbs []string, mid string) (context.Context, context.CancelFunc, int64, []string, string, error) {
	a.policy.RLock()
	defer a.policy.RUnlock()
	u := c.MustGet("user").(user)
	var revision int64
	var usage string
	if err := a.db.QueryRow(c, `SELECT s.revision,u.usage_kind FROM authorization_state s JOIN users u ON u.id=$1 WHERE s.id=1 AND NOT u.disabled`, u.ID).Scan(&revision, &usage); err != nil {
		return nil, nil, 0, nil, "", err
	}
	if len(kbs) > 0 {
		var count int
		if err := a.db.QueryRow(c, `SELECT count(*) FROM knowledge_bases k WHERE k.id=ANY($1::text[]) AND ($3::boolean OR EXISTS(SELECT 1 FROM documents d WHERE d.kb_id=k.id AND `+documentAccess("$2", "$3")+`))`, kbs, u.ID, u.Role == "admin").Scan(&count); err != nil {
			return nil, nil, 0, nil, "", err
		}
		if count != len(kbs) {
			return nil, nil, 0, nil, "", pgx.ErrNoRows
		}
	}
	docs, err := a.visibleDocuments(c, u, kbs)
	if err != nil {
		return nil, nil, 0, nil, "", err
	}
	ctx, cancel := context.WithTimeout(c.Request.Context(), 120*time.Second)
	a.flights.Store(mid, cancel)
	return ctx, cancel, revision, docs, usage, nil
}
