"""
Compute Boundary Robustness (BR), Rule-Label Preservation (RLP) and a genuine
severity-based Severity Preservation (SP) from the recorded mutation data.

SP requires a severity oracle. Two facts make it computable here:
  * every rule's severity is recorded per pattern in the raw results;
  * the fallback detector's severity is fixed at "medium", read from source
    (app/security/enhanced_secrets.py:745, pattern "High Entropy String
    (unclassified)").
The OTHER outcome (a different named rule fires) has rate exactly 0 in all
251 recorded cells, so no unrecorded third severity enters the computation.
"""
import os

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_R = lambda *p: os.path.join(_ROOT, *p)
import json

FALLBACK_SEVERITY = "medium"

d = json.load(open(_R("results", "mutation_bench.json")))
pp = d["per_pattern"]

rows = []
other_total = 0.0
for name, v in pp.items():
    sev = v["severity"]
    cells = {c: cell for c, cell in v["cells"].items() if cell is not None}
    if not cells:
        continue
    br = min(c["rule_match_rate"] for c in cells.values())

    num_rlp = sum(c["rule_match_rate"] for c in cells.values())
    # a fallback rescue preserves severity only if the rule's own severity
    # already equals the fallback detector's severity
    fb_preserves = 1.0 if sev == FALLBACK_SEVERITY else 0.0
    num_sp = sum(c["rule_match_rate"] + fb_preserves * c["fallback_rate"]
                 for c in cells.values())
    den = sum(1.0 - c["missed_rate"] for c in cells.values())
    other_total += sum(c["other_rule_rate"] for c in cells.values())

    rows.append((name, sev, len(cells), br, num_rlp / den, num_sp / den))

rows.sort(key=lambda r: r[3])
print(f"{'rule':<28}{'sev':<10}{'|B_p|':>6}{'BR':>9}{'RLP':>9}{'SP':>9}")
print("-" * 71)
for name, sev, nb, br, rlp, sp in rows:
    flag = "  <-- fragile" if br < 0.99 else ""
    print(f"{name[:27]:<28}{sev:<10}{nb:>6}{br:>9.4f}{rlp:>9.4f}{sp:>9.4f}{flag}")

print()
print(f"total OTHER mass across all cells: {other_total:.6f}")
frag = [r for r in rows if r[3] < 0.99]
print(f"fragile rules (BR<0.99): {len(frag)}")
print(f"macro BR  over 24 rules: {sum(r[3] for r in rows)/len(rows):.4f}")
print(f"macro RLP over 24 rules: {sum(r[4] for r in rows)/len(rows):.4f}")
print(f"macro SP  over 24 rules: {sum(r[5] for r in rows)/len(rows):.4f}")
print()
print("fragile-rule detail (RLP vs SP diverge only for medium-severity rules):")
for name, sev, nb, br, rlp, sp in frag:
    print(f"  {name:<24} sev={sev:<9} BR={br:.4f}  RLP={rlp:.4f}  SP={sp:.4f}")

json.dump({r[0]: {"severity": r[1], "n_applicable": r[2], "BR": r[3],
                  "RLP": r[4], "SP": r[5]} for r in rows},
          open(_R("results", "metrics_br_rlp_sp.json"), "w"), indent=1)
