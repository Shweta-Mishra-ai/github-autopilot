"""
bench_realworld_newtools.py — Nosey Parker, Kingfisher and detect-secrets
on the 31-project, nine-language corpus of bench_realworld_multilang.py,
so that all six scanners of bench_multitool.py have a false-positive figure
on one file set.

SAME FILE SET
  Each project is staged with bench_realworld_multilang.stage(), the
  function that produced the committed three-tool result, and its file and
  line counts are checked against that result before scanning: a mismatch
  stops the run, since the comparison would then be across different
  trees. The three tools already measured are not re-run.

SCANNER SETTINGS
  As in bench_multitool.py: Nosey Parker's report without its default
  3-matches-per-finding limit, Kingfisher with --no-dedup, and no tool
  verifying a candidate against a provider (--no-validate, --no-verify;
  Nosey Parker does not verify).

FINDINGS
  A finding is one reported match: (file, line, rule). Every finding is
  put through the real-world harness's stated categoriser on its source
  line. No finding is asserted to be a live credential; released public
  code is assumed to contain none. Line text goes only to FINDINGS_DUMP, a
  local file, never to results/.

Run: python bench_realworld_newtools.py > results/realworld_newtools_bench.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_multitool as mt  # noqa: E402
import bench_realworld_multilang as rw  # noqa: E402

TOOLS = ("noseyparker", "kingfisher", "detectsecrets")
COMMITTED = os.environ.get(
    "REALWORLD_RESULTS", os.path.join(mt._ROOT, "results", "realworld_multilang_bench.json"))
DUMP = os.environ.get("FINDINGS_DUMP")


def _rel(root, p):
    return os.path.relpath(p, root) if os.path.isabs(p) else os.path.normpath(p)


def scan_noseyparker(root, work):
    ds = os.path.join(work, "_np.ds")
    shutil.rmtree(ds, ignore_errors=True)
    subprocess.run([mt.NOSEYPARKER, "scan", "--datastore", ds, root, "-q"],
                   capture_output=True, check=True, timeout=7200)
    p = subprocess.run([mt.NOSEYPARKER, "report", "--datastore", ds, "--format", "jsonl",
                        "--max-matches=0", "--max-provenance=0",
                        "--suppress-redundant=false"],
                       capture_output=True, text=True, check=True, timeout=7200)
    out = []
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        for m in rec["matches"]:
            ln = m["location"]["source_span"]["start"]["line"]
            for prov in m["provenance"]:
                if prov.get("kind") == "file":
                    out.append({"file": _rel(root, prov["path"]), "line": ln,
                                "rule": m["rule_text_id"]})
    return out


def scan_kingfisher(root, work):
    p = subprocess.run([mt.KINGFISHER, "scan", root, "--format", "jsonl", "--no-validate",
                        "--no-dedup", "--no-update-check", "-q"],
                       capture_output=True, text=True, timeout=7200)
    out = []
    for line in p.stdout.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "finding" in rec:
            f = rec["finding"]
            out.append({"file": _rel(root, f["path"]), "line": f.get("line"),
                        "rule": rec["rule"]["id"]})
    return out


def scan_detectsecrets(root, work):
    p = subprocess.run([mt.DETECT_SECRETS, "scan", "--all-files", "--no-verify", "."],
                       cwd=root, capture_output=True, text=True, check=True, timeout=7200)
    out = []
    for fn, recs in json.loads(p.stdout)["results"].items():
        for r in recs:
            out.append({"file": os.path.normpath(fn), "line": r["line_number"],
                        "rule": r["type"]})
    return out


SCAN = {"noseyparker": scan_noseyparker, "kingfisher": scan_kingfisher,
        "detectsecrets": scan_detectsecrets}


def main():
    rw.verify_pins()
    committed = json.load(open(COMMITTED))["projects"]
    dump = open(DUMP, "w") if DUMP else None
    tmp = tempfile.mkdtemp(prefix="rwnew_")
    projects = {}
    try:
        for lang, repo, sha, stratum in rw.CORPUS:
            root = os.path.join(tmp, repo.replace("/", "_"))
            os.makedirs(root)
            n_files, n_lines = rw.stage(repo, root)
            ref = committed[repo]
            if (n_files, n_lines) != (ref["n_files"], ref["n_lines"]):
                raise SystemExit(f"{repo}: staged {n_files} files / {n_lines} lines, "
                                 f"committed run had {ref['n_files']} / {ref['n_lines']}")
            per_tool, wall = {}, {}
            for tool in TOOLS:
                t0 = time.perf_counter()
                fs = sorted(SCAN[tool](root, tmp), key=lambda f: (f["file"], f["line"] or 0,
                                                                   f["rule"]))
                wall[tool] = round(time.perf_counter() - t0, 2)
                cats = Counter()
                for f in fs:
                    text = rw._line(root, f["file"], f["line"]) if f["line"] else ""
                    cat = rw.categorise(f, text)
                    cats[cat] += 1
                    if dump:
                        dump.write(json.dumps({"repo": repo, "tool": tool, **f,
                                               "category": cat, "text": text[:300]}) + "\n")
                fb = mt.FALLBACK[tool]
                per_tool[tool] = {
                    "n_findings": len(fs),
                    "n_findings_generic_rules": sum(f["rule"] in fb for f in fs),
                    "findings_per_kloc": round(len(fs) / (n_lines / 1000), 4) if n_lines else None,
                    "by_rule": dict(Counter(f["rule"] for f in fs).most_common()),
                    "by_category": dict(cats.most_common()),
                }
            projects[repo] = {"language": lang, "stratum": stratum, "commit": sha,
                              "n_files": n_files, "n_lines": n_lines,
                              "scan_wall_s": wall, "tools": per_tool}
            shutil.rmtree(root, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if dump:
            dump.close()

    def totals(ps):
        lines = sum(p["n_lines"] for p in ps)
        out = {"n_projects": len(ps), "n_lines": lines}
        for tool in TOOLS:
            n = sum(p["tools"][tool]["n_findings"] for p in ps)
            g = sum(p["tools"][tool]["n_findings_generic_rules"] for p in ps)
            cats = sum((Counter(p["tools"][tool]["by_category"]) for p in ps), Counter())
            out[tool] = {"n_findings": n, "n_findings_generic_rules": g,
                         "findings_per_kloc": round(n / (lines / 1000), 4) if lines else None,
                         "by_category": dict(cats.most_common())}
        return out
    ps = list(projects.values())
    return {
        "design": __doc__.strip().splitlines()[0],
        "n_projects": len(ps),
        "n_files": sum(p["n_files"] for p in ps),
        "n_lines": sum(p["n_lines"] for p in ps),
        "totals": totals(ps),
        "totals_by_stratum": {st: totals([p for p in ps if p["stratum"] == st])
                              for st in ("general", "credential_dense")},
        "totals_by_language": {lg: totals([p for p in ps if p["language"] == lg])
                               for lg in sorted({p["language"] for p in ps})},
        "projects": projects,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
