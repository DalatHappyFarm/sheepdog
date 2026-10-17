# Contributing

Thanks for helping.

- Open an issue first for anything bigger than a small fix.
- These must pass: `go test ./...`, `go vet ./...`, `gofmt -l .` (no output), `python3 -m unittest`, and `go build -o bin/sheepdog ./cmd/sheepdog && python3 contract/run.py --server "bin/sheepdog serve" --cli bin/sheepdog`.
- The Go server and the Python original must behave the same: change both, and re-record `contract/golden` with the Python server (`python3 contract/run.py --record`) only when the behaviour change is intended.
- Keep changes small and add a test for new behaviour.
- Run `scripts/privacy-scan.sh` before you push. It checks the files you are about to publish for home directories, emails, secrets and private addresses, plus any patterns in your own git-ignored `.privacy-patterns.local`.
- Fake secrets in tests must not look like real provider tokens in the file bytes (GitHub push protection stops on them): build them by concatenation (`"xox" + "b-..."`) in code, and write JSON fixtures through `python3 scripts/defang_json.py FILE.json`.
- Never commit secrets or personal data: `.token`, `*.db` and `.privacy-patterns.local` are git-ignored. Use neutral names in tests and docs (`/work/alpha`, user `jdoe`).
- By contributing you agree your work is released under the MIT license.
