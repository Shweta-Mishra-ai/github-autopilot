"""
repair_autopilot.py — Apply to a copy of the primary subject the two rule
changes the application now ships, and nothing else.

  1. Boundary repair (widening). The five rules whose trailing \\b follows a
     class containing a hyphen end in (?![A-Za-z0-9_]) instead: a credential
     ending in `-` is reported under its own rule. Every string the original
     accepts is still accepted (Section RQ2 of the paper).
  2. Precision repair (narrowing). The PagerDuty rule, \\b[a-z0-9]{32}\\b,
     matched any 32-character lowercase word; it is keyword-anchored like the
     Datadog rule beside it. This one rejects strings the original accepted,
     by design, and is validated for what it removes as well as what it adds.

The patterns are copied verbatim from app/security/enhanced_secrets.py as
the application ships them, so the measured table is the shipped one minus
every unrelated change since commit 38b2013.

Run: python repair_autopilot.py <subject-tree> <out-tree> <out.json>
"""
from __future__ import annotations

import json
import shutil
import sys

END = r"(?![A-Za-z0-9_])"

# (rule, original pattern literal at 38b2013, repaired pattern literal, kind)
CHANGES = [
    ("OpenAI API Key (new)", r'r"\bsk-proj-[a-zA-Z0-9_-]{50,}\b"',
     r'r"\bsk-proj-[a-zA-Z0-9_-]{50,}' + END + '"', "boundary"),
    ("GCP API Key", r'r"\bAIza[0-9A-Za-z_\-]{35}\b"',
     r'r"\bAIza[0-9A-Za-z_\-]{35}' + END + '"', "boundary"),
    ("Google OAuth Token", r'r"\bya29\.[0-9A-Za-z_\-]{68,}\b"',
     r'r"\bya29\.[0-9A-Za-z_\-]{68,}' + END + '"', "boundary"),
    ("SendGrid API Key", r'r"\bSG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}\b"',
     r'r"\bSG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}' + END + '"', "boundary"),
    ("JWT Token", r'r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"',
     r'r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}' + END + '"',
     "boundary"),
    ("PagerDuty Integration Key", r'r"\b[a-z0-9]{32}\b"',
     r'''r"(?i)pagerduty.{0,20}['\"][a-z0-9]{32}['\"]"''', "precision"),
]

RULES_FILE = "app/security/enhanced_secrets.py"


def main(subject, out_tree, out_json):
    shutil.rmtree(out_tree, ignore_errors=True)
    shutil.copytree(subject, out_tree, ignore=shutil.ignore_patterns(".git"))
    path = f"{out_tree}/{RULES_FILE}"
    src = open(path).read()
    for rule, old, new, _kind in CHANGES:
        if src.count(old) != 1:
            raise SystemExit(f"{rule}: expected the 38b2013 pattern exactly once")
        src = src.replace(old, new)
    open(path, "w").write(src)
    json.dump({"rules_file": RULES_FILE,
               "changes": [{"rule": r, "kind": k, "before": o[2:-1], "after": n[2:-1]}
                           for r, o, n, k in CHANGES]},
              open(out_json, "w"), indent=2)


if __name__ == "__main__":
    main(*sys.argv[1:4])
