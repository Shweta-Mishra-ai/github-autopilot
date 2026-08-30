r"""
bench_fixvalidation.py — Does the proposed remedy actually work?

The paper diagnoses five boundary-fragile rules and proposes replacing the
trailing \b with a lookahead. Proposing a fix is not evidence that it works,
so this harness applies candidate fixes to the rule table IN MEMORY (the
subject repository is never modified) and re-runs the full battery.

Two candidates are evaluated, because they differ on one case that matters:

  A  (?=[^\w-]|$)     the remedy as first proposed. Requires the following
                      character to be neither a word character NOR a hyphen.
  B  (?![A-Za-z0-9_]) requires only that the following character is not a word
                      character, so a hyphen may legitimately terminate a match.

Both fix the reported defect. They diverge when a credential whose own final
character is a word character is immediately followed by a hyphen -- which the
ORIGINAL \b accepted. If A rejects that case, A is a regression, and the paper
would be proposing a fix that trades one boundary defect for another.

Measured per arm: the four-way outcome distribution over all 12 embeddings,
boundary robustness BR, and a false-positive regression check over the same
real-code corpus used in RQ5.
"""
from __future__ import annotations

import json
import random
import logging
import os
import re
import sys

SUBJECT_REPO_PATH = os.environ.get(
    "SUBJECT_REPO", os.path.expanduser("~/github-autopilot"))
sys.path.insert(0, SUBJECT_REPO_PATH)

import exrex  # noqa: E402

import app.security.enhanced_secrets as scanner  # noqa: E402
from app.security.enhanced_secrets import scan_diff  # noqa: E402

logging.disable(logging.CRITICAL)

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
N = 120
SCANNED_FILE = "app/settings.py"
FALLBACK = "High Entropy String (unclassified)"
SEED = 20260823

AFFECTED = ["OpenAI API Key (new)", "GCP API Key", "Google OAuth Token",
            "SendGrid API Key", "JWT Token"]

CONTEXTS = {
    "canonical":      lambda v: f'+cfg_value = "{v}"',
    "single_quoted":  lambda v: f"+cfg_value = '{v}'",
    "unquoted":       lambda v: f"+cfg_value = {v}",
    "yaml_value":     lambda v: f"+  cfg_value: {v}",
    "json_value":     lambda v: f'+  "cfg_value": "{v}",',
    "env_file":       lambda v: f"+CFG_VALUE={v}",
    "url_query":      lambda v: f'+endpoint = "https://api.example.com/v1?p={v}"',
    "trailing_comma": lambda v: f'+values = ["{v}", "next"]',
    "in_parens":      lambda v: f"+client = Client({v})",
    "leading_space":  lambda v: f'+cfg_value =    "{v}"',
}
FINAL_CHARS = {"trailing_hyphen": "-", "trailing_under": "_"}

ORIGINAL = list(scanner.PATTERNS)

ARMS = {
    "baseline":  None,
    "fix_A":     r"(?=[^\w-]|$)",
    "fix_B":     r"(?![A-Za-z0-9_])",
}


def patched_table(lookahead):
    """Return a rule table with the trailing \b of the affected rules replaced."""
    out = []
    for name, pat, sev, ent in ORIGINAL:
        if lookahead and name in AFFECTED and pat.endswith(r"\b"):
            pat = pat[:-2] + lookahead
        out.append((name, pat, sev, ent))
    return out


def classify(line, rule):
    f = scan_diff(line, file_path=SCANNED_FILE)
    if not f:
        return "MISSED"
    names = {x.pattern_name for x in f}
    if rule in names:
        return "RULE_OK"
    if names - {FALLBACK}:
        return "OTHER"
    return "FALLBACK"


def gen(pat, n):
    """Generate n credentials from the rule's own expression."""
    body = pat[2:-2] if pat.startswith(r"\b") and pat.endswith(r"\b") else pat
    return [exrex.getone(body) for _ in range(n)]


def force_final(cred, ch):
    return cred[:-1] + ch if cred[-1] != ch else cred


def run_arm(name, lookahead):
    scanner.PATTERNS[:] = patched_table(lookahead)
    rules = {n: p for n, p, _, _ in ORIGINAL if n in AFFECTED}
    per_rule = {}
    for rule, pat in rules.items():
        random.seed(SEED)
        creds = gen(pat, N)
        cells = {}
        for cname, fn in CONTEXTS.items():
            c = [classify(fn(v), rule) for v in creds]
            cells[cname] = {k: round(c.count(k) / N, 4)
                            for k in ("RULE_OK", "OTHER", "FALLBACK", "MISSED")}
        for fname, ch in FINAL_CHARS.items():
            mod = [force_final(v, ch) for v in creds]
            c = [classify(CONTEXTS["canonical"](v), rule) for v in mod]
            cells[fname] = {k: round(c.count(k) / N, 4)
                            for k in ("RULE_OK", "OTHER", "FALLBACK", "MISSED")}
        br = min(v["RULE_OK"] for v in cells.values())
        per_rule[rule] = {"BR": round(br, 4), "cells": cells}

    # regression probe: credential ending in a WORD char, followed by a hyphen.
    # The original \b accepted this; a fix must not start rejecting it.
    probe = {}
    for rule, pat in rules.items():
        random.seed(SEED)
        creds = [force_final(c, "X") for c in gen(pat, N)]
        hit = sum(classify(f'+cfg_value = "{v}-suffix"', rule) == "RULE_OK"
                  for v in creds)
        probe[rule] = round(hit / N, 4)

    scanner.PATTERNS[:] = ORIGINAL
    return {"per_rule": per_rule, "hyphen_follows_probe": probe}


def false_positive_check(lookahead):
    """Scan the real-code corpus; a correct fix must add no new findings."""
    scanner.PATTERNS[:] = patched_table(lookahead)
    total, files = 0, 0
    corpus = os.path.join(_ROOT, "realcorpus")
    for root, _, names in os.walk(corpus):
        for fn in names:
            if not fn.endswith(".py"):
                continue
            files += 1
            try:
                with open(os.path.join(root, fn), encoding="utf-8",
                          errors="ignore") as fh:
                    for line in fh:
                        total += len(scan_diff("+" + line.rstrip("\n"),
                                               file_path=SCANNED_FILE))
            except OSError:
                pass
    scanner.PATTERNS[:] = ORIGINAL
    return {"files_scanned": files, "findings": total}


out = {
    "design": __doc__.strip(),
    "n_samples_per_cell": N,
    "seed": SEED,
    "candidates": {k: v for k, v in ARMS.items() if v},
    "arms": {},
    "false_positives_real_code": {},
}
for name, la in ARMS.items():
    out["arms"][name] = run_arm(name, la)
    out["false_positives_real_code"][name] = false_positive_check(la)

print(json.dumps(out, indent=1))
