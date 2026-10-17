package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDefaultDataDirFallsBackToTheOldFolder(t *testing.T) {
	h := t.TempDir()
	t.Setenv("HOME", h)
	n, o := filepath.Join(h, ".local/share/sheepdog/board.db"), filepath.Join(h, ".local/share/agent-board/board.db")
	if got := dataDB(); got != n { // nothing yet: the new folder is made
		t.Errorf("got %s want %s", got, n)
	}
	if st, err := os.Stat(filepath.Dir(n)); err != nil || !st.IsDir() {
		t.Error("the new folder was not made")
	}
	os.MkdirAll(filepath.Dir(o), 0o755)
	os.WriteFile(o, nil, 0o644)
	if got := dataDB(); got != o { // only the old file exists
		t.Errorf("got %s want %s", got, o)
	}
	os.WriteFile(n, nil, 0o644)
	if got := dataDB(); got != n { // the new one wins once it exists
		t.Errorf("got %s want %s", got, n)
	}
}

func TestPurgeRefusesWithoutAnAllowlistOrMode(t *testing.T) {
	t.Setenv("SHEEPDOG_ALLOW", "")
	db := filepath.Join(t.TempDir(), "x.db")
	if got := purge([]string{"--outside-allow", "--db", db}); got != 1 {
		t.Errorf("empty allowlist: exit %d, want 1", got)
	}
	if got := purge([]string{"--allow", "/x", "--db", db}); got != 2 {
		t.Errorf("no --outside-allow: exit %d, want 2", got)
	}
	if got := purge([]string{"--outside-allow", "--allow", "/x", "--db", db}); got != 1 { // the database does not exist: purge never creates one
		t.Errorf("missing database: exit %d, want 1", got)
	}
}
