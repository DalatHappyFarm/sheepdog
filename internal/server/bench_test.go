package server_test

import (
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/DalatHappyFarm/sheepdog/internal/server"
	"github.com/DalatHappyFarm/sheepdog/internal/store"
)

func benchServer(b *testing.B) *server.Server {
	db, err := store.Open(filepath.Join(b.TempDir(), "b.db"))
	if err != nil {
		b.Fatal(err)
	}
	b.Cleanup(db.Close)
	tok := token
	s := server.New(db, &tok)
	for _, k := range []string{"F1", "F2", "F3"} {
		if _, err := db.TaskUpsert(map[string]any{"key": k, "title": "t", "worktree": "/work/" + k}); err != nil {
			b.Fatal(err)
		}
	}
	return s
}

const hookBody = `{"session_id":"bench","cwd":"/work/F3/sub","hook_event_name":"PostToolUse","agent_id":"a-run","tool_name":"Bash","tool_input":{"command":"go test ./... -run TestSomething -count=1"},"duration_ms":812}`

// the whole hook path in the handler: parse, reduce, one write transaction, empty reply
func BenchmarkHookPost(b *testing.B) {
	s := benchServer(b)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		r := httptest.NewRequest("POST", "/api/hook", strings.NewReader(hookBody))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if w.Code != 200 {
			b.Fatal(w.Code)
		}
	}
}

func BenchmarkStateCached(b *testing.B) {
	s := benchServer(b)
	get := func() int {
		r := httptest.NewRequest("GET", "/api/state", nil)
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		return w.Code
	}
	get()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if get() != 200 {
			b.Fatal("state")
		}
	}
}

func BenchmarkStateRebuild(b *testing.B) {
	s := benchServer(b)
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		r := httptest.NewRequest("POST", "/api/task", strings.NewReader(`{"key":"F1","note":"x"}`))
		r.Header.Set("Authorization", "Bearer "+token)
		s.ServeHTTP(httptest.NewRecorder(), r) // a write invalidates the cache
		g := httptest.NewRequest("GET", "/api/state", nil)
		g.Header.Set("Authorization", "Bearer "+token)
		s.ServeHTTP(httptest.NewRecorder(), g)
	}
}
