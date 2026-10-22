"""
analysis_multitool.py — Cluster-robust statistics for the six-scanner
battery. Pure: reads results/multitool_bench.json, scans nothing, and is
deterministic (seeded bootstrap).

WHY CLUSTERS
  A credential type is drawn from a rule table, and several tables declare
  the same provider's credential: GitHub's ghp_ token is a Gitleaks, a
  TruffleHog, a Nosey Parker and a Kingfisher type. Type-level tests treat
  those as independent observations, which they are not. Every comparison
  below is therefore also run with the PROVIDER as the unit
  (bench_multitool.provider_of_type; the same normalised name the
  label-matching uses):
    cluster sign test   per provider, the share of its types tool x covers
                        minus the share tool y covers; an exact sign test
                        on the providers where that difference is nonzero
    cluster bootstrap   95% percentile interval for the difference in
                        label-matched coverage, resampling providers with
                        replacement (B = 10000, seeded)
  and p-values are Holm-adjusted over the 15 pairs of tools.

SENSITIVITY
  one_type_per_provider  the comparison repeated on one type per provider
                         (the first in sort order), which removes the
                         duplication instead of modelling it
  coverage_at_0.8        a type counted covered only when the expected
                         rule fires on at least 80% of canonical samples
                         instead of 50%

DEFECT PREVALENCE
  For each tool and each boundary context, the share of its label-matched
  types the context defeats entirely, with a provider-cluster bootstrap
  interval, so that "')' defeats most Gitleaks types" carries its
  uncertainty.

Run: python analysis_multitool.py > results/multitool_summary.json
"""
from __future__ import annotations

import json
import math
import os
import random
from collections import defaultdict

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RES = os.path.join(_ROOT, "results")

TOOLS = ("autopilot", "gitleaks", "trufflehog", "noseyparker", "kingfisher",
         "detectsecrets")
GENERATOR_OF = {"autopilot": "AP", "gitleaks": "GL", "trufflehog": "TH",
                "noseyparker": "NP", "kingfisher": "KF", "detectsecrets": None}
LINEAGE = {"GL": {"GL", "KF"}, "KF": {"GL", "KF"}}
GENERATORS = ("AP", "GL", "TH", "NP", "KF")
SEED = 20260823
B = 10000
BOUNDARY = ("unquoted", "yaml_value", "env_file", "url_query", "trailing_comma",
            "in_parens", "trailing_hyphen", "trailing_under", "url_query_amp")


def sign_test(a, b):
    n = a + b
    if n == 0:
        return 1.0
    k = min(a, b)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def holm(ps):
    """Holm step-down adjustment of a {name: p} mapping."""
    order = sorted(ps, key=ps.get)
    out, running = {}, 0.0
    for i, k in enumerate(order):
        running = max(running, min(1.0, (len(order) - i) * ps[k]))
        out[k] = running
    return out


def neutral_ids(x, y, pt):
    banned = set()
    for tool in (x, y):
        g = GENERATOR_OF[tool]
        if g:
            banned |= LINEAGE.get(g, {g})
    return sorted(t for t in pt if t.split(":", 1)[0] not in banned)


def covered(rec, threshold):
    """label_matched, at a canonical-rate threshold other than 0.5."""
    if threshold == 0.5:
        return rec["label_matched"]
    return rec["label_matched"] and rec["canonical_rule_ok"] >= threshold


def clusters(pt, ids):
    by = defaultdict(list)
    for t in ids:
        by[pt[t]["provider"]].append(t)
    return dict(sorted(by.items()))


def pair_stats(pt, x, y, ids, threshold=0.5, rng=None):
    a = sum(1 for t in ids if covered(pt[t][x], threshold) and not covered(pt[t][y], threshold))
    b = sum(1 for t in ids if covered(pt[t][y], threshold) and not covered(pt[t][x], threshold))
    cl = clusters(pt, ids)
    diffs = []
    for ts in cl.values():
        sx = sum(covered(pt[t][x], threshold) for t in ts) / len(ts)
        sy = sum(covered(pt[t][y], threshold) for t in ts) / len(ts)
        diffs.append((sx - sy, len(ts), sx * len(ts), sy * len(ts)))
    ca = sum(1 for d, *_ in diffs if d > 0)
    cb = sum(1 for d, *_ in diffs if d < 0)
    out = {"n_types": len(ids), f"only_{x}": a, f"only_{y}": b,
           "p_type_level": sign_test(a, b),
           "n_providers": len(cl), f"providers_favouring_{x}": ca,
           f"providers_favouring_{y}": cb, "p_cluster": sign_test(ca, cb)}
    if rng is not None and diffs:
        est = (sum(d[2] for d in diffs) - sum(d[3] for d in diffs)) / sum(d[1] for d in diffs)
        boots = []
        for _ in range(B):
            s = [diffs[rng.randrange(len(diffs))] for _ in diffs]
            n = sum(d[1] for d in s)
            boots.append((sum(d[2] for d in s) - sum(d[3] for d in s)) / n)
        boots.sort()
        out["coverage_difference"] = round(est, 4)
        out["coverage_difference_ci95_cluster_bootstrap"] = [
            round(boots[int(0.025 * B)], 4), round(boots[int(0.975 * B) - 1], 4)]
    return out


def compare_all(pt, select, threshold=0.5, bootstrap=True):
    rng = random.Random(SEED)
    out = {}
    for i, x in enumerate(TOOLS):
        for y in TOOLS[i + 1:]:
            ids = select(x, y)
            out[f"{x}_vs_{y}"] = pair_stats(pt, x, y, ids, threshold,
                                            rng if bootstrap else None)
    for key in ("p_type_level", "p_cluster"):
        adj = holm({k: v[key] for k, v in out.items()})
        for k in out:
            out[k][key + "_holm"] = adj[k]
    return out


def one_per_provider(pt, ids):
    seen, out = set(), []
    for t in ids:
        if pt[t]["provider"] not in seen:
            seen.add(pt[t]["provider"])
            out.append(t)
    return out


def prevalence(pt, tool, ctx, rng):
    """Share of the tool's label-matched types the context defeats entirely,
    with a provider-cluster bootstrap interval."""
    rows = [(pt[t]["provider"], pt[t][tool]["cells"][ctx]["paired_retention"] == 0.0)
            for t in pt if pt[t][tool]["label_matched"]
            and ctx in pt[t][tool]["cells"]
            and pt[t][tool]["cells"][ctx]["paired_retention"] is not None]
    if not rows:
        return None
    by = defaultdict(list)
    for p, fail in rows:
        by[p].append(fail)
    cl = list(by.values())
    boots = []
    for _ in range(B):
        s = [cl[rng.randrange(len(cl))] for _ in cl]
        boots.append(sum(map(sum, s)) / sum(map(len, s)))
    boots.sort()
    k = sum(f for _, f in rows)
    return {"types": len(rows), "total_failure": k, "share": round(k / len(rows), 4),
            "providers": len(cl),
            "ci95_cluster_bootstrap": [round(boots[int(0.025 * B)], 4),
                                       round(boots[int(0.975 * B) - 1], 4)]}


def main():
    with open(os.path.join(RES, "multitool_bench.json")) as fh:
        d = json.load(fh)
    pt = d["per_type"]

    def neutral(x, y):
        return neutral_ids(x, y, pt)

    def neutral_one(x, y):
        return one_per_provider(pt, neutral_ids(x, y, pt))
    rng = random.Random(SEED + 7)
    return {
        "design": __doc__.strip().splitlines()[0],
        "n_types": len(pt),
        "n_providers": len({v["provider"] for v in pt.values()}),
        "types_per_provider_max": max(
            sum(1 for v in pt.values() if v["provider"] == p)
            for p in {v["provider"] for v in pt.values()}),
        "neutral": compare_all(pt, neutral),
        "sensitivity": {
            "one_type_per_provider": compare_all(pt, neutral_one, bootstrap=False),
            "coverage_at_0.8": compare_all(pt, neutral, threshold=0.8, bootstrap=False),
        },
        "defect_prevalence": {tool: {c: prevalence(pt, tool, c, rng) for c in BOUNDARY}
                              for tool in TOOLS},
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
