"""
validate_autopilot_repair.py — What repair_autopilot.py fixes and what it
costs, for two copies of the primary subject:
    original  the pinned commit 38b2013 (the one every other result uses)
    repaired  the same tree after repair_autopilot.py
The subject's rules are imported, not run as a binary, so each copy is
scanned in its own process (SUBJECT_REPO selects it at import time).

PART A, the battery. bench_multitool.py's corpus, generated once from the
original subject (same types, seed and contexts), then scanned by both
copies, so only the scanner differs. `original` must reproduce the
published per-type results for the subject exactly
(results/multitool_bench.json); the script stops if it does not. Reported:
label-matched coverage, mislabelled coverage by rule, and per context the
label-matched types missed entirely; types lost or gained; and every cell
in which the repaired copy retains less than the original.

PART B, real code. The 31 pinned projects of bench_realworld_multilang.py,
staged identically; `original` must reproduce the published per-project,
per-rule counts. A finding is (file, line, rule); findings the repair adds
or removes are counted by rule, by project and by whether the file is a
test, documentation or fixture path. Matched values are never recorded.

Run: python validate_autopilot_repair.py > results/autopilot_repair_validation.json
     (env as bench_multitool.py and bench_realworld_multilang.py, plus
      SUBJECT_ORIGINAL and SUBJECT_REPAIRED, the two trees)
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "..", "benchmarks")
RESULTS = os.path.join(HERE, "..", "results")
TEST_DOC_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|fixtures?|docs?|examples?|mocks?|__mocks__)(/|$)"
    r"|\.(test|spec)\.|\.md$", re.I)


# ── workers, each run in a process whose SUBJECT_REPO picks the subject ──
def _gen(workdir, meta_out):
    sys.path.insert(0, BENCH)
    import bench_multitool as mt
    tt, gl = mt.tt, mt.gl
    random.seed(mt.SEED)
    cfg = tomllib.loads(open(gl.GITLEAKS_TOML, "rb").read().decode())
    th, _ = tt.types_trufflehog(tt.TRUFFLEHOG_SRC)
    gl_types = [t for t in tt.types_gitleaks(cfg) if t["id"] != f"GL:{gl.GENERIC}"]
    np_, _ = mt.types_from_rust_rules("NP", mt.load_noseyparker_rules(mt.NOSEYPARKER_RULES),
                                      mt._skip_np)
    kf, _ = mt.types_from_rust_rules("KF", mt.load_kingfisher_rules(mt.KINGFISHER_RULES),
                                     mt._skip_kf)
    meta, _info = mt.build(gl_types + th + tt.types_autopilot() + np_ + kf, workdir)
    json.dump(meta, open(meta_out, "w"))


def _scan(workdir, meta_in, out):
    sys.path.insert(0, BENCH)
    import bench_multitool as mt
    meta = {k: tuple(v) for k, v in json.load(open(meta_in)).items()}
    findings, _ = mt.tt.scan_autopilot(workdir, meta)
    per = mt.analyse_tool("autopilot", meta, findings)
    for t, rec in per.items():
        rec["label_matched"] = bool(rec["covered"] and
                                    mt.label_matches(t, "autopilot", rec["expected_rule"]))
    json.dump(per, open(out, "w"))


def _real(out):
    sys.path.insert(0, BENCH)
    import bench_realworld_multilang as rw
    rw.verify_pins()
    res, tmp = {}, tempfile.mkdtemp(prefix="rwap_")
    try:
        for _lang, repo, _sha, _stratum in rw.CORPUS:
            root = os.path.join(tmp, repo.replace("/", "_"))
            os.makedirs(root)
            n_files, n_lines = rw.stage(repo, root)
            fs = rw.scan_autopilot(root)
            res[repo] = {"n_files": n_files, "n_lines": n_lines,
                         "findings": sorted([f["file"], f["line"], f["rule"]] for f in fs)}
            shutil.rmtree(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    json.dump(res, open(out, "w"))


def _run(worker, subject, *args):
    env = {**os.environ, "SUBJECT_REPO": subject}
    subprocess.run([sys.executable, __file__, f"--{worker}", *args], env=env, check=True)


# ── comparison ──────────────────────────────────────────────────────────
def _missed_entirely(per):
    out = defaultdict(lambda: {"types": 0, "total_failure": 0})
    for rec in per.values():
        if not rec["label_matched"]:
            continue
        for c, v in rec["cells"].items():
            if v["paired_retention"] is not None:
                out[c]["types"] += 1
                out[c]["total_failure"] += v["paired_retention"] == 0.0
    return dict(sorted(out.items()))


def _battery_summary(per):
    return {"label_matched": sum(r["label_matched"] for r in per.values()),
            "mislabelled_coverage_by_rule": dict(Counter(
                r["expected_rule"] for r in per.values()
                if r["covered"] and not r["label_matched"]).most_common()),
            "missed_entirely_by_context": _missed_entirely(per)}


def main():
    subjects = {"original": os.environ["SUBJECT_ORIGINAL"],
                "repaired": os.environ["SUBJECT_REPAIRED"]}
    tmp = tempfile.mkdtemp(prefix="apval_")
    try:
        corpus, meta = os.path.join(tmp, "corpus"), os.path.join(tmp, "meta.json")
        os.mkdir(corpus)
        _run("gen", subjects["original"], corpus, meta)
        per, real = {}, {}
        for name, tree in subjects.items():
            _run("scan", tree, corpus, meta, os.path.join(tmp, f"{name}.json"))
            per[name] = json.load(open(os.path.join(tmp, f"{name}.json")))
            _run("real", tree, os.path.join(tmp, f"{name}_real.json"))
            real[name] = json.load(open(os.path.join(tmp, f"{name}_real.json")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # the original copy must be the published subject
    pub = json.load(open(os.path.join(RESULTS, "multitool_bench.json")))["per_type"]
    if {t: v["autopilot"] for t, v in pub.items()} != per["original"]:
        raise SystemExit("original subject does not reproduce multitool_bench.json")
    pub_rw = json.load(open(os.path.join(RESULTS, "realworld_multilang_bench.json")))["projects"]
    for repo, p in pub_rw.items():
        o = real["original"][repo]
        if ((p["n_files"], p["n_lines"], p["tools"]["autopilot"]["by_rule"])
                != (o["n_files"], o["n_lines"],
                    dict(Counter(f[2] for f in o["findings"]).most_common()))):
            raise SystemExit(f"original subject does not reproduce {repo}")

    o, r = per["original"], per["repaired"]
    worse = [{"type": t, "context": c, "original": v["paired_retention"],
              "repaired": r[t]["cells"][c]["paired_retention"]}
             for t in o if o[t]["label_matched"] and r[t]["label_matched"]
             for c, v in o[t]["cells"].items()
             if v["paired_retention"] is not None
             and r[t]["cells"].get(c, {}).get("paired_retention") is not None
             and r[t]["cells"][c]["paired_retention"] < v["paired_retention"]]

    os.environ["SUBJECT_REPO"] = subjects["original"]  # read only for _line
    sys.path.insert(0, BENCH)
    import bench_realworld_multilang as rw

    removed, added = Counter(), Counter()
    rm_proj, ad_proj, rm_path, ad_path = Counter(), Counter(), Counter(), Counter()
    same_line = keyword_on_line = later_occurrence = 0
    for repo in real["original"]:
        a = {tuple(f) for f in real["original"][repo]["findings"]}
        b = {tuple(f) for f in real["repaired"][repo]["findings"]}
        root = rw.repo_dir(repo)
        rm_lines = {(f, ln) for f, ln, _ in a - b}
        for f, ln, rule in a - b:
            removed[rule] += 1
            rm_proj[repo] += 1
            rm_path["test_doc_or_fixture" if TEST_DOC_PATH.search(f) else "other"] += 1
            # Is the removed finding a PagerDuty key the narrowed rule now
            # misses? Only if the provider is named on its line.
            keyword_on_line += bool(re.search(r"pagerduty", rw._line(root, f, ln), re.I))
        for f, ln, rule in b - a:
            added[rule] += 1
            ad_proj[repo] += 1
            ad_path["test_doc_or_fixture" if TEST_DOC_PATH.search(f) else "other"] += 1
            if (f, ln) in rm_lines:
                same_line += 1
                continue
            # The scanner reports a value once per scanned file, so a value the
            # PagerDuty rule claimed on an earlier line suppressed this one.
            # Values are compared here and never kept.
            here = set(re.findall(r"\b[a-z0-9]{32}\b", rw._line(root, f, ln)))
            later_occurrence += any(
                g == f and n < ln and here & set(re.findall(r"\b[a-z0-9]{32}\b",
                                                            rw._line(root, g, n)))
                for g, n in rm_lines)

    total = {k: sum(len(p["findings"]) for p in v.values()) for k, v in real.items()}
    return {
        "design": __doc__.strip().splitlines()[0],
        "original_reproduces_published": True,
        "battery": {
            "n_types": len(o),
            "original": _battery_summary(o),
            "repaired": _battery_summary(r),
            "label_matched_lost": sorted(t for t in o if o[t]["label_matched"]
                                         and not r[t]["label_matched"]),
            "label_matched_gained": sorted(t for t in o if r[t]["label_matched"]
                                           and not o[t]["label_matched"]),
            "cells_with_lower_retention": worse,
        },
        "real_code": {
            "n_projects": len(real["original"]),
            "n_files": sum(p["n_files"] for p in real["original"].values()),
            "n_lines": sum(p["n_lines"] for p in real["original"].values()),
            "findings": total,
            "removed_by_rule": dict(removed), "added_by_rule": dict(added),
            "removed_by_project": dict(rm_proj), "added_by_project": dict(ad_proj),
            "removed_by_path": dict(rm_path), "added_by_path": dict(ad_path),
            "removed_with_provider_named_on_line": keyword_on_line,
            "added_on_a_removed_finding_line": same_line,
            "added_on_a_later_occurrence_of_a_removed_value": later_occurrence,
        },
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("--"):
        {"--gen": _gen, "--scan": _scan, "--real": _real}[sys.argv[1]](*sys.argv[2:])
    else:
        print(json.dumps(main(), indent=2))
