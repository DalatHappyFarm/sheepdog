package store

// Helpers that reproduce the small Python semantics the server relies on: str(), truthiness, code point slicing,
// splitlines, int(), and json.dumps with ensure_ascii.

import (
	"encoding/json"
	"math"
	"strconv"
	"strings"
	"unicode/utf8"
)

// BadRequest is an error the HTTP layer answers with 400 (Python's ValueError and KeyError).
type BadRequest string

func (e BadRequest) Error() string { return string(e) }

func bad(msg string) error { return BadRequest(msg) }

func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case json.Number:
		f, err := strconv.ParseFloat(string(x), 64)
		return err != nil || f != 0
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

// pyStr is Python's str(v) for a decoded JSON value.
func pyStr(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case string:
		return x
	case json.Number:
		return pyNum(x)
	}
	return pyRepr(v)
}

func pyNum(n json.Number) string {
	s := string(n)
	if !strings.ContainsAny(s, ".eE") {
		return s
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return s
	}
	return pyFloat(f)
}

func pyFloat(f float64) string {
	if math.IsInf(f, 1) {
		return "inf"
	}
	if math.IsInf(f, -1) {
		return "-inf"
	}
	if math.IsNaN(f) {
		return "nan"
	}
	a := math.Abs(f)
	if a != 0 && (a >= 1e16 || a < 1e-4) {
		s := strconv.FormatFloat(f, 'e', -1, 64)
		if i := strings.IndexByte(s, 'e'); i >= 0 && len(s)-i == 3 { // e+5 -> e+05
			s = s[:i+2] + "0" + s[i+2:]
		}
		return s
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

func pyRepr(v any) string {
	switch x := v.(type) {
	case string:
		q := "'"
		if strings.Contains(x, "'") && !strings.Contains(x, `"`) {
			q = `"`
		}
		var b strings.Builder
		b.WriteString(q)
		for _, r := range x {
			switch {
			case r == '\\':
				b.WriteString(`\\`)
			case r == '\n':
				b.WriteString(`\n`)
			case r == '\r':
				b.WriteString(`\r`)
			case r == '\t':
				b.WriteString(`\t`)
			case string(r) == q:
				b.WriteString(`\` + q)
			case r < 0x20 || r == 0x7f:
				b.WriteString(`\x` + strconv.FormatInt(int64(r)+0x100, 16)[1:])
			default:
				b.WriteRune(r)
			}
		}
		b.WriteString(q)
		return b.String()
	case []any:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = pyRepr(e)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sortStrings(keys)
		parts := make([]string, len(keys))
		for i, k := range keys {
			parts[i] = pyRepr(k) + ": " + pyRepr(x[k])
		}
		return "{" + strings.Join(parts, ", ") + "}"
	}
	return pyStr(v)
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

// head is s[:n] by code points.
func head(s string, n int) string {
	if len(s) <= n {
		return s
	}
	i := 0
	for ; n > 0 && i < len(s); n-- {
		_, w := utf8.DecodeRuneInString(s[i:])
		i += w
	}
	return s[:i]
}

// tail is s[-n:] by code points.
func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	i := len(s)
	for ; n > 0 && i > 0; n-- {
		_, w := utf8.DecodeLastRuneInString(s[:i])
		i -= w
	}
	return s[i:]
}

// runeLen is len(s) for a Python str.
func runeLen(s string) int { return utf8.RuneCountInString(s) }

// pySplitLines is str.splitlines() without line ends kept.
func pySplitLines(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); {
		r, w := utf8.DecodeRuneInString(s[i:])
		brk := false
		switch r {
		case '\n', '\v', '\f', '\x1c', '\x1d', '\x1e', '\u0085', '\u2028', '\u2029':
			brk = true
		case '\r':
			brk = true
			if i+1 < len(s) && s[i+1] == '\n' {
				w = 2
			}
		}
		if brk {
			out = append(out, s[start:i])
			start = i + w
		}
		i += w
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}

// pyInt is the server's _int(x): int(x) or nil when it cannot be one.
func pyInt(v any) (int64, bool) {
	switch x := v.(type) {
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	case json.Number:
		if n, err := x.Int64(); err == nil {
			return n, true
		}
		f, err := strconv.ParseFloat(string(x), 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || math.Abs(f) > 9e18 {
			return 0, false
		}
		return int64(f), true
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(strings.ReplaceAll(x, "_", "")), 10, 64)
		return n, err == nil
	}
	return 0, false
}

// jstr appends s as a JSON string the way Python's json.dumps writes it (ensure_ascii).
func jstr(b []byte, s string) []byte {
	const hex = "0123456789abcdef"
	b = append(b, '"')
	for _, r := range s {
		switch {
		case r == '"':
			b = append(b, '\\', '"')
		case r == '\\':
			b = append(b, '\\', '\\')
		case r == '\n':
			b = append(b, '\\', 'n')
		case r == '\r':
			b = append(b, '\\', 'r')
		case r == '\t':
			b = append(b, '\\', 't')
		case r == '\b':
			b = append(b, '\\', 'b')
		case r == '\f':
			b = append(b, '\\', 'f')
		case r >= ' ' && r <= '~':
			b = append(b, byte(r))
		case r > 0xffff:
			r -= 0x10000
			hi, lo := 0xd800+(r>>10), 0xdc00+(r&0x3ff)
			b = append(b, '\\', 'u', hex[hi>>12&15], hex[hi>>8&15], hex[hi>>4&15], hex[hi&15], '\\', 'u', hex[lo>>12&15], hex[lo>>8&15], hex[lo>>4&15], hex[lo&15])
		default:
			b = append(b, '\\', 'u', hex[r>>12&15], hex[r>>8&15], hex[r>>4&15], hex[r&15])
		}
	}
	return append(b, '"')
}

// KV is one key of an ordered JSON object (Python dicts keep insertion order, the stored text should too).
type KV struct {
	K string
	V any
}

// jsonCompact is json.dumps(obj, separators=(",", ":")) for the value kinds the server stores.
func jsonCompact(v any) []byte { return jenc(nil, v, ",", ":") }

// jsonPy is json.dumps(obj) (separators ", " and ": ").
func jsonPy(v any) []byte { return jenc(nil, v, ", ", ": ") }

func jenc(b []byte, v any, item, colon string) []byte {
	switch x := v.(type) {
	case nil:
		return append(b, "null"...)
	case bool:
		return strconv.AppendBool(b, x)
	case int:
		return strconv.AppendInt(b, int64(x), 10)
	case int64:
		return strconv.AppendInt(b, x, 10)
	case string:
		return jstr(b, x)
	case json.Number:
		return append(b, x...)
	case []string:
		b = append(b, '[')
		for i, s := range x {
			if i > 0 {
				b = append(b, item...)
			}
			b = jstr(b, s)
		}
		return append(b, ']')
	case []any:
		b = append(b, '[')
		for i, e := range x {
			if i > 0 {
				b = append(b, item...)
			}
			b = jenc(b, e, item, colon)
		}
		return append(b, ']')
	case []KV:
		b = append(b, '{')
		for i, e := range x {
			if i > 0 {
				b = append(b, item...)
			}
			b = jstr(b, e.K)
			b = append(b, colon...)
			b = jenc(b, e.V, item, colon)
		}
		return append(b, '}')
	}
	j, _ := json.Marshal(v)
	return append(b, j...)
}

// Quote is urllib.parse.quote(s, safe=""): only letters, digits and _.-~ stay.
func Quote(s string) string {
	const hex = "0123456789ABCDEF"
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-' || c == '~' {
			b.WriteByte(c)
		} else {
			b.WriteByte('%')
			b.WriteByte(hex[c>>4])
			b.WriteByte(hex[c&15])
		}
	}
	return b.String()
}
