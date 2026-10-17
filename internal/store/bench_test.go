package store

import (
	"path/filepath"
	"testing"
)

func benchDB(b *testing.B, tasks int) *DB {
	db, err := Open(filepath.Join(b.TempDir(), "p.db"))
	if err != nil {
		b.Fatal(err)
	}
	b.Cleanup(db.Close)
	for i := 0; i < tasks; i++ {
		db.TaskUpsert(map[string]any{"key": "F" + string(rune('A'+i%26)) + string(rune('a'+i/26)), "title": "t"})
	}
	return db
}

func BenchmarkUpsert(b *testing.B) {
	db := benchDB(b, 1)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		db.TaskUpsert(map[string]any{"key": "FAa", "title": "y"})
	}
}

func BenchmarkGetTaskOutsideTx(b *testing.B) {
	db := benchDB(b, 1)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		db.GetTask("FAa")
	}
}

func BenchmarkStateUncached50Tasks(b *testing.B) {
	db := benchDB(b, 50)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		db.cache = map[int64]cached{}
		if _, err := db.State(0); err != nil {
			b.Fatal(err)
		}
	}
}
