"""
verify_tse.py — Re-derive every number in the TSE manuscript
(paper/tse/main.tex) from the raw results and check that the text states
it. Like verify_paper.py for the first manuscript: a number is checked by
recomputing it, formatting it as the paper does, and requiring that string
in the source.

It also fails while the manuscript contains a \\PENDING{...} marker: a
figure the replication package has not yet produced.

Run: python verify_tse.py          (exit status 1 on any failure)
     python verify_tse.py --allow-pending   (report, but tolerate markers)
"""
from __future__ import annotations

import json
import os
import re
import sys

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RES = os.path.join(_ROOT, "results")
TEX = os.path.join(_ROOT, "paper", "tse", "main.tex")

TOOLS = ("autopilot", "gitleaks", "trufflehog", "noseyparker", "kingfisher",
         "detectsecrets")
LABEL = {"autopilot": "AP", "gitleaks": "Gitleaks", "trufflehog": "TruffleHog",
         "noseyparker": "Nosey Parker", "kingfisher": "Kingfisher",
         "detectsecrets": "detect-secrets"}


def load(name):
    with open(os.path.join(RES, name)) as fh:
        return json.load(fh)


def thousands(n):
    return f"{n:,}".replace(",", "{,}")


def ci(pair):
    return f"[{pair[0]:.2f}, {pair[1]:.2f}]"


def sci(p):
    """The paper's p-value format."""
    if p >= 0.01:
        s = f"{p:.2g}"
        return "1" if s == "1" else s
    if p >= 0.001:
        return f"{p:.2g}"
    m, e = f"{p:.1e}".split("e")
    return f"${m}\\times10^{{{int(e)}}}$"


class Checker:
    def __init__(self, tex):
        self.tex = tex
        self.flat = re.sub(r"\s+", " ", tex)
        self.ok = self.bad = 0

    def has(self, what, s):
        if s in self.flat:
            self.ok += 1
        else:
            self.bad += 1
            print(f"MISSING  {what}: {s!r}")


def main(allow_pending):
    tex = open(TEX).read()
    c = Checker(tex)
    mb = load("multitool_bench.json")
    ms = load("multitool_summary.json")
    rw = load("realworld_multilang_bench.json")
    gv = load("gitleaks_repair_validation.json")
    g830 = load("gitleaks830_repair_validation.json")
    blv = load("betterleaks_repair_validation.json")
    ft = load("gitleaks_fulltable_bench.json")

    # ── the battery ─────────────────────────────────────────────────────
    gen = ms["types_generated"]
    n_types = sum(gen.values())
    c.has("types", thousands(n_types))
    c.has("files", thousands(mb["n_files"]))
    c.has("providers", f"belong to {ms['n_providers']} providers, up to "
                       f"{ms['types_per_provider_max']} types each")
    for g, n in gen.items():
        c.has(f"types from {g}", f"& {n} &" if g != "TH" else f"& {n} & own (Go detectors)")
    c.has("excluded", f"excluded ({len(mb['types_excluded'])}, listed)")
    sk = mb["rules_not_generators"]
    c.has("TH multi", f"{sk['TH']['needs_multiple_patterns']} need two or more patterns, "
                      f"{sk['TH']['pattern_built_by_helper']} build theirs in a helper and "
                      f"{sk['TH']['no_pattern']} have none")
    c.has("KF skip", f"({sk['KF']['needs_multiple_patterns']} depend on another rule's match, "
                     f"{sk['KF']['hidden_component_rule']} are hidden components and "
                     f"{sk['KF']['path_restricted']} are path rules)")
    sc = mb["self_coverage"]
    assert all(sc[t]["label_matched"] == sc[t]["of"] for t in ("autopilot", "gitleaks", "noseyparker"))
    c.has("self AP GL NP", "AP, GL and NP cover all of their own types")
    c.has("self KF TH", f"KF {sc['kingfisher']['label_matched']} of {sc['kingfisher']['of']}, "
                        f"TH {sc['trufflehog']['label_matched']} of {sc['trufflehog']['of']}")
    c.has("TH stable", "in the final battery none was unstable") if \
        mb["trufflehog_unstable_detections"] == 0 else c.has("TH unstable", "unstable")

    # ── Table: coverage by generator ────────────────────────────────────
    cov = ms["coverage_label_matched"]
    name = {"autopilot": "AP", "gitleaks": "Gitleaks", "trufflehog": "TruffleHog",
            "noseyparker": "Nosey Parker", "kingfisher": "Kingfisher",
            "detectsecrets": "detect-secrets"}
    for t in TOOLS:
        row = " & ".join(str(cov[t][g]) for g in ("AP", "GL", "TH", "NP", "KF"))
        c.has(f"coverage row {t}", f"{name[t]} & {row} & {ms['label_matched_total'][t]} \\\\")
    c.has("KF covers GL", f"Kingfisher covers {cov['kingfisher']['GL']} of Gitleaks' {gen['GL']}")
    c.has("GL covers KF", f"Gitleaks {cov['gitleaks']['KF']} of Kingfisher's {gen['KF']}")
    other = max(cov[a][g] for a, own in (("kingfisher", "KF"), ("gitleaks", "GL"))
                for g in ("AP", "TH", "NP"))
    c.has("other tables", f"against at most {other} of any other table's")

    # ── Table: defect prevalence ────────────────────────────────────────
    dp = ms["defect_prevalence"]
    lm = ms["label_matched_total"]
    c.has("label-matched row", "label-matched types & " +
          " & ".join(str(lm[t]) for t in TOOLS) + " \\\\")
    c.has("GL parens", f"\\textbf{{{dp['gitleaks']['in_parens']['total_failure']}}} "
                       f"{ci(dp['gitleaks']['in_parens']['ci95_cluster_bootstrap'])}")
    c.has("KF parens", f"\\textbf{{{dp['kingfisher']['in_parens']['total_failure']}}} "
                       f"{ci(dp['kingfisher']['in_parens']['ci95_cluster_bootstrap'])}")
    c.has("GL amp", f"\\textbf{{{dp['gitleaks']['url_query_amp']['total_failure']}}} "
                    f"{ci(dp['gitleaks']['url_query_amp']['ci95_cluster_bootstrap'])}")
    c.has("KF amp", f"\\textbf{{{dp['kingfisher']['url_query_amp']['total_failure']}}} "
                    f"{ci(dp['kingfisher']['url_query_amp']['ci95_cluster_bootstrap'])}")
    for t in ("gitleaks", "kingfisher"):
        v = dp[t]["trailing_comma"]
        c.has(f"{t} comma", f"{v['total_failure']} {ci(v['ci95_cluster_bootstrap'])}")
    hy = {t: dp[t]["trailing_hyphen"] for t in TOOLS}
    c.has("hyphen row", "trailing hyphen & " + " & ".join(
        (f"\\textbf{{{hy[t]['total_failure']} / {hy[t]['types']}}} "
         f"{ci(hy[t]['ci95_cluster_bootstrap'])}") if t == "trufflehog"
        else f"{hy[t]['total_failure']} / {hy[t]['types']}" for t in TOOLS) + " \\\\")
    uq = " & ".join(" / ".join(str(dp[t][x]["total_failure"])
                               for x in ("unquoted", "yaml_value", "env_file")) for t in TOOLS)
    c.has("unquoted row", f"unquoted / YAML / \\texttt{{.env}} & {uq} \\\\")
    c.has("AP eight", "AP's eight in every unquoted context")
    assert all(dp["autopilot"][x]["total_failure"] == 8 for x in
               ("unquoted", "yaml_value", "env_file", "in_parens", "url_query_amp",
                "trailing_comma"))
    c.has("TH robust", f"at most {max(dp['trufflehog'][x]['total_failure'] for x in ('unquoted','yaml_value','env_file','url_query','trailing_comma','in_parens','url_query_amp'))} of {lm['trufflehog']} types")
    c.has("TH hyphen text", f"a trailing hyphen defeats {hy['trufflehog']['total_failure']} "
                            f"of the {hy['trufflehog']['types']} label-matched types")
    hm = ms["hyphen_marginal_label_matched"]["trufflehog"]
    c.has("TH marginal", f"{hm['types_marginal_at_least_1_in_100']} of the {hm['types']} types "
                         f"have a marginal failure probability of at least 1 in 100 "
                         f"(maximum {hm['max_marginal']:.3f})")
    np_ = dp["noseyparker"]
    c.has("NP hyphen", f"{np_['trailing_hyphen']['total_failure']} of "
                       f"{np_['trailing_hyphen']['types']} applicable types fail on a trailing hyphen "
                       f"and at most {max(np_[x]['total_failure'] for x in ('unquoted','yaml_value','env_file','url_query','trailing_comma','in_parens','url_query_amp'))} "
                       f"of {lm['noseyparker']}")
    assert all(not v or v["total_failure"] == 0 for v in dp["detectsecrets"].values())
    c.has("DS none", f"detect-secrets fails none of its {lm['detectsecrets']} label-matched types")
    c.has("abstract GL", f"for {dp['gitleaks']['in_parens']['total_failure']} of its {lm['gitleaks']} types")
    c.has("abstract KF", f"({dp['kingfisher']['in_parens']['total_failure']} of {lm['kingfisher']})")
    c.has("abstract TH", f"(TruffleHog: {hy['trufflehog']['total_failure']} of "
                         f"{hy['trufflehog']['types']} applicable types)")

    # ── Gitleaks full table (n=40) ──────────────────────────────────────
    pr = ft["per_rule"]
    fr = {r: v["fragile_contexts"] for r, v in pr.items() if v.get("fragile_contexts")}
    paren = [r for r, v in fr.items() if "in_parens" in v]
    comma = [r for r in paren if "trailing_comma" in fr[r]]
    c.has("fulltable", f"Of 195 rules, {sum(s['n_analysed'] for s in ft['summary'].values())} "
                       f"are analysed and {len(fr)} lose more than half their values")
    c.has("fulltable paren", f"{len(paren)} of them in the call-argument context, "
                             f"{len(comma)} of those also after a comma")

    # ── Mislabelling ────────────────────────────────────────────────────
    mis = ms["mislabelled_types"]
    c.has("AP mislabel", f"credited with {mis['autopilot']} types")
    c.has("PagerDuty", f"{ms['mislabelled_by_rule']['autopilot']['PagerDuty Integration Key']} "
                       "of them under a PagerDuty rule")
    others = [mis[t] for t in TOOLS if t != "autopilot"]
    c.has("others mislabel", f"mislabel {min(others)} to {max(others)} types each")

    # ── Table: neutral comparison ───────────────────────────────────────
    ne = ms["neutral"]
    ab = {"autopilot": "AP", **{t: LABEL[t] for t in TOOLS if t != "autopilot"}}
    gens = {k: v["generators"] for k, v in mb["paired_coverage_neutral"].items()}
    for k, v in ne.items():
        x, y = k.split("_vs_")
        d = v["coverage_difference"]
        lo, hi = v["coverage_difference_ci95_cluster_bootstrap"]

        def f3(z):
            return f"${z:.3f}$" if z < 0 else f"${z:.3f}$"
        row = (f"{ab[x]} vs {ab[y]} & {', '.join(gens[k])} & {thousands(v['n_types'])} & "
               f"{v[f'only_{x}']} / {v[f'only_{y}']} & {sci(v['p_type_level_holm'])} & "
               f"{v[f'providers_favouring_{x}']} / {v[f'providers_favouring_{y}']} & "
               f"{sci(v['p_cluster_holm'])} & ${d:.3f}$ [${lo:.3f}$, ${hi:.3f}$] \\\\")
        c.has(f"neutral {k}", row.replace("$-", "$-"))
    ap_worse = [t for t in ("gitleaks", "trufflehog", "noseyparker", "kingfisher")
                if ne[f"autopilot_vs_{t}"]["p_cluster_holm"] < 0.05
                and ne[f"autopilot_vs_{t}"]["p_type_level_holm"] < 0.05
                and ne[f"autopilot_vs_{t}"]["coverage_difference_ci95_cluster_bootstrap"][1] < 0]
    assert len(ap_worse) == 4, ap_worse
    for sens in ("one_type_per_provider", "coverage_at_0.8"):
        for t in ap_worse:
            assert ms["sensitivity"][sens][f"autopilot_vs_{t}"]["p_type_level_holm"] < 0.05, (sens, t)
    c.has("four of five", "covering fewer types than four of the other five tools")
    ds = ne["autopilot_vs_detectsecrets"]
    c.has("AP DS", f"by type the difference is significant ($p_{{\\text{{Holm}}}}={sci(ds['p_type_level_holm'])}$), "
                   f"by provider it is {ds['providers_favouring_autopilot']} to {ds['providers_favouring_detectsecrets']} "
                   f"($p={sci(ds['p_cluster_holm'])}$)")
    assert ds["coverage_difference_ci95_cluster_bootstrap"][0] < 0 < ds["coverage_difference_ci95_cluster_bootstrap"][1]
    for k in ("gitleaks_vs_trufflehog", "trufflehog_vs_kingfisher"):
        assert ne[k]["p_type_level_holm"] > 0.05 and ne[k]["p_cluster_holm"] > 0.05
        for sens in ("one_type_per_provider", "coverage_at_0.8"):
            assert ms["sensitivity"][sens][k]["p_type_level_holm"] > 0.05
    c.has("neutral n", f"rest on {ne['gitleaks_vs_trufflehog']['n_types']} neutral types")
    mx = max(abs(v["coverage_difference"]) for v in ne.values())
    c.has("max diff", f"at most {round(mx * 100)} percentage points")

    # ── Encoding table (prefix stratum) ─────────────────────────────────
    enc = ms["encoding_label_matched"]
    pre = {t: enc[t]["prefix"] for t in TOOLS}
    c.has("enc header", " & & " + " & ".join(str(ms["label_matched_by_stratum"][t]["prefix"]) for t in TOOLS) + " \\\\")

    def row(ctxs):
        vals = []
        for t in TOOLS:
            fs = {pre[t][x]["total_failure"] for x in ctxs}
            assert len(fs) == 1, (t, ctxs, fs)
            vals.append(str(fs.pop()))
        return " & ".join(vals) + " \\\\"
    c.has("enc escaping", row(("esc_json_in_string", "esc_newline_after", "esc_quote_around")))
    c.has("enc shell", "shell default \\texttt{\\$\\{V:-v\\}} & " + row(("tpl_shell_default",)))
    c.has("enc html", "HTML-escaped quote & " + row(("mk_html_escaped",)))
    c.has("enc filesyntax", row(("fs_dockerfile_env", "fs_yaml_block")))
    c.has("enc base64", "base64; Kubernetes secret & " + row(("tx_base64_wrapped", "tx_k8s_secret")))
    c.has("enc url", "URL-encoded & " + row(("tx_url_encoded",)))
    c.has("enc split", "split and concatenated & " + row(("tx_split_concat",)))
    for x in ("fs_toml_inline", "mk_markdown_code", "mk_xml_attribute", "tpl_bearer_header",
              "tpl_jinja_default", "tpl_js_template"):
        assert all(pre[t][x]["total_failure"] == 0 for t in TOOLS), x

    # ── Gitleaks repair validation ──────────────────────────────────────
    c.has("rw files", f"{thousands(gv['n_files'])} files, {thousands(gv['n_lines'])} lines")
    tot = gv["totals"]
    c.has("gl before", f"({thousands(tot['original'])} before)")
    for t in ("R1", "R2"):
        c.has(f"{t} row", f"{t} & 0 & {gv['added'][t]['n']} & {gv['removed'][t]['n']} \\\\")
    assert gv["added"]["R1P"]["n"] == gv["removed"]["R1P"]["n"] == 0
    assert set(gv["added"]["R1"]["by_rule"]) == {"generic-api-key"}
    assert set(gv["added"]["R2"]["by_rule"]) == {"generic-api-key"}
    c.has("R1 backslash", f"R1 adds {gv['added']['R1']['by_char_after_secret'][chr(92)]} "
                          f"of its {gv['added']['R1']['n']} before a backslash")
    for v in ("R1", "R1P", "R2"):
        r = load(f"repair/gitleaks_fulltable_{v}.json")
        assert sum(s["n_analysed"] for s in r["summary"].values()) == 183
        frag = sum(s["n_boundary_fragile"] for s in r["summary"].values())
        assert frag == (1 if v == "R1P" else 0), (v, frag)
    for d, n in ((g830, "8.30.1"), (blv, "Betterleaks")):
        assert d["totals"]["original"] == d["totals"]["R1P"]
        assert d["added"]["R1P"]["n"] == d["removed"]["R1P"]["n"] == 0
        c.has(f"{n} totals", thousands(d["totals"]["original"]))
    c.has("830 counts", "applied to 8.30.1 (150 rules) and to Betterleaks 1.9.0 (335 rules)")
    assert load("repair/gitleaks-8.30.R1P.json")["n_changed"] == 150
    assert load("repair/betterleaks-1.9.R1P.json")["n_changed"] == 335
    th1 = load("repair/repair_trufflehog_3.82.13.json")["n_patterns_repaired"]
    th2 = load("repair/repair_trufflehog_3.99.3.json")["n_patterns_repaired"]
    c.has("TH patterns", f"It changes {th1} patterns in 3.82.13 and {th2} in 3.99.3")
    c.has("TH patterns 2", f"{th1} detector patterns in 3.82.13 end in")

    # TruffleHog repair validation
    tv = load("trufflehog_repair_validation.json")
    tb = tv["battery"]
    assert tb["identical_detections"]["source_vs_release"] == tb["identical_detections"]["of"]
    c.has("TH identical", f"identical to the release on all {thousands(tb['n_files'])} battery files")
    hyb = tb["source"]["total_failure_by_context"]["trailing_hyphen"]
    hya = tb["repaired"]["total_failure_by_context"]["trailing_hyphen"]
    c.has("TH repaired hyphen", f"defeated by a trailing hyphen on {'none' if hya['total_failure'] == 0 else hya['total_failure']} "
                                f"of the {hya['types']} applicable types, against {hyb['total_failure']} before")
    ch = tb["repair_changes_fragility"]
    assert not ch["types_newly_fragile"] and not ch["types_lost_coverage"] and not ch["types_gained_coverage"]
    c.has("TH lm unchanged", f"its {tb['repaired']['label_matched_total']} label-matched types are unchanged")
    rw1 = tv["realworld"]
    assert rw1["added"]["n"] == rw1["removed"]["n"] == 0
    assert len(set(rw1["totals_stable"].values())) == 1
    c.has("TH rw 3.82", f"the same {rw1['totals_stable']['release']} findings as the release")
    rw2 = load("trufflehog399_repair_validation.json")["realworld"]
    assert rw2["added"]["n"] == rw2["removed"]["n"] == 0
    assert len(set(rw2["totals_stable"].values())) == 1
    c.has("TH rw 3.99", f"the same {rw2['totals_stable']['source']} as its unmodified build")

    # reproducer matrix
    m = load("repair/reproducer_matrix_gitleaks_kingfisher.json")["found"]
    assert not m["gitleaks 8.30.1"]["gl_b_parens"] and m["gitleaks 8.30.1+R1P"]["gl_b_parens"]
    assert m["gitleaks 8.30.1"]["gl_f_hf_newline"] and not m["gitleaks 8.21.2"]["gl_f_hf_newline"]
    assert not any(m[k]["gl_e_escaped"] for k in m)
    assert not m["kingfisher 2.14.0"]["gl_c_amp"]
    t = load("repair/reproducer_matrix_trufflehog.json")["found"]
    assert not t["trufflehog 3.99.3"]["th_b_hyphen"] and t["3.99.3 repaired"]["th_b_hyphen"]
    assert t["trufflehog 3.99.3"]["th_d_hyphen_letter"]

    # ── real code: first three tools, exposure ─────────────────────────
    words = {9: "nine", 10: "ten"}
    c.has("languages", f"{words[rw['n_languages']]} languages")
    assert "ten languages" not in c.flat
    tt = rw["totals"]
    for tool in ("autopilot", "gitleaks", "trufflehog"):
        c.has(f"fp {tool}", f"{thousands(tt[tool]['n_findings'])} "
                            + ("findings " if tool == "autopilot" else "")
                            + f"({tt[tool]['findings_per_kloc']:.3f}")
    nw = load("realworld_newtools_bench.json")
    assert (nw["n_files"], nw["n_lines"]) == (rw["n_files"], rw["n_lines"])
    for tool, label in (("noseyparker", "Nosey Parker"), ("kingfisher", "Kingfisher"),
                        ("detectsecrets", "detect-secrets")):
        v = nw["totals"][tool]
        c.has(f"fp {tool}", f"{label} {thousands(v['n_findings'])} ({v['findings_per_kloc']:.3f})")
    ds = nw["totals"]["detectsecrets"]
    c.has("ds generic", f"{thousands(ds['n_findings_generic_rules'])} of its findings come from its high-entropy and keyword plugins")
    c.has("ds npm", f"{thousands(nw['projects']['npm/cli']['tools']['detectsecrets']['n_findings'])} from one repository")
    c.has("ds general", f"in the general stratum its rate is {nw['totals_by_stratum']['general']['detectsecrets']['findings_per_kloc']:.3f} per kLOC")
    assert nw["totals"]["kingfisher"]["n_findings_generic_rules"] == 0
    rates = {**{t: rw["totals"][t]["findings_per_kloc"] for t in ("autopilot", "gitleaks", "trufflehog")},
             **{t: nw["totals"][t]["findings_per_kloc"] for t in ("noseyparker", "kingfisher")}}
    assert min(rates, key=rates.get) == "kingfisher"
    pc = rw["part_c_credential_shaped_tokens"]
    op = pc["by_path"]["other_path"]
    c.has("exposure n", f"there are {thousands(op['n_tokens'])} such tokens")
    c.has("exposure share", f"{op['share_followed_by_char_gitleaks_terminator_rejects'] * 100:.1f}\\%")
    c.has("exposure comma", f"a comma alone by {op['share_by_following_character'][','] * 100:.1f}\\%")
    w = dict(pc["repository_weighted"])
    # median from exact per-repository counts, not the stored rounded share
    # (as verify_paper.py)
    shares = sorted(round(v["share_followed_by_char_gitleaks_terminator_rejects"] * v["n_tokens"])
                    / v["n_tokens"] for v in pc["by_repository"].values() if v["n_tokens"] >= 20)
    k = len(shares) // 2
    w["median_share_rejected"] = (shares[k - 1] + shares[k]) / 2 if len(shares) % 2 == 0 else shares[k]
    c.has("exposure median", f"Over the {w['n_repositories']} repositories with at least 20 such "
                             f"tokens the median share is {w['median_share_rejected'] * 100:.1f}\\%")

    pending = re.findall(r"\\PENDING\{([^}]*)\}", tex)
    print(f"{c.ok}/{c.ok + c.bad} numeric claims verified against raw data")
    if pending:
        print(f"{len(pending)} pending figure(s): " + "; ".join(pending))
    return c.bad == 0 and (allow_pending or not pending)


if __name__ == "__main__":
    sys.exit(0 if main("--allow-pending" in sys.argv) else 1)
