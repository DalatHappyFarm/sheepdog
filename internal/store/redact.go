package store

import (
	"regexp"
	"strings"
)

// The redaction rules of sheepdog.py, in the same order. RE2 has no backtracking, so every pattern is linear.
// \w and \s are written out as Unicode classes to match Python's str patterns. Known difference: \b is ASCII-only here, so a secret
// right next to a non-ASCII letter is still redacted (Python would not redact it): more redaction, never less.
const (
	w     = `\p{L}\p{N}_`              // inside [...]: Python's \w
	sp    = `\s\x0b\x1c-\x1f\x85\p{Z}` // inside [...]: Python's \s (RE2's \s lacks \v and the Unicode spaces)
	names = `(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|pwd)`
	val   = `("[^"]*"|'[^']*'|[^` + sp + `"']+)`
)

var redactions = []struct {
	rx   *regexp.Regexp
	rep  string
	any  []string // the rule can only match when the text holds one of these (compared lower-case when fold is set): a cheap skip, never a change
	fold bool
}{
	{regexp.MustCompile(`(?i)(authorization["']?[` + sp + `]*[:=][` + sp + `]*)(?:(?:bearer|basic|token)[` + sp + `]+)?[^` + sp + `"']+`), "${1}[REDACTED]", []string{"authorization"}, true},
	{regexp.MustCompile(`(?i)\bbearer[` + sp + `]+[` + w + `.~+/=-]{8,}`), "Bearer [REDACTED]", []string{"bearer"}, true},
	{regexp.MustCompile(`(?i)(` + names + `[` + w + `-]{0,40}["']?[` + sp + `]*[=:][` + sp + `]*)` + val), "${1}[REDACTED]", []string{"token", "password", "passwd", "secret", "key", "pwd"}, true},
	{regexp.MustCompile(`(?i)(--[` + w + `-]{0,40}` + names + `[` + w + `-]{0,40}[` + sp + `]+)` + val), "${1}[REDACTED]", []string{"--"}, true},
	{regexp.MustCompile(`\b(?:AKIA|ASIA)[A-Z0-9]{16}\b`), "[REDACTED]", []string{"AKIA", "ASIA"}, false},
	{regexp.MustCompile(`\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|glpat-[` + w + `-]{10,}|xox[baprs]-[` + w + `-]{10,})`), "[REDACTED]", []string{"sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "glpat-", "xox"}, false},
	{regexp.MustCompile(`\beyJ[` + w + `-]{10,}\.[` + w + `-]{10,}\.[` + w + `-]*`), "[REDACTED]", []string{"eyJ"}, false},
	{regexp.MustCompile(`(?i)\b([a-z][a-z0-9+.-]{0,20}://[^` + sp + `:/@]+:)[^` + sp + `@/]+@`), "${1}[REDACTED]@", []string{"://"}, false},
}

func hasAny(s string, subs []string) bool {
	for _, x := range subs {
		if strings.Contains(s, x) {
			return true
		}
	}
	return false
}

// Redact removes secrets from s.
func Redact(s string) string {
	low := ""
	for _, r := range redactions {
		hay := s
		if r.fold {
			if low == "" || len(low) == 0 {
				low = strings.ToLower(s)
			}
			hay = low
		}
		if !hasAny(hay, r.any) {
			continue
		}
		s = r.rx.ReplaceAllString(s, r.rep)
		low = "" // the text may have changed
	}
	return s
}

// Clip redacts on the full text first, then cuts, so a secret is never half-kept.
func Clip(s string, n int) string {
	s = strings.ReplaceAll(Redact(s), "\n", " ")
	if runeLen(s) <= n {
		return s
	}
	return head(s, n-1) + "..."
}
