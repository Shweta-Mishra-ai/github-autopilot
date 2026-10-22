"""
validate_gitleaks_repair.py — What the two repairs of repair_gitleaks.py cost
on real code: Gitleaks 8.21.2 with the original, R1 and R2 tables over the
same staged tree of the 31 pinned projects of bench_realworld_multilang.py
(identical staging, so the same 9.87M lines).

A finding is (file, line, rule, secret). The report gives, per table, the
total, and for each repair the findings it adds and removes relative to the
original, with each added finding put through the real-world harness's
stated categoriser. An added finding is not assumed to be a false positive:
it may be a credential-shaped value the original missed because of the
character after it. Every added finding is listed (file, line, rule, the
character after the secret) so that it can be checked by hand; the line
text itself goes only to FINDINGS_DUMP, a local file, never to results/.

Run: python validate_gitleaks_repair.py R1=R1.toml R1P=R1P.toml R2=R2.toml \
         > results/gitleaks_repair_validation.json
     (any NAME=TABLE pairs; GITLEAKS and GITLEAKS_TOML name the binary and
      the original table, so the same check runs against a later release)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "benchmarks"))
import bench_gitleaks_fulltable as gl  # noqa: E402
import bench_realworld_multilang as rw  # noqa: E402

DUMP = os.environ.get("FINDINGS_DUMP")


# SCANNER=betterleaks runs the same check with Betterleaks, whose `dir`
# command takes the place of `detect --no-git` and writes the same report
SCANNER = os.environ.get("SCANNER", "gitleaks")


def scan(root, toml, rep):
    if SCANNER == "gitleaks":
        gl.GITLEAKS_TOML = toml
        return {(f["file"], f["line"], f["rule"], f["raw"])
                for f in rw.scan_gitleaks(root, rep)}
    subprocess.run([gl.GITLEAKS, "dir", root, "--config", toml, "--report-format",
                    "json", "--report-path", rep, "--no-banner", "--log-level", "error"],
                   capture_output=True, timeout=3600)
    return {(os.path.relpath(f["File"], root) if os.path.isabs(f["File"]) else f["File"],
             f["StartLine"], f["RuleID"], f.get("Secret") or "")
            for f in (json.load(open(rep)) or [])}


def after_char(root, f):
    line = rw._line(root, f[0], f[1])
    i = line.find(f[3])
    if i < 0:
        return "?", line
    j = i + len(f[3])
    return (line[j] if j < len(line) else "EOL"), line


def main(*pairs):
    rw.verify_pins()
    repairs = dict(p.split("=", 1) for p in pairs)
    tables = {"original": gl.GITLEAKS_TOML, **repairs}
    tmp = tempfile.mkdtemp(prefix="glrep_")
    totals = {t: Counter() for t in tables}
    added = {t: [] for t in repairs}
    removed = {t: [] for t in repairs}
    n_files = n_lines = 0
    dump = []
    try:
        for lang, repo, _sha, stratum in rw.CORPUS:
            root = os.path.join(tmp, repo.replace("/", "_"))
            os.mkdir(root)
            nf, nl = rw.stage(repo, root)
            n_files += nf
            n_lines += nl
            got = {t: scan(root, p, os.path.join(tmp, f"_{t}.json")) for t, p in tables.items()}
            for t, fs in got.items():
                totals[t][repo] = len(fs)
            for t in repairs:
                for kind, fs in (("added", got[t] - got["original"]),
                                 ("removed", got["original"] - got[t])):
                    for f in sorted(fs):
                        ch, line = after_char(root, f)
                        rec = {"repo": repo, "language": lang, "stratum": stratum,
                               "file": f[0], "line": f[1], "rule": f[2],
                               "char_after_secret": ch,
                               "category": rw.categorise({"file": f[0]}, line)}
                        (added if kind == "added" else removed)[t].append(rec)
                        dump.append({**rec, "table": t, "change": kind, "text": line})
            shutil.rmtree(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if DUMP:
        with open(DUMP, "w") as fh:
            for d in dump:
                fh.write(json.dumps(d) + "\n")

    def summary(rows):
        return {"n": len(rows),
                "by_category": dict(Counter(r["category"] for r in rows).most_common()),
                "by_char_after_secret": dict(Counter(r["char_after_secret"] for r in rows).most_common()),
                "by_rule": dict(Counter(r["rule"] for r in rows).most_common()),
                "by_repo": dict(Counter(r["repo"] for r in rows).most_common())}
    return {
        "design": __doc__.strip().splitlines()[0],
        "n_projects": len(rw.CORPUS), "n_files": n_files, "n_lines": n_lines,
        "totals": {t: sum(c.values()) for t, c in totals.items()},
        "totals_by_repo": {t: dict(c) for t, c in totals.items()},
        "added": {t: summary(v) for t, v in added.items()},
        "removed": {t: summary(v) for t, v in removed.items()},
        "added_findings": added,
        "removed_findings": removed,
    }


if __name__ == "__main__":
    print(json.dumps(main(*sys.argv[1:]), indent=2))
