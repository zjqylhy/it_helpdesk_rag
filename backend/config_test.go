package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestRootConfigWithoutBackendEnv(t *testing.T) {
	root := t.TempDir()
	backend := filepath.Join(root, "backend")
	if err := os.Mkdir(backend, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".env"), []byte("HELPDESK_ROOT_TEST=from-root\nHELPDESK_PRESET_TEST=from-file\n"), 0600); err != nil {
		t.Fatal(err)
	}
	// 模拟新终端只有根配置，容器注入的同名变量仍保持优先。
	t.Setenv("HELPDESK_ROOT_TEST", "cleanup-placeholder")
	if err := os.Unsetenv("HELPDESK_ROOT_TEST"); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HELPDESK_PRESET_TEST", "from-process")
	t.Chdir(backend)
	loadLocalEnvironment()
	if os.Getenv("HELPDESK_ROOT_TEST") != "from-root" {
		t.Fatal("missing backend/.env prevented loading the root config")
	}
	if os.Getenv("HELPDESK_PRESET_TEST") != "from-process" {
		t.Fatal("root config overwrote process environment")
	}
}
