# web/

Every HTML, CSS and JS string the server sends lives here, so the Python server and the Go server serve the same bytes.
Neither implementation embeds markup in code.
The Python server reads these files once, at start, from the `web/` folder next to `sheepdog.py`.
The Go server embeds them in the binary at build time (`go:embed`, see `assets.go` in the repo root) and fills them once at start, so the binary needs no files next to it.
All files are UTF-8, LF line endings, no BOM.
Files are used byte for byte: a trailing newline counts, so do not let an editor add or remove one.

## Files

| File | Used by | Served as |
| --- | --- | --- |
| `board.html` | `GET /` | the board page, filled with `THEME_JS` and `BASE_CSS` |
| `login.html` | `GET /` without a session (when the client accepts `text/html`), `POST /login` with a wrong token (401) | the sign-in page, filled with `THEME_JS`, then `ERR` per request |
| `arch.html` | `GET /architecture` | the architecture page, filled per request |
| `arch-diagram.html` | the `DIAGRAM` placeholder of `arch.html` | an SVG fragment, inserted verbatim |
| `base.css` | the `BASE_CSS` placeholder of `board.html` and `arch.html` | the board's colour tokens and base type, inserted verbatim |
| `theme.js` | the `THEME_JS` placeholder of every page | the code that applies the stored System/Light/Dark choice before first paint, inserted verbatim |

`/brand/<name>` serves the PNGs in `../brand/` and is not part of this folder.

## Placeholder syntax

A placeholder is `{{NAME}}`, upper case letters and underscore only, nothing inside the braces but the name.
Fill is a plain string replace, in one pass: a value that is inserted is never scanned for placeholders again.
Only the names listed below are replaced.
Any other `{{...}}` text (the board's JavaScript contains `{{` in places) stays exactly as written.
In Go: `strings.NewReplacer("{{THEME_JS}}", v1, ...)` does exactly this.
The fill is the same for every request except where "per request" is noted.

`arch.html` has one extra rule: every `\n` in the file is dropped before filling.
The file is laid out one element per line to be readable, but the page it produces is a single line.
This applies to `arch.html` only, and only to its own text, not to the values inserted afterwards.

## Placeholders

Escaping column: "none" means the value is inserted as is and is trusted (a fixed file or a constant from the code).
"HTML-escaped" means Python's `html.escape(s, quote=True)`: `&` becomes `&amp;`, `<` becomes `&lt;`, `>` becomes `&gt;`, `"` becomes `&quot;` and `'` becomes `&#x27;`.
Go's `html.EscapeString` writes `&#34;` and `&#39;` for the last two, so use a custom escaper there.

| Name | In | Meaning | Escaping |
| --- | --- | --- | --- |
| `THEME_JS` | `board.html`, `login.html`, `arch.html` | the content of `theme.js`; every page wraps it as `<script>{{THEME_JS}}</script>` | none |
| `BASE_CSS` | `board.html`, `arch.html` | the content of `base.css`, which ends with a newline | none |
| `DIAGRAM` | `arch.html` | the content of `arch-diagram.html` | none |
| `ERR` | `login.html` (per request) | the empty string for the sign-in page, `<p class=err>Wrong token</p>` for the 401 after a wrong token | none |
| `NUMBERS` | `arch.html` (per request) | one `<li>TEXT</li>` per line of the Numbers list, in the order below, nothing between them | each TEXT HTML-escaped |
| `ROUTES` | `arch.html` (per request) | one `<tr><td>METHOD</td><td><code>PATH</code></td><td>DOC</td></tr>` per route, in route table order, nothing between rows | PATH and DOC HTML-escaped |
| `TABLES` | `arch.html` (per request) | one `<tr><td><code>NAME</code></td><td>COLUMNS</td></tr>` per table of the open database, ordered by table name, `sqlite_*` tables left out | COLUMNS HTML-escaped; NAME is a plain identifier |
| `EVENTS` | `arch.html` (per request) | one `<tr><td><code>EVENT</code></td><td>DESCRIPTION</td></tr>` per hook event the server handles, in table order | DESCRIPTION HTML-escaped |

Details that decide the bytes:

- `PATH` is the route's pattern with each named group `(?P<key>[^/]+)` written as `<key>`, then escaped, so it reads `/api/task/&lt;key&gt;/events`.
  The route table is the one in `sheepdog.py` (`ROUTES`); the Go server needs the same method, path text and doc text for each route, in the same order.
- `COLUMNS` is the column names of the table in `PRAGMA table_info` order, joined with `, `.
  On a database that was migrated from an older version the added columns come last, so the order depends on the file.
- `EVENT` and `DESCRIPTION` come from `HOOK_EVENTS` in `sheepdog.py`.
- `NUMBERS` has these seven items, in this order (`%.1f` and `%g` are printf formats):
  1. `board page %.1f KB of the 90 KB budget`, with the size of the filled board page in bytes divided by 1000.
  2. `page polls /api/state every %g s`, with the number found in the filled board page by the pattern `setTimeout\(loop,(\d+)\)` divided by 1000 (3000 gives `3`).
  3. `board state: cached per revision, rebuilt after a write or after 2 s`
  4. `hook: curl gives up after 2 s (sheepdog hook: 1 s); the reply is empty unless decisions wait`
  5. `request bodies: 8 MB on /api/hook, 1 MB elsewhere`
  6. `long-poll: at most 55 s per call`
  7. `an agent silent for 2 h shows as stale; events are pruned after 30 days`

`contract/baseline-pages/` holds the exact bytes of the four pages as the Python server sent them before this folder existed: the board, the sign-in page, the sign-in page after a wrong token, and the architecture page of an empty database.
`python3 contract/pages.py OUTDIR [--server ...]` writes the same four files from any server, and `cmp` proves them identical.
