package store

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Allowlist is the set of folder roots hooks are accepted from. Empty means every folder.
type Allowlist struct {
	roots []string // real absolute paths
	mu    sync.Mutex
	seen  map[string]seenPath // cwd -> its resolved path, so a busy hook stream does not stat the folder every time
}

type seenPath struct {
	real string
	exp  time.Time
}

// RealPath resolves symlinks in the part of p that exists and normalises the rest lexically (like Python's os.path.realpath).
func RealPath(p string) string {
	cur := filepath.Clean(p)
	rest := ""
	for {
		if r, err := filepath.EvalSymlinks(cur); err == nil {
			if rest == "" {
				return r
			}
			return filepath.Join(r, rest)
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return filepath.Clean(p)
		}
		rest = filepath.Join(filepath.Base(cur), rest)
		cur = parent
	}
}

// ExpandHome expands a leading ~ or ~/.
func ExpandHome(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		if h, err := os.UserHomeDir(); err == nil {
			return filepath.Join(h, p[1:])
		}
	}
	return p
}

// ParseAllow builds the allowlist from SHEEPDOG_ALLOW-style text (separated by the OS path list separator) plus extra roots.
func ParseAllow(value string, extra []string) *Allowlist {
	a := &Allowlist{seen: map[string]seenPath{}}
	parts := append(filepath.SplitList(value), extra...)
	for _, x := range parts {
		if x = strings.TrimSpace(x); x != "" {
			a.roots = append(a.roots, RealPath(ExpandHome(x)))
		}
	}
	return a
}

// Roots returns the real root paths.
func (a *Allowlist) Roots() []string {
	if a == nil {
		return nil
	}
	return a.roots
}

// Active says whether any root is set.
func (a *Allowlist) Active() bool { return a != nil && len(a.roots) > 0 }

// Allowed: no allowlist, no cwd at all, or the real cwd is a root or inside one (never compared by base name).
func (a *Allowlist) Allowed(cwd string) bool {
	if !a.Active() || cwd == "" {
		return true
	}
	if !strings.HasPrefix(cwd, "/") {
		return false
	}
	real := a.resolve(cwd)
	for _, r := range a.roots {
		if under(real, r) {
			return true
		}
	}
	return false
}

func (a *Allowlist) resolve(cwd string) string {
	now := time.Now()
	a.mu.Lock()
	if s, ok := a.seen[cwd]; ok && now.Before(s.exp) {
		a.mu.Unlock()
		return s.real
	}
	a.mu.Unlock()
	r := RealPath(cwd)
	a.mu.Lock()
	if len(a.seen) > 512 {
		a.seen = map[string]seenPath{}
	}
	a.seen[cwd] = seenPath{r, now.Add(30 * time.Second)}
	a.mu.Unlock()
	return r
}

// AllowedStored is Allowed for a folder as the database holds it: either a raw absolute path (rows from before paths were rewritten)
// or "<rootname>:<relative path>" as the Scrubber writes folders under an allowed root. "~/...", "<tmp>/..." and other forms are outside.
func (a *Allowlist) AllowedStored(cwd string) bool {
	if !a.Active() || cwd == "" {
		return true
	}
	if strings.HasPrefix(cwd, "/") {
		return a.Allowed(cwd)
	}
	for _, r := range a.roots {
		name := r[strings.LastIndexByte(r, '/')+1:] + ":"
		if len(r) > 1 && len(name) > 1 && strings.HasPrefix(cwd, name) {
			return true
		}
	}
	return false
}
