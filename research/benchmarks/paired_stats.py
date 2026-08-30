"""
Paired comparison between scanners on the identical positive corpora.

Checks first whether per-type detection is deterministic. If every credential
type yields recall exactly 0 or 1, the 40 samples within a type are not
independent observations, and a sample-level McNemar test would be
inflated by a factor of 40. In that case the defensible unit of analysis is
the credential TYPE, and the paired test is an exact binomial (sign) test on
the discordant types.
"""
import os

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_R = lambda *p: os.path.join(_ROOT, *p)
import json
from itertools import combinations
from math import comb

d = json.load(open(_R("results", "comparative_bench.json")))

for corpus_key in ("corpus_A", "corpus_B"):
    c = d[corpus_key]
    print(f"===== {c['corpus_label']} =====")
    per = {r["tool"]: r["per_type_recall"] for r in c["results"]}

    vals = {v for t in per.values() for v in t.values()}
    print("distinct per-type recall values observed:", sorted(vals))
    deterministic = vals <= {0.0, 1.0}
    print("per-type detection deterministic:", deterministic)

    for r in c["results"]:
        cf = r["confusion"]
        print(f"  {r['tool']:<20} P={r['precision']:.3f} "
              f"[{r['precision_ci95'][0]:.3f},{r['precision_ci95'][1]:.3f}]  "
              f"R={r['recall']:.3f} [{r['recall_ci95'][0]:.3f},"
              f"{r['recall_ci95'][1]:.3f}]  F1={r['f1']:.3f}  "
              f"tp={cf['tp']} fp={cf['fp']} fn={cf['fn']}")

    types = sorted(next(iter(per.values())).keys())
    print(f"  paired sign test over n={len(types)} credential types:")
    for a, b in combinations(per, 2):
        only_a = [t for t in types if per[a][t] > per[b][t]]
        only_b = [t for t in types if per[b][t] > per[a][t]]
        nb, nc = len(only_a), len(only_b)
        n = nb + nc
        if n == 0:
            p = 1.0
        else:
            k = min(nb, nc)
            p = min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)
        print(f"    {a:<20} vs {b:<20} discordant {nb}/{nc}  exact p={p:.4f}")
        if only_a:
            print(f"        only {a}: {', '.join(only_a)}")
        if only_b:
            print(f"        only {b}: {', '.join(only_b)}")
    print()
