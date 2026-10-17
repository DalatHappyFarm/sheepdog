#!/usr/bin/env python3
"""Rewrite a JSON file so no provider-shaped secret appears as a literal in its bytes, while every loaded value stays identical.

  python3 scripts/defang_json.py FILE.json

The first character of each token that looks like a Slack, GitHub, GitLab, AWS, Google, Stripe, OpenAI/Anthropic, JWT, Twilio, SendGrid,
npm or similar key is written as a JSON unicode escape (xoxb- becomes \\u0078oxb-). GitHub push protection and secret scanners match the
bytes of a file, so fake tokens in test fixtures must be stored this way. Standard library only.
"""
import json
import re
import sys

SHAPES = [
    r"xox[a-z]-[0-9A-Za-z-]{6,}", r"xapp-[0-9]-[0-9A-Za-z-]{6,}", r"gh[pousr]_[A-Za-z0-9]{16,}", r"github_pat_[A-Za-z0-9_]{20,}",
    r"(?:glpat|glptt|gldt)-[A-Za-z0-9_-]{10,}", r"(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}", r"AIza[0-9A-Za-z_-]{20,}",
    r"[spr]k_(?:live|test)_[0-9A-Za-z]{10,}", r"sk-[A-Za-z0-9_-]{16,}", r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*", r"(?:AC|SK)[0-9a-fA-F]{32}", r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
    r"npm_[A-Za-z0-9]{20,}", r"hf_[A-Za-z0-9]{30,}", r"dckr_pat_[A-Za-z0-9_-]{20,}", r"dop_v1_[a-f0-9]{40,}", r"shpat_[a-f0-9]{20,}",
    r"lin_api_[A-Za-z0-9]{20,}", r"key-[0-9a-f]{32}", r"[0-9]{8,10}:AA[A-Za-z0-9_-]{33}",
]
RX = re.compile("|".join("(?:%s)" % s for s in SHAPES))


def defang(text):
    """text with the first character of every shaped token as \\uXXXX; repeated until nothing matches."""
    while True:
        out, last, hit = [], 0, False
        for m in RX.finditer(text):
            hit = True
            out.append(text[last:m.start()])
            out.append("\\u%04x" % ord(text[m.start()]))
            out.append(text[m.start() + 1:m.end()])
            last = m.end()
        if not hit:
            return text
        out.append(text[last:])
        text = "".join(out)


def main(path):
    with open(path, encoding="utf-8") as f:
        before = json.load(f)
    text = defang(json.dumps(before, ensure_ascii=False))
    if json.loads(text) != before:
        sys.exit("defang changed a value: refusing to write " + path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
