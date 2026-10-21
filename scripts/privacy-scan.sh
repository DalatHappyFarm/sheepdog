#!/bin/sh
# Scan the files git would publish (tracked, plus untracked and not ignored) for anything that identifies a person, a machine or a client.
#
#   scripts/privacy-scan.sh
#
# Exit 0: clean. Exit 1: every hit is printed as file:line:match. Exit 2: not run inside a git repository.
# Generic checks: home directories (/Users/<name>, /home/<name>), macOS temp folders, claude temp dirs, email addresses, bearer tokens,
# long hex or base64 strings, private IP addresses. The fake names used in tests (jdoe, example.com) are allowed.
# Plus one extended regex per line of .privacy-patterns.local (git-ignored: put names you must never publish there, never in a tracked
# file). Lines starting with # and blank lines are skipped; those patterns match case-insensitively.
# Uses rg when it is installed, grep -E otherwise.

cd "$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "privacy-scan: not in a git repository" >&2; exit 2; }

files=$(mktemp) || exit 2
trap 'rm -f "$files"' EXIT HUP INT TERM
{ git ls-files; git ls-files --others --exclude-standard; } | sort -u |
	grep -v -E '\.(png|jpe?g|gif|ico|woff2?)$|(^|/)go\.sum$|(^|/)bin/' | while IFS= read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done > "$files"

if command -v rg >/dev/null 2>&1; then
	find_hits() { tr '\n' '\0' < "$files" | xargs -0 rg --no-heading --with-filename --line-number --only-matching --no-messages $2 -e "$1" -- 2>/dev/null; }
else
	find_hits() { tr '\n' '\0' < "$files" | xargs -0 grep -nHoIE $2 -e "$1" 2>/dev/null; }
fi

status=0
# scan LABEL REGEX ALLOW-REGEX [FLAGS] [SKIP-FILES]: print the hits of REGEX whose matched text does not match ALLOW-REGEX and whose
# file does not match SKIP-FILES (the recorded contract answers and the redaction fixtures are made of fake secrets and digests)
scan() {
	label=$1 re=$2 allow=$3 flags=${4:-} skip=${5:-}
	find_hits "$re" "$flags" | while IFS= read -r hit; do
		m=${hit#*:*:}
		if [ -n "$allow" ] && printf '%s' "$m" | grep -q -E "$allow"; then continue; fi
		if [ -n "$skip" ] && printf '%s' "${hit%%:*}" | grep -q -E "$skip"; then continue; fi
		printf '%s [%s]\n' "$hit" "$label"
	done
}
fixtures='^(contract/golden/|contract/baseline-pages/|internal/store/testdata/)'


out=$(
	scan "home directory" '/Users/[A-Za-z0-9._-]+' '^/Users/(jdoe[0-9]*|<[a-z]+>|Shared)$'
	scan "home directory" '/home/[A-Za-z0-9._-]+' '^/home/(jdoe|ubuntu|runner|<[a-z]+>)$'
	scan "macOS temp folder" '/var/folders/[0-9a-z_]{2}/[0-9a-zA-Z_]{20,}' ''
	scan "claude temp dir" 'claude-[0-9]{3,}' ''
	scan "email address" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' '@(example\.(com|org|net)|users\.noreply\.github\.com|db\.host)$' '' '^internal/store/testdata/'
	scan "bearer token" '[Bb]earer [A-Za-z0-9._~+/=-]{20,}' '' '' '^internal/store/testdata/'
	scan "hex secret" '(^|[^0-9A-Za-z])[0-9a-f]{40,}([^0-9A-Za-z]|$)' '' '' "$fixtures"
	# the README diagrams carry their icon as a base64 data URI (an image in an <img> cannot load another file)
	scan "base64 secret" '(^|[^0-9A-Za-z+/])[A-Za-z0-9+/]{48,}={0,2}([^0-9A-Za-z+/=]|$)' 'abcdefghijklmnopqrstuvwxyz' '' "$fixtures|^design/prototype.html$|^docs/diagrams/[^/]+[.]svg$"
	# provider-shaped secrets: not even fake ones may sit in a published file as a literal (GitHub push protection and every scanner
	# stops on them). Build such test values by concatenation, or store them with a JSON unicode escape in the first character.
	scan "slack token" '(^|[^A-Za-z0-9])(xox[a-z]-[0-9A-Za-z-]{6,}|xapp-[0-9]-[0-9A-Za-z-]{6,})' ''
	scan "github token" '(^|[^A-Za-z0-9])(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})' ''
	scan "gitlab token" '(^|[^A-Za-z0-9])(glpat|glptt|gldt)-[A-Za-z0-9_-]{10,}' ''
	scan "aws key id" '(^|[^A-Za-z0-9])(AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}' ''
	scan "aws secret key" '[Aa][Ww][Ss]_?[Ss][Ee][Cc][Rr][Ee][Tt]_?([Aa][Cc][Cc][Ee][Ss][Ss]_?)?[Kk][Ee][Yy]["'"'"' :=]{0,5}[A-Za-z0-9/+=]{30,}' ''
	scan "google api key" '(^|[^A-Za-z0-9])AIza[0-9A-Za-z_-]{20,}' ''
	scan "stripe key" '(^|[^A-Za-z0-9])[spr]k_(live|test)_[0-9A-Za-z]{10,}' ''
	scan "openai or anthropic key" '(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}' ''
	scan "private key" '-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----' ''
	scan "jwt" '(^|[^A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*' ''
	scan "twilio key" '(^|[^A-Za-z0-9])(AC|SK)[0-9a-fA-F]{32}([^A-Za-z0-9]|$)' ''
	scan "sendgrid key" '(^|[^A-Za-z0-9])SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}' ''
	scan "npm token" '(^|[^A-Za-z0-9])npm_[A-Za-z0-9]{20,}' ''
	scan "other provider token" '(^|[^A-Za-z0-9])(hf_[A-Za-z0-9]{30,}|dckr_pat_[A-Za-z0-9_-]{20,}|dop_v1_[a-f0-9]{40,}|shpat_[a-f0-9]{20,}|lin_api_[A-Za-z0-9]{20,}|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}|key-[0-9a-f]{32}|[0-9]{8,10}:AA[A-Za-z0-9_-]{33})' ''
	scan "private IP address" '(^|[^0-9.])(10\.[0-9]{1,3}|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}([^0-9.]|$)' ''
	if [ -f .privacy-patterns.local ]; then
		while IFS= read -r p || [ -n "$p" ]; do
			[ -z "$p" ] && continue
			[ "${p#\#}" != "$p" ] && continue
			scan "local pattern" "$p" '' "-i"
		done < .privacy-patterns.local
	fi
)
if [ -n "$out" ]; then
	printf '%s\n' "$out"
	echo "privacy-scan: found the above" >&2
	exit 1
fi
echo "privacy-scan: clean ($(wc -l < "$files" | tr -d ' ') files)"
exit $status
