"""
bench_delimiter.py — Delimiter-boundary probe across all three scanners.

The cross-tool mutation run (bench_crosstool.py) showed the three scanners
failing in DIFFERENT contexts. This experiment isolates the mechanism behind
each failure by varying exactly one thing: the single character immediately
FOLLOWING the credential.

For each (tool, credential type, terminator) we place an otherwise-valid
credential in a minimal line and record whether the tool's own expected rule
fires. Because only the terminating character changes between cells, any
difference is attributable to the rule's boundary handling and nothing else.

The terminators span the characters a credential is actually followed by in
real source: end of line, quotes, whitespace, punctuation, brackets of every
kind, and URL syntax.

Run: python bench_delimiter.py > results/delimiter_bench.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets as _secrets
import shutil
import string
import subprocess
import sys
import tempfile

SUBJECT_REPO_PATH = os.environ.get(
    "SUBJECT_REPO", os.path.expanduser("~/github-autopilot"))
sys.path.insert(0, SUBJECT_REPO_PATH)
from app.security.enhanced_secrets import scan_diff  # noqa: E402

logging.disable(logging.CRITICAL)

# Package root, derived from this file's location so the package is
# portable. Override with BMT_ROOT if you relocate results/ elsewhere.
_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRATCH = _ROOT
GITLEAKS = os.path.join(SCRATCH, "gitleaks")
TRUFFLEHOG = os.path.join(SCRATCH, "trufflehog")

ALNUM = string.ascii_letters + string.digits
UPPER_NUM = string.ascii_uppercase + string.digits
N = 20


def r(a, n):
    return "".join(_secrets.choice(a) for _ in range(n))


TYPES = {
    "aws_access_key_id": lambda: "AKIA" + r(UPPER_NUM, 16),
    "github_pat":        lambda: "ghp_" + r(ALNUM, 36),
    "gcp_api_key":       lambda: "AIza" + r(ALNUM + "_-", 35),
    "npm_token":         lambda: "npm_" + r(ALNUM, 36),
    "stripe_secret_key": lambda: "sk_live_" + r(ALNUM, 24),
    "sendgrid_api_key":  lambda: f"SG.{r(ALNUM+'_-',22)}.{r(ALNUM+'_-',43)}",
}

# (label, template). {V} is the credential. Exactly one character differs
# after the credential between most of these.
TERMINATORS = {
    "end_of_line":      "cfg = {V}",
    "double_quote":     'cfg = "{V}"',
    "single_quote":     "cfg = '{V}'",
    "whitespace":       "cfg = {V} next",
    "semicolon":        "cfg = {V};",
    "backtick":         "cfg = `{V}`",
    "colon":            "cfg:{V}",
    "comma":            "vals = [{V}, 1]",
    "close_paren":      "client = Client({V})",
    "close_bracket":    "vals = a[{V}]",
    "close_brace":      "vals = {{{V}}}",
    "ampersand":        "url = https://h/p?k={V}&z=1",
    "slash":            "url = https://h/{V}/x",
    "angle_bracket":    "tag = <{V}>",
    "pipe":             "cmd = echo {V}|cat",
    "question_mark":    "url = https://h/{V}?x=1",
}


def build(wd):
    meta, idx = {}, 0
    for t, gen in TYPES.items():
        for term, tmpl in TERMINATORS.items():
            for _ in range(N):
                idx += 1
                sid = f"d{idx:06d}"
                line = tmpl.replace("{V}", gen())
                open(os.path.join(wd, f"{sid}.txt"), "w").write(line + "\n")
                meta[sid] = (t, term)
    return meta


def scan_ap(wd, meta):
    out = {}
    for sid in meta:
        line = open(os.path.join(wd, f"{sid}.txt")).read().rstrip("\n")
        out[sid] = {x.pattern_name for x in
                    scan_diff("+" + line + "\n", file_path="app/settings.py")}
    return out


def scan_gl(wd, meta):
    rep = os.path.join(wd, "_g.json")
    subprocess.run([GITLEAKS, "detect", "--source", wd, "--no-git",
                    "--report-format", "json", "--report-path", rep],
                   capture_output=True, timeout=3600)
    out = {s: set() for s in meta}
    try:
        for f in json.load(open(rep)) or []:
            m = re.match(r"(d\d{6})\.txt", os.path.basename(f.get("File", "")))
            if m and m.group(1) in out:
                out[m.group(1)].add(f.get("RuleID", "?"))
    except Exception:
        pass
    return out


def scan_th(wd, meta):
    p = subprocess.run([TRUFFLEHOG, "filesystem", wd, "--json",
                        "--no-verification", "--no-update"],
                       capture_output=True, timeout=7200, text=True)
    out = {s: set() for s in meta}
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if "DetectorName" not in rec:
            continue
        fp = (rec.get("SourceMetadata", {}).get("Data", {})
                 .get("Filesystem", {}).get("file", ""))
        m = re.match(r"(d\d{6})\.txt", os.path.basename(fp))
        if m and m.group(1) in out:
            out[m.group(1)].add(rec["DetectorName"])
    return out


FALLBACKS = {"GitHub Autopilot": {"High Entropy String (unclassified)"},
             "Gitleaks 8.21.2": {"generic-api-key"},
             "TruffleHog 3.82.13": set()}


def analyse(tool, findings, meta):
    fb = FALLBACKS[tool]
    # expected rule per type, from the double-quote terminator (canonical)
    expected = {}
    for t in TYPES:
        counts = {}
        for sid, (tt, term) in meta.items():
            if tt == t and term == "double_quote":
                for rule in findings[sid] - fb:
                    counts[rule] = counts.get(rule, 0) + 1
        expected[t] = max(counts, key=counts.get) if counts else None

    grid, covered = {}, []
    for t in TYPES:
        exp = expected[t]
        base_ids = [s for s, (tt, term) in meta.items()
                    if tt == t and term == "double_quote"]
        base = (sum(1 for s in base_ids if exp and exp in findings[s])
                / max(len(base_ids), 1))
        if base < 0.5:
            continue
        covered.append(t)
        row = {}
        for term in TERMINATORS:
            ids = [s for s, (tt, tm) in meta.items() if tt == t and tm == term]
            hit = sum(1 for s in ids if exp and exp in findings[s])
            row[term] = round(hit / len(ids), 4)
        grid[t] = {"expected_rule": exp, "by_terminator": row}

    agg = {}
    for term in TERMINATORS:
        vals = [grid[t]["by_terminator"][term] for t in covered]
        agg[term] = round(sum(vals) / len(vals), 4) if vals else None

    accepted = [k for k, v in agg.items() if v is not None and v >= 0.95]
    rejected = [k for k, v in agg.items() if v is not None and v <= 0.05]
    partial = [k for k, v in agg.items()
               if v is not None and 0.05 < v < 0.95]
    return {
        "tool": tool,
        "types_covered": covered,
        "per_type": grid,
        "aggregate_by_terminator": agg,
        "terminators_accepted": accepted,
        "terminators_rejected": rejected,
        "terminators_partial": partial,
        "n_terminators_rejected": len(rejected),
    }


def main():
    wd = tempfile.mkdtemp(prefix="delim_")
    try:
        meta = build(wd)
        res = {
            "design": ("Only the character immediately following the credential "
                       "varies between cells, so any difference is attributable "
                       "to the rule's boundary handling."),
            "n_samples_per_cell": N,
            "n_types": len(TYPES),
            "n_terminators": len(TERMINATORS),
            "n_files": len(meta),
            "terminator_templates": TERMINATORS,
            "results": {},
        }
        for tool, fn in [("GitHub Autopilot", scan_ap),
                         ("Gitleaks 8.21.2", scan_gl),
                         ("TruffleHog 3.82.13", scan_th)]:
            f = fn(wd, meta)
            res["results"][tool] = analyse(tool, f, meta)
        return res
    finally:
        shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
