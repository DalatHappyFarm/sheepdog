package store

import (
	"os"
	"os/user"
	"sort"
	"strings"
	"sync"
	"time"
)

// Scrubber rewrites every string that can carry a path or a name before it is stored, so nothing that identifies the person or the
// machine reaches the database. The rules, in order:
//
//	(a) a path under an allowed root           -> <rootname>:<relative path>   (the root itself: <rootname>:)
//	(b) the home directory (also /Users/<user>, /home/<user>, the real path of $HOME) -> ~ , the rest kept
//	(c) temp dirs (/tmp, /private/tmp, /var/folders/..., $TMPDIR) -> <tmp>/<base name of the rest>
//	(d) the login user name and the host name as standalone tokens -> <user> / <host>
//
// sheepdog.py has the same rules (class Scrubber); contract/rewrite-cases.json holds cases both must answer identically.
type Scrubber struct {
	roots    []rootName // real paths of the allowed roots, longest first
	homes    []string   // longest first
	encRoots []rootName // Claude Code's dash-encoded form of each root ("/work/alpha" is "-work-alpha"), longest first
	encHomes []string   // the same for the homes
	tmps     []string   // longest first
	tokens   []token    // longest first

	mu   sync.Mutex
	seen map[string]seenPath
}

type rootName struct{ path, name string }

type token struct{ text, rep string }

// ScrubConfig is what the rewrite depends on; ScrubberFromEnv fills it from the process.
type ScrubConfig struct {
	Home, User, Host, TmpDir string
}

var skipTokens = map[string]bool{"user": true, "host": true, "tmp": true, "root": true, "localhost": true}

// genericUsers are account names that identify nobody (and are ordinary words in paths): the user-name rule skips them. Their home
// paths are still rewritten.
var genericUsers = map[string]bool{"admin": true, "administrator": true, "root": true, "user": true, "ubuntu": true, "debian": true, "ec2-user": true,
	"centos": true, "fedora": true, "dev": true, "developer": true, "runner": true, "git": true, "www-data": true, "node": true, "app": true,
	"vagrant": true, "pi": true, "guest": true, "test": true}

func nameCh(c byte) bool { // the characters of a path component
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-'
}

func wordCh(c byte) bool { // the letters and digits of a word; _ and - separate, so jdoe-mac and my_jdoe are scrubbed
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9'
}

func pathEnd(c byte) bool { // the characters that end a path in free text
	return strings.IndexByte(" \t\r\n\"'`<>|;&,()[]{}:*?=\\", c) >= 0
}

// NewScrubber builds the rewrite for the given allowed roots (as ParseAllow returns them, real paths).
func NewScrubber(roots []string, c ScrubConfig) *Scrubber {
	s := &Scrubber{seen: map[string]seenPath{}}
	for _, r := range roots {
		r = strings.TrimRight(r, "/")
		if i := strings.LastIndexByte(r, '/'); len(r) > 1 && r[i+1:] != "" {
			s.roots = append(s.roots, rootName{r, r[i+1:]})
		}
	}
	sort.SliceStable(s.roots, func(i, j int) bool { return len(s.roots[i].path) > len(s.roots[j].path) })
	add := func(list *[]string, p string) {
		if p = strings.TrimRight(p, "/"); len(p) > 1 && p[0] == '/' {
			for _, x := range *list {
				if x == p {
					return
				}
			}
			*list = append(*list, p)
		}
	}
	if c.Home != "" {
		add(&s.homes, c.Home)
		add(&s.homes, RealPath(c.Home))
	}
	if c.User != "" && onlyNameCh(c.User) {
		add(&s.homes, "/Users/"+c.User)
		add(&s.homes, "/home/"+c.User)
		if validToken(c.User) && !genericUsers[asciiLower(c.User)] {
			s.tokens = append(s.tokens, token{c.User, "<user>"})
		}
	}
	for _, h := range []string{c.Host, strings.SplitN(c.Host, ".", 2)[0]} {
		if validToken(h) {
			s.tokens = append(s.tokens, token{h, "<host>"})
		}
	}
	add(&s.tmps, "/private/tmp")
	add(&s.tmps, "/tmp")
	if c.TmpDir != "" {
		add(&s.tmps, c.TmpDir)
		add(&s.tmps, RealPath(c.TmpDir))
	}
	byLen := func(l []string) { sort.SliceStable(l, func(i, j int) bool { return len(l[i]) > len(l[j]) }) }
	byLen(s.homes)
	byLen(s.tmps)
	sort.SliceStable(s.tokens, func(i, j int) bool { return len(s.tokens[i].text) > len(s.tokens[j].text) })
	for _, r := range s.roots {
		s.encRoots = append(s.encRoots, rootName{dashEncode(r.path), r.name})
	}
	for _, h := range s.homes {
		s.encHomes = append(s.encHomes, dashEncode(h))
	}
	return s
}

// dashEncode is how Claude Code names a folder in its own paths: "/" and "." become "-".
func dashEncode(p string) string {
	b := []byte(p)
	for i, c := range b {
		if c == '/' || c == '.' {
			b[i] = '-'
		}
	}
	return string(b)
}

func onlyNameCh(t string) bool {
	for i := 0; i < len(t); i++ {
		if !nameCh(t[i]) {
			return false
		}
	}
	return true
}

func validToken(t string) bool { return len(t) >= 3 && !skipTokens[asciiLower(t)] && onlyNameCh(t) }

// ScrubberFromEnv takes the home, user, host and temp dir of this process (SHEEPDOG_HOSTNAME overrides the host name).
func ScrubberFromEnv(roots []string) *Scrubber {
	var c ScrubConfig
	c.Home = os.Getenv("HOME")
	if c.Home == "" {
		c.Home, _ = os.UserHomeDir()
	}
	c.User = os.Getenv("USER")
	if c.User == "" {
		c.User = os.Getenv("LOGNAME")
	}
	if c.User == "" {
		if u, err := user.Current(); err == nil {
			c.User = u.Username
		}
	}
	c.Host = os.Getenv("SHEEPDOG_HOSTNAME")
	if c.Host == "" {
		c.Host, _ = os.Hostname()
	}
	c.TmpDir = os.Getenv("TMPDIR")
	return NewScrubber(roots, c)
}

func asciiLower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 32
		}
	}
	return string(b)
}

// leftOK: a home, temp or root path may only start at a real path start: the beginning, whitespace, a quote, or one of > = ( [ { , ; | & :
// (also a URL's "file:///" and the JSON escapes \n \r \t of stored JSON text). Never after a word character or - _ . ~ /, nor right
// after <user>, <host> or <tmp>, so "src/tmp", "~/tmp" and "org-<user>/tmp" are not paths to rewrite, and rewriting stays idempotent.
func leftOK(s string, i int) bool {
	if i == 0 {
		return true
	}
	p := s[i-1]
	if p == '>' { // after a tag or a redirect, but not right after one of our own placeholders (that is the end of a name, not a path start)
		return !(i >= 5 && s[i-5:i] == "<tmp>" || i >= 6 && (s[i-6:i] == "<user>" || s[i-6:i] == "<host>"))
	}
	if strings.IndexByte(" \t\r\n\"'`=([{,;|&:", p) >= 0 {
		return true
	}
	if p == '/' && i >= 3 && s[i-2] == '/' && s[i-3] == ':' { // file:///tmp/x
		return true
	}
	return (p == 'n' || p == 'r' || p == 't') && i >= 2 && s[i-2] == '\\'
}

func rightOK(s string, end int) bool { return end >= len(s) || s[end] == '/' || !nameCh(s[end]) }

// varFolders matches /var/folders/<a>/<b>[/<one char>] (also behind /private) at i and returns the end of that prefix, or -1.
func varFolders(s string, i int) int {
	rest := s[i:]
	switch {
	case strings.HasPrefix(rest, "/private/var/folders/"):
		i += len("/private/var/folders/")
	case strings.HasPrefix(rest, "/var/folders/"):
		i += len("/var/folders/")
	default:
		return -1
	}
	comp := func() bool {
		j := i
		for j < len(s) && nameCh(s[j]) {
			j++
		}
		ok := j > i
		i = j
		return ok
	}
	if !comp() || i >= len(s) || s[i] != '/' {
		return -1
	}
	i++
	if !comp() {
		return -1
	}
	if i+2 <= len(s) && s[i] == '/' && nameCh(s[i+1]) && (i+2 == len(s) || !nameCh(s[i+2])) && s[i+1] != '.' && s[i+1] != '-' {
		i += 2 // the single-character level (T, C, 0) that holds the temp files
	}
	return i
}

// Text rewrites the paths and names in free text.
func (s *Scrubber) Text(in string) string {
	in = s.paths(in)
	return s.names(in)
}

func (s *Scrubber) paths(in string) string {
	var b strings.Builder
	last, i := 0, 0
	set := "/"
	if len(s.encRoots)+len(s.encHomes) > 0 {
		set = "/-"
	}
	for {
		k := strings.IndexAny(in[i:], set)
		if k < 0 {
			break
		}
		i += k
		if in[i] == '-' {
			if end, rep, ok := s.matchEnc(in, i); ok {
				b.WriteString(in[last:i])
				b.WriteString(rep)
				last, i = end, end
				continue
			}
			i++
			continue
		}
		if !leftOK(in, i) {
			i++
			continue
		}
		if end, rep, skipSlash, ok := s.match(in, i); ok {
			b.WriteString(in[last:i])
			b.WriteString(rep)
			if skipSlash && end < len(in) && in[end] == '/' {
				end++
			}
			last, i = end, end
			if end > len(in) {
				break
			}
			continue
		}
		i++
	}
	if last == 0 {
		return in
	}
	b.WriteString(in[last:])
	return b.String()
}

// match tries the path rules at in[i] (a slash). It returns where the replaced text ends, the replacement, and whether a "/" after the
// end is swallowed (rule a: root/rel becomes root:rel).
func (s *Scrubber) match(in string, i int) (end int, rep string, skipSlash, ok bool) {
	rest := in[i:]
	for _, r := range s.roots {
		if strings.HasPrefix(rest, r.path) && rightOK(in, i+len(r.path)) {
			return i + len(r.path), r.name + ":", true, true
		}
	}
	for _, h := range s.homes {
		if strings.HasPrefix(rest, h) && rightOK(in, i+len(h)) {
			return i + len(h), "~", false, true
		}
	}
	tmpEnd := -1
	if e := varFolders(in, i); e >= 0 {
		tmpEnd = e
	}
	for _, t := range s.tmps {
		if strings.HasPrefix(rest, t) && i+len(t) > tmpEnd {
			tmpEnd = i + len(t)
		}
	}
	if tmpEnd >= 0 && rightOK(in, tmpEnd) {
		if tmpEnd < len(in) && in[tmpEnd] == '/' { // keep only the base name of what follows
			j := tmpEnd + 1
			for j < len(in) && !pathEnd(in[j]) {
				j++
			}
			tail := in[tmpEnd+1 : j]
			base := tail[strings.LastIndexByte(tail, '/')+1:]
			if len(s.encRoots)+len(s.encHomes) > 0 {
				base = s.paths(base) // a clipped dash-encoded folder name can be what is left of the path
			}
			return j, "<tmp>/" + base, false, true
		}
		return tmpEnd, "<tmp>", false, true
	}
	return 0, "", false, false
}

// matchEnc tries the dash-encoded rules at in[i] (a "-"): an encoded root becomes <rootname>:, an encoded home ~, the rest kept. It
// starts at a path start or right after a "/" and ends at "-", "/" or any other non-name character (so -Users-jdoe2 is not -Users-jdoe).
func (s *Scrubber) matchEnc(in string, i int) (end int, rep string, ok bool) {
	if !(leftOK(in, i) || in[i-1] == '/') {
		return
	}
	rest := in[i:]
	for _, r := range s.encRoots {
		if strings.HasPrefix(rest, r.path) && encRight(in, i+len(r.path)) {
			return i + len(r.path), r.name + ":", true
		}
	}
	for _, h := range s.encHomes {
		if strings.HasPrefix(rest, h) && encRight(in, i+len(h)) {
			return i + len(h), "~", true
		}
	}
	return
}

func encRight(s string, end int) bool { return end >= len(s) || s[end] == '-' || !nameCh(s[end]) }

func (s *Scrubber) names(in string) string {
	for _, t := range s.tokens {
		in = replaceToken(in, t.text, t.rep)
	}
	return in
}

// replaceToken replaces whole-word, case-insensitive (ASCII) occurrences of tok.
func replaceToken(in, tok, rep string) string {
	if len(in) < len(tok) {
		return in
	}
	low, t := asciiLower(in), asciiLower(tok)
	var b strings.Builder
	last, i := 0, 0
	for {
		j := strings.Index(low[i:], t)
		if j < 0 {
			break
		}
		j += i
		e := j + len(t)
		if (j == 0 || !wordCh(in[j-1])) && (e >= len(in) || !wordCh(in[e])) {
			b.WriteString(in[last:j])
			b.WriteString(rep)
			last, i = e, e
		} else {
			i = j + 1
		}
	}
	if last == 0 {
		return in
	}
	b.WriteString(in[last:])
	return b.String()
}

func (s *Scrubber) real(p string) string {
	now := time.Now()
	s.mu.Lock()
	if x, ok := s.seen[p]; ok && now.Before(x.exp) {
		s.mu.Unlock()
		return x.real
	}
	s.mu.Unlock()
	r := RealPath(p)
	s.mu.Lock()
	if len(s.seen) > 512 {
		s.seen = map[string]seenPath{}
	}
	s.seen[p] = seenPath{r, now.Add(30 * time.Second)}
	s.mu.Unlock()
	return r
}

// Cwd rewrites a folder: it resolves symlinks first, so a brief's folder and a hook's folder end up in the same form and delivery
// still matches them by prefix.
func (s *Scrubber) Cwd(raw string) string {
	if !strings.HasPrefix(raw, "/") {
		return s.Text(raw)
	}
	real := s.real(raw)
	for _, r := range s.roots {
		if under(real, r.path) {
			return s.names(r.name + ":" + strings.TrimLeft(real[len(r.path):], "/"))
		}
	}
	return s.Text(real)
}

var identKeys = map[string]bool{"hook_event_name": true, "tool_name": true, "session_id": true, "tool_use_id": true, "agent_id": true, "notification_type": true}

// Payload rewrites every string of a hook payload (not the identifiers), and the top-level cwd with Cwd. It returns a copy.
func (s *Scrubber) Payload(p map[string]any) map[string]any {
	out := make(map[string]any, len(p))
	for k, v := range p {
		switch {
		case identKeys[k]:
			out[k] = v
		case k == "cwd":
			if c, ok := v.(string); ok {
				out[k] = head(s.Cwd(c), 500)
			} else {
				out[k] = s.value(v)
			}
		default:
			out[k] = s.value(v)
		}
	}
	return out
}

func (s *Scrubber) value(v any) any {
	switch x := v.(type) {
	case string:
		return s.Text(x)
	case []any:
		o := make([]any, len(x))
		for i, e := range x {
			o[i] = s.value(e)
		}
		return o
	case map[string]any:
		o := make(map[string]any, len(x))
		for k, e := range x {
			o[k] = s.value(e)
		}
		return o
	}
	return v
}
