"""
validate_trufflehog_repair.py — What repair_trufflehog.py fixes and what it
costs, for three TruffleHog 3.82.13 binaries:
    release   the published linux/amd64 binary
    source    built from the pinned commit with the release's toolchain
              (go1.23.2, CGO_ENABLED=0, the release's ldflags); go1.24
              builds of this commit panic at start-up inside wazero 1.8.0
    repaired  the same build of the source after repair_trufflehog.py
`source` against `release` checks that the rebuild is the published tool,
so that `repaired` against `source` measures the repair and nothing else.

PART A, the battery. bench_multitool.py's corpus (same types, seed and
contexts); each binary is run TRUFFLEHOG_RUNS times and only detections in
every run are kept. Reported per binary: label-matched coverage per
generator, and per context the label-matched types the binary misses
entirely (paired retention 0).

PART B, real code. The 31 pinned projects of bench_realworld_multilang.py,
staged identically. A finding is (file, detector, raw secret), counted, so
TruffleHog's unreliable line numbers play no part. Findings the repair adds
or removes are listed (file, detector, the character after the secret); the
line text goes only to FINDINGS_DUMP, a local file.

Run: python validate_trufflehog_repair.py > results/trufflehog_repair_validation.json
     (env as bench_multitool.py, plus TRUFFLEHOG_RELEASE, TRUFFLEHOG_SOURCE,
      TRUFFLEHOG_REPAIRED; PARTS=B runs only the real-code check, which is
      how the same repair is checked on a later release)
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile
import tomllib
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "benchmarks"))
import bench_gitleaks_fulltable as gl  # noqa: E402
import bench_multitool as mt  # noqa: E402
import bench_realworld_multilang as rw  # noqa: E402
import bench_threetool_expanded as tt  # noqa: E402

BINARIES = {
    "release": os.environ.get("TRUFFLEHOG_RELEASE", tt.TRUFFLEHOG),
    "source": os.environ["TRUFFLEHOG_SOURCE"],
    "repaired": os.environ["TRUFFLEHOG_REPAIRED"],
}
DUMP = os.environ.get("FINDINGS_DUMP")


def types():
    """bench_multitool.main's type list, in its order, from the same seed."""
    random.seed(mt.SEED)
    cfg = tomllib.loads(open(gl.GITLEAKS_TOML, "rb").read().decode())
    th, _ = tt.types_trufflehog(tt.TRUFFLEHOG_SRC)
    gl_types = [t for t in tt.types_gitleaks(cfg) if t["id"] != f"GL:{gl.GENERIC}"]
    np_types, _ = mt.types_from_rust_rules(
        "NP", mt.load_noseyparker_rules(mt.NOSEYPARKER_RULES), mt._skip_np)
    kf_types, _ = mt.types_from_rust_rules(
        "KF", mt.load_kingfisher_rules(mt.KINGFISHER_RULES), mt._skip_kf)
    return gl_types + th + tt.types_autopilot() + np_types + kf_types


def part_a():
    ts = types()
    tmp = tempfile.mkdtemp(prefix="threp_")
    wd = os.path.join(tmp, "corpus")
    os.mkdir(wd)
    try:
        meta, info = mt.build(ts, wd)
        found, unstable = {}, {}
        for name, binary in BINARIES.items():
            tt.TRUFFLEHOG = binary
            found[name], _el, unstable[name] = mt.scan_trufflehog_stable(wd, meta)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    tids = sorted({tid for tid, _, _ in meta.values()})
    src = {g: [t for t in tids if t.startswith(g + ":")] for g in mt.GENERATORS}
    out = {"n_files": len(meta), "unstable_detections": unstable,
           "identical_detections": {
               "source_vs_release": sum(found["source"][s] == found["release"][s]
                                        for s in meta),
               "of": len(meta)}}
    for name in BINARIES:
        per = mt.analyse_tool("trufflehog", meta, found[name])
        for t in tids:
            per[t]["label_matched"] = bool(per[t]["covered"] and mt.label_matches(
                t, "trufflehog", per[t]["expected_rule"]))
        lm = [t for t in tids if per[t]["label_matched"]]
        ctx = Counter()
        n_ctx = Counter()
        for t in lm:
            for c, v in per[t]["cells"].items():
                if v["paired_retention"] is not None:
                    n_ctx[c] += 1
                    ctx[c] += v["paired_retention"] == 0.0
        out[name] = {
            "label_matched": {g: sum(per[t]["label_matched"] for t in ids)
                              for g, ids in src.items()},
            "label_matched_total": len(lm),
            "total_failure_by_context": {c: {"types": n_ctx[c], "total_failure": ctx[c]}
                                         for c in sorted(n_ctx)},
            "fragile_types": sum(bool(per[t]["fragile_contexts"]) for t in lm),
        }
        out[name]["_per_type"] = {t: per[t]["fragile_contexts"] for t in lm}
    rel, rep = out["source"].pop("_per_type"), out["repaired"].pop("_per_type")
    out["release"].pop("_per_type")
    out["repair_changes_fragility"] = {
        "types_fixed": sorted(t for t in rel if rel[t] and not rep.get(t)),
        "types_newly_fragile": sorted(t for t in rep if rep[t] and not rel.get(t)),
        "types_lost_coverage": sorted(set(rel) - set(rep)),
        "types_gained_coverage": sorted(set(rep) - set(rel)),
    }
    return out


def _once(root, binary):
    tt.TRUFFLEHOG = binary
    out = Counter()
    for (f, _ln, rule, raw), n in rw._trufflehog_once(root).items():
        out[(f, rule, raw)] += n
    return out


def part_b():
    rw.verify_pins()
    tmp = tempfile.mkdtemp(prefix="threp_rw_")
    totals = {n: 0 for n in BINARIES}
    unstable = {n: 0 for n in BINARIES}
    changes = {"added": [], "removed": []}
    dump = []
    n_files = n_lines = 0
    try:
        for lang, repo, _sha, stratum in rw.CORPUS:
            root = os.path.join(tmp, repo.replace("/", "_"))
            os.mkdir(root)
            nf, nl = rw.stage(repo, root)
            n_files += nf
            n_lines += nl
            stable = {}
            for name, binary in BINARIES.items():
                runs = [_once(root, binary) for _ in range(mt.TRUFFLEHOG_RUNS)]
                s, u = runs[0], Counter(runs[0])
                for r in runs[1:]:
                    s, u = s & r, u | r
                stable[name] = s
                totals[name] += sum(s.values())
                unstable[name] += sum((u - s).values())
            for kind, diff in (("added", stable["repaired"] - stable["source"]),
                               ("removed", stable["source"] - stable["repaired"])):
                for (f, rule, raw), n in sorted(diff.items()):
                    text = ""
                    ch = "?"
                    for ln in open(os.path.join(root, f), encoding="utf-8",
                                   errors="ignore"):
                        i = ln.find(raw)
                        if raw and i >= 0:
                            text = ln.rstrip("\n")
                            j = i + len(raw)
                            ch = text[j] if j < len(text) else "EOL"
                            break
                    rec = {"repo": repo, "language": lang, "stratum": stratum,
                           "file": f, "detector": rule, "count": n,
                           "char_after_secret": ch,
                           "category": rw.categorise({"file": f}, text)}
                    changes[kind].append(rec)
                    dump.append({**rec, "change": kind, "text": text})
            shutil.rmtree(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if DUMP:
        with open(DUMP, "w") as fh:
            for d in dump:
                fh.write(json.dumps(d) + "\n")

    def summary(rows):
        return {"n": sum(r["count"] for r in rows),
                "by_category": dict(Counter(r["category"] for r in rows).most_common()),
                "by_char_after_secret": dict(Counter(r["char_after_secret"] for r in rows).most_common()),
                "by_detector": dict(Counter(r["detector"] for r in rows).most_common())}
    return {"n_projects": len(rw.CORPUS), "n_files": n_files, "n_lines": n_lines,
            "totals_stable": totals, "unstable": unstable,
            "added": summary(changes["added"]), "removed": summary(changes["removed"]),
            "added_findings": changes["added"], "removed_findings": changes["removed"]}


PARTS = os.environ.get("PARTS", "A,B").split(",")


def main():
    out = {"design": __doc__.strip().splitlines()[0], "binaries": list(BINARIES)}
    if "A" in PARTS:
        out["battery"] = part_a()
    if "B" in PARTS:
        out["realworld"] = part_b()
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
