"""
gen_figs4.py — Publication figures, v4 (journal-grade typography and colour).

WHAT CHANGED FROM v3 AND WHY
  v3 used a saturated categorical palette and an RdYlBu diverging map. That
  reads as dashboard/presentation styling rather than journal styling. Top
  venues (IEEE TSE, ACM TOSEM, EMSE, and the JAMA/NPG figure conventions in
  the sciences generally) use restrained, desaturated colour, minimal
  chart furniture, and near-monochrome line art for schematics.

  v4 therefore uses:
    * A muted JAMA-style categorical palette (dark slate, brick, muted amber,
      sage, steel) — desaturated, print-safe, and distinguishable under both
      deuteranopia and protanopia.
    * A custom muted diverging colormap (brick -> warm parchment -> slate)
      for rate matrices. No rainbow, no neon, no pure saturated primaries.
      Every cell is numerically annotated, so the encoding never has to carry
      the value alone — which also makes the figures safe in greyscale print.
    * Near-monochrome LINE ART for the architecture schematic: light neutral
      fills, thin dark rules, black text. Colour is used only to mark which
      subsystems the study measures, and only as a thin accent rule.
    * Restrained furniture: hairline spines, faint grids, no bold display
      type inside plots, legends outside the data area.

  Readability decisions from v3 are retained: large type (base 13.5 pt),
  generous physical size, and zero overlapping text.

Outputs vector PDF (for the manuscript) and 600 dpi PNG (for slides/print).
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np

_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RES = os.path.join(_ROOT, "results")
FIGS = os.path.join(_ROOT, "figures_pdf_vector")
PNGS = os.path.join(_ROOT, "figures_png_600dpi")
os.makedirs(FIGS, exist_ok=True)
os.makedirs(PNGS, exist_ok=True)

plt.rcParams.update({
    "font.size": 13.5,
    "axes.titlesize": 14,
    "axes.labelsize": 13.5,
    "xtick.labelsize": 12.5,
    "ytick.labelsize": 12.5,
    "legend.fontsize": 12.5,
    "font.family": "DejaVu Sans",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.edgecolor": "#3A3A3A",
    "axes.linewidth": 0.8,
    "axes.labelcolor": "#1A1A1A",
    "text.color": "#1A1A1A",
    "xtick.color": "#3A3A3A",
    "ytick.color": "#3A3A3A",
    "xtick.major.width": 0.8,
    "ytick.major.width": 0.8,
    "axes.grid": True,
    "grid.color": "#BFBFBF",
    "grid.alpha": 0.35,
    "grid.linewidth": 0.5,
    "legend.frameon": False,
    "figure.dpi": 200,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.06,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
})

# ── Muted JAMA-style categorical palette ────────────────────────────────────
C = {
    "slate":  "#374E55",   # dark slate  — primary
    "brick":  "#B24745",   # muted brick — failure / negative
    "amber":  "#DF8F44",   # muted amber — secondary
    "steel":  "#5B8CA8",   # muted steel blue
    "sage":   "#79AF97",   # sage green
    "plum":   "#6A6599",   # muted purple
    "stone":  "#80796B",   # warm grey
    "ink":    "#1A1A1A",
    "rule":   "#3A3A3A",
    "faint":  "#E8E6E1",
    "paper":  "#F5F2EC",
}
TOOLCOL = {"GitHub Autopilot": C["slate"],
           "Gitleaks 8.21.2": C["amber"],
           "TruffleHog 3.82.13": C["sage"]}

# Muted diverging map: brick (0) -> parchment (0.5) -> slate (1)
RATE_CMAP = LinearSegmentedColormap.from_list(
    "muted_rate", ["#9E3D3B", "#C77F6F", "#EDE4D3", "#7E9AAB", "#374E55"], N=256)


def load(n):
    with open(os.path.join(RES, n)) as f:
        return json.load(f)


def save(fig, name):
    fig.savefig(os.path.join(FIGS, name))
    fig.savefig(os.path.join(PNGS, name.replace(".pdf", ".png")), dpi=600)
    plt.close(fig)
    print("wrote", name)


def annotate(ax, M, fontsize=10.5):
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            v = M[i, j]
            if np.isnan(v):
                ax.text(j, i, "n/a", ha="center", va="center",
                        fontsize=fontsize - 1.5, color="#9A9A9A")
            else:
                col = "white" if (v < 0.28 or v > 0.82) else C["ink"]
                ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                        fontsize=fontsize, color=col)


def grid_lines(ax, M, lw=1.2):
    ax.set_xticks(np.arange(-.5, M.shape[1], 1), minor=True)
    ax.set_yticks(np.arange(-.5, M.shape[0], 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=lw)
    ax.grid(which="major", visible=False)
    ax.tick_params(which="minor", length=0)


# ══ FIG 0 — conceptual framework of boundary-mutation testing ═══════════════
def fig_framework():
    """Schematic of the method itself. This is the paper's contribution, so it
    is the first figure the reader meets. Near-monochrome line art; colour is
    used only to distinguish the four outcome classes, matching Fig. 3."""
    fig, ax = plt.subplots(figsize=(15.2, 7.8))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    def box(cx, cy, w, h, title, sub=None, fc=C["faint"], ec=C["rule"],
            tfs=13.0, sfs=11.5, lw=1.1, tcol=C["ink"]):
        ax.add_patch(FancyBboxPatch(
            (cx - w / 2, cy - h / 2), w, h,
            boxstyle="round,pad=0.008,rounding_size=0.012",
            facecolor=fc, edgecolor=ec, linewidth=lw))
        if sub:
            ax.text(cx, cy + h * 0.17, title, ha="center", va="center",
                    fontsize=tfs, color=tcol)
            ax.text(cx, cy - h * 0.22, sub, ha="center", va="center",
                    fontsize=sfs, color="#4A4A4A")
        else:
            ax.text(cx, cy, title, ha="center", va="center",
                    fontsize=tfs, color=tcol)

    def arrow(x1, y1, x2, y2, lw=1.3):
        ax.add_patch(FancyArrowPatch(
            (x1, y1), (x2, y2), arrowstyle="-|>", mutation_scale=15,
            linewidth=lw, color=C["rule"], shrinkA=0, shrinkB=0))

    def line(x1, y1, x2, y2, lw=1.3):
        ax.plot([x1, x2], [y1, y2], color=C["rule"], linewidth=lw,
                solid_capstyle="round")

    # ── tier 1: the pipeline ────────────────────────────────────────────────
    T1Y, T1H, T1W = 0.845, 0.150, 0.176
    xs = [0.100, 0.298, 0.496, 0.694, 0.892]
    stages = [
        ("Detection rule  $\\rho$", "regex $r(\\rho)$,  severity $sev(\\rho)$"),
        ("Rule-derived generator", "$\\mathcal{G}(\\rho)\\;\\rightarrow\\;c \\in \\mathcal{L}(r(\\rho))$"),
        ("Boundary embedding", "$b \\in B_{\\rho}\\;\\rightarrow\\;$ source line $b(c)$"),
        ("Scanner under test", "$T(b(c))\\;\\rightarrow\\;$ fired rule names"),
        ("Rule-level oracle", "$O(\\rho, b, c)$"),
    ]
    for x, (t, s_) in zip(xs, stages):
        box(x, T1Y, T1W, T1H, t, s_)
    for a, b_ in zip(xs, xs[1:]):
        arrow(a + T1W / 2, T1Y, b_ - T1W / 2, T1Y)

    # note under the generator / embedding stages
    ax.text(0.298, T1Y - T1H / 2 - 0.040,
            "syntactically in-scope",
            ha="center", va="top", fontsize=10.5, color="#6A6A6A", style="italic")
    ax.text(0.496, T1Y - T1H / 2 - 0.040,
            "12 embeddings, 6 families",
            ha="center", va="top", fontsize=10.5, color="#6A6A6A", style="italic")

    # ── tier 2: the four outcome classes ────────────────────────────────────
    T2Y, T2H, T2W = 0.470, 0.132, 0.196
    oxs = [0.185, 0.395, 0.605, 0.815]
    outs = [
        ("RULE_OK", "$\\rho$ fired", C["slate"], "white"),
        ("OTHER", "another named rule", C["steel"], "white"),
        ("FALLBACK", "entropy detector only", C["amber"], C["ink"]),
        ("MISSED", "nothing reported", C["brick"], "white"),
    ]
    for x, (t, s_, fc, tc) in zip(oxs, outs):
        box(x, T2Y, T2W, T2H, t, s_, fc=fc, ec=fc, tfs=13.0, sfs=11.0, tcol=tc)
        ax.texts[-1].set_color(tc)

    BAR1 = 0.645
    line(0.892, T1Y - T1H / 2, 0.892, BAR1)
    line(oxs[0], BAR1, 0.892, BAR1)
    for x in oxs:
        arrow(x, BAR1, x, T2Y + T2H / 2)

    # ── tier 3: the two derived metrics ─────────────────────────────────────
    T3Y, T3H = 0.150, 0.150
    box(0.300, T3Y, 0.345, T3H,
        "Boundary robustness   $BR(\\rho)$",
        "$\\min_{b}\\;\\hat p_{\\mathrm{RULE\\_OK}}(\\rho,b)$   —   worst-case detection",
        fc="white", ec=C["slate"], lw=1.6, tfs=13.0, sfs=11.2)
    box(0.700, T3Y, 0.345, T3H,
        "Label / severity preservation",
        "$RLP(\\rho)$, $SP(\\rho)$   —   correctly labelled once detected",
        fc="white", ec=C["slate"], lw=1.6, tfs=13.0, sfs=11.2)

    BAR2 = 0.320
    for x in oxs:
        line(x, T2Y - T2H / 2, x, BAR2)
    line(oxs[0], BAR2, oxs[-1], BAR2)
    arrow(0.300, BAR2, 0.300, T3Y + T3H / 2)
    arrow(0.700, BAR2, 0.700, T3Y + T3H / 2)

    save(fig, "fig00_framework.pdf")

# ══ FIG 1 — architecture, near-monochrome line art ══════════════════════════
def fig_architecture():
    fig, ax = plt.subplots(figsize=(14.0, 8.4))
    ax.set_xlim(0, 100); ax.set_ylim(0, 62); ax.axis("off")

    def box(x, y, w, h, label, sub, fill="#FFFFFF", edge=C["rule"],
            lw=1.1, fs=12.5, subfs=10.5, tc=C["ink"]):
        ax.add_patch(FancyBboxPatch((x, y), w, h,
                                    boxstyle="round,pad=0.5,rounding_size=0.9",
                                    fc=fill, ec=edge, lw=lw, zorder=2))
        ax.text(x + w/2, y + h*(0.62 if sub else 0.5), label, ha="center",
                va="center", fontsize=fs, color=tc, zorder=3)
        if sub:
            ax.text(x + w/2, y + h*0.26, sub, ha="center", va="center",
                    fontsize=subfs, color="#4A4A4A", zorder=3)

    def arrow(x1, y1, x2, y2, lw=1.0):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle="-|>",
                                     mutation_scale=13, lw=lw, color=C["rule"],
                                     zorder=1, shrinkA=2, shrinkB=2))

    def measured_tag(x, y, text, color):
        """Thin accent rule + label marking a measured subsystem."""
        ax.plot([x, x + 8.5], [y, y], color=color, lw=2.6, solid_capstyle="butt")
        ax.text(x + 9.4, y, text, fontsize=10.5, va="center", color=color)

    box(30, 54, 40, 6.4, "GitHub platform", "webhook delivery · REST API",
        fill=C["faint"])
    arrow(50, 54, 50, 49.9)
    ax.text(51.2, 51.8, "POST /webhook", fontsize=10.5, style="italic",
            color="#5A5A5A")

    ax.add_patch(FancyBboxPatch((4, 39.8), 92, 9.9,
                                boxstyle="round,pad=0.5,rounding_size=0.9",
                                fc="#FBFAF8", ec=C["rule"], lw=1.0, zorder=1))
    ax.text(6.4, 47.9, "Ingress security pipeline", fontsize=12.5, color=C["ink"])
    measured_tag(76.0, 47.9, "see supplement", C["stone"])
    stages = [("1 size cap", ""), ("2 HMAC-SHA256", "verify"),
              ("3 idempotency", "SHA-256 + Redis"), ("4 rate limit", "Redis INCR"),
              ("5 bot filter", ""), ("6 dispatch", "HTTP 202")]
    sw = 14.2
    for i, (s, sub) in enumerate(stages):
        x = 6.4 + i*(sw + 0.9)
        box(x, 40.9, sw, 5.3, s, sub, fill="#FFFFFF", fs=10.5, subfs=9.2)
        if i < len(stages) - 1:
            arrow(x + sw, 43.55, x + sw + 0.9, 43.55, lw=0.9)

    arrow(50, 39.8, 50, 35.7)
    ax.text(51.2, 37.6, "asynchronous", fontsize=10.5, style="italic",
            color="#5A5A5A")

    box(6, 28.5, 40, 7.1, "Durable event queue",
        "Redis list · cap 200 · at-least-once", fill=C["faint"])
    box(54, 28.5, 40, 7.1, "Bounded thread pool",
        "6 workers · cap 50 · backpressure", fill=C["faint"])
    measured_tag(76.0, 36.6, "see supplement", C["stone"])
    arrow(46, 32.0, 54, 32.0)
    arrow(26, 28.5, 26, 24.3); arrow(74, 28.5, 74, 24.3)

    box(6, 17.7, 88, 6.5, "Event handlers",
        "pull_request · issues · issue_comment (27 commands) · push · check_run",
        fill="#FFFFFF")
    arrow(28, 17.7, 28, 13.5); arrow(72, 17.7, 72, 13.5)

    box(6, 5.9, 44, 7.5, "AI router + circuit breakers",
        "Groq 70B → Groq 8B → Gemini → OpenRouter", fill="#FFFFFF",
        edge="#9A9A9A")
    ax.text(6, 3.6, "out of scope — no provider API credential",
            fontsize=10.5, style="italic", color="#7A7A7A")

    box(54, 5.9, 40, 7.5, "Secret scanner",
        "43 rules · entropy gate · fallback", fill=C["faint"], lw=1.5)
    measured_tag(54, 3.6, "primary object of study — RQ1–RQ5", C["brick"])

    save(fig, "fig01_architecture.pdf")


# ══ FIG 2 — mutation heatmap ════════════════════════════════════════════════
def fig_mutation_heatmap():
    d = load("mutation_bench.json")
    pp = {k: v for k, v in d["per_pattern"].items() if "error" not in v}
    ctxs = ["canonical", "single_quoted", "unquoted", "yaml_value", "json_value",
            "env_file", "url_query", "trailing_comma", "in_parens",
            "leading_space", "trailing_under", "trailing_hyphen"]
    nice = {"canonical": "quoted\nassign", "single_quoted": "single\nquote",
            "unquoted": "unquoted", "yaml_value": "YAML", "json_value": "JSON",
            "env_file": "dotenv", "url_query": "URL\nquery",
            "trailing_comma": "list\nmember", "in_parens": "call\narg",
            "leading_space": "extra\nspace", "trailing_under": "ends\n'_'",
            "trailing_hyphen": "ends\n'-'"}
    frag = [k for k, v in pp.items() if v.get("is_boundary_fragile")]
    rows = frag + [k for k in pp if k not in frag]
    M = np.full((len(rows), len(ctxs)), np.nan)
    for i, r_ in enumerate(rows):
        for j, c in enumerate(ctxs):
            cell = pp[r_]["cells"].get(c)
            if cell is not None:
                M[i, j] = cell["rule_match_rate"]

    fig, ax = plt.subplots(figsize=(16.0, 11.4))
    im = ax.imshow(M, cmap=RATE_CMAP, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(ctxs)))
    ax.set_xticklabels([nice[c] for c in ctxs], fontsize=14.5)
    ax.tick_params(axis="x", top=True, labeltop=True, bottom=False, labelbottom=False)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([("▸ " + r_) if r_ in frag else r_ for r_ in rows], fontsize=14.5)
    for i, r_ in enumerate(rows):
        if r_ in frag:
            ax.get_yticklabels()[i].set_color(C["brick"])
    annotate(ax, M, fontsize=12.5)
    ax.axhline(len(frag) - 0.5, color=C["rule"], lw=1.6)
    grid_lines(ax, M, lw=1.3)
    cb = fig.colorbar(im, ax=ax, fraction=0.026, pad=0.018)
    cb.set_label("rate at which the credential's own rule fires", fontsize=14)
    cb.ax.tick_params(labelsize=13)
    cb.outline.set_linewidth(0.6)
    save(fig, "fig02_mutation_heatmap.pdf")


# ══ FIG 3 — outcome decomposition ═══════════════════════════════════════════
def fig_fragile_outcomes():
    d = load("mutation_bench.json")
    frag = d["boundary_fragile_patterns"]; pp = d["per_pattern"]
    names = list(frag)
    rule = [pp[n]["cells"]["trailing_hyphen"]["rule_match_rate"]*100 for n in names]
    fb = [pp[n]["cells"]["trailing_hyphen"]["fallback_rate"]*100 for n in names]
    miss = [pp[n]["cells"]["trailing_hyphen"]["missed_rate"]*100 for n in names]
    y = np.arange(len(names))

    fig, ax = plt.subplots(figsize=(15.4, 7.4))
    ax.barh(y, rule, 0.62, label="rule fires — correct label and severity",
            color=C["slate"], edgecolor="white", linewidth=0.8)
    ax.barh(y, fb, 0.62, left=rule, label="entropy fallback only — severity downgraded",
            color=C["amber"], edgecolor="white", linewidth=0.8)
    ax.barh(y, miss, 0.62, left=np.array(rule)+np.array(fb),
            label="nothing reported — silent false negative",
            color=C["brick"], edgecolor="white", linewidth=0.8)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{n}\n({frag[n]['severity'].upper()})" for n in names],
                       fontsize=15)
    ax.invert_yaxis()
    ax.set_xlabel("share of samples (%)", fontsize=15)
    ax.set_xlim(0, 100)
    ax.tick_params(axis="x", labelsize=14)
    ax.grid(axis="y", visible=False)
    for i in range(len(names)):
        for val, left, col in ((rule[i], 0, "white"), (fb[i], rule[i], C["ink"]),
                               (miss[i], rule[i]+fb[i], "white")):
            if val > 7:
                ax.text(left + val/2, i, f"{val:.0f}", ha="center", va="center",
                        fontsize=14, color=col)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=3,
              fontsize=13.5, columnspacing=1.6, handlelength=1.5)
    save(fig, "fig03_fragile_outcomes.pdf")


# ══ FIG 4 — mechanism + marginal rates ══════════════════════════════════════
def fig_mechanism():
    d = load("mutation_bench.json"); pp = d["per_pattern"]
    prob = load("probability_analysis.json")
    spec = {"SendGrid API Key": ("{43}", "fixed"), "GCP API Key": ("{35}", "fixed"),
            "OpenAI API Key (new)": ("{50,}", "variable"),
            "Google OAuth Token": ("{68,}", "variable"),
            "JWT Token": ("{10,}", "variable")}
    names = list(spec)
    vals = [pp[n]["cells"]["trailing_hyphen"]["rule_match_rate"]*100 for n in names]
    cols = [C["brick"] if spec[n][1] == "fixed" else C["slate"] for n in names]

    fig, axes = plt.subplots(1, 2, figsize=(15.0, 6.0),
                             gridspec_kw={"width_ratios": [1, 1.08]})
    ax = axes[0]
    x = np.arange(len(names))
    b = ax.bar(x, vals, 0.58, color=cols, edgecolor="white", linewidth=0.9)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{n.replace(' API Key','').replace(' Token','')}\n{spec[n][0]}"
                        for n in names], fontsize=11.5)
    ax.set_ylabel("rule fires when value ends in '-'  (%)")
    ax.set_ylim(0, 108)
    for bb, v in zip(b, vals):
        ax.text(bb.get_x()+bb.get_width()/2, v+3, f"{v:.0f}", ha="center", fontsize=12)
    ax.legend(handles=[mpatches.Patch(color=C["brick"], label="fixed count — cannot backtrack"),
                       mpatches.Patch(color=C["slate"], label="variable count — backtracks, truncates")],
              loc="upper center", bbox_to_anchor=(0.5, -0.19))
    ax.set_title("(a) Quantifier type determines whether the\nfailure is total or partial",
                 fontsize=13.5)

    ax2 = axes[1]
    mr = prob["marginal_rates"]
    labels, mvals = [], []
    for k in ["SendGrid API Key", "GCP API Key", "Google OAuth Token",
              "JWT Token (HS384, 48-byte sig)", "JWT Token (HS256, 32-byte sig)"]:
        if k in mr:
            labels.append(k.replace(" API Key", "").replace(" Token", "").replace(" (", "\n("))
            mvals.append(mr[k]["p_not_correctly_labelled_marginal"]*100)
    yy = np.arange(len(labels))
    ax2.barh(yy, mvals, 0.58,
             color=[C["brick"] if v > 1 else C["amber"] if v > 0.1 else C["stone"]
                    for v in mvals], edgecolor="white", linewidth=0.9)
    ax2.set_yticks(yy); ax2.set_yticklabels(labels, fontsize=11)
    ax2.invert_yaxis()
    ax2.set_xlabel("marginal P(not correctly labelled)  (%)")
    ax2.set_xlim(0, max(mvals)*1.5 if max(mvals) > 0 else 1)
    ax2.grid(axis="y", visible=False)
    for i, v in enumerate(mvals):
        txt = "structurally impossible" if v == 0 else f"{v:.3f}   (1 in {round(100/v)})"
        ax2.text(v + max(mvals)*0.045, i, txt, va="center", fontsize=11)
    ax2.set_title("(b) Marginal rates; HS256 signatures\ncannot end in a hyphen",
                  fontsize=13.5)
    fig.tight_layout()
    save(fig, "fig04_mechanism.pdf")


# ══ FIG 4b — repair validation ══════════════════════════════════════════════
def fig_repair():
    d = load("fixvalidation_bench.json")
    rules = ["SendGrid API Key", "GCP API Key", "OpenAI API Key (new)",
             "JWT Token", "Google OAuth Token"]
    nice = {"SendGrid API Key": "SendGrid", "GCP API Key": "GCP",
            "OpenAI API Key (new)": "OpenAI", "JWT Token": "JWT",
            "Google OAuth Token": "Google OAuth"}
    arms = ["baseline", "fix_A", "fix_B"]
    arm_label = {"baseline": "baseline (original \\b)",
                 "fix_A": "candidate A  (?=[^\\w-]|$)",
                 "fix_B": "candidate B  (?![A-Za-z0-9_])  \u2014 recommended"}
    arm_col = {"baseline": C["brick"], "fix_A": C["amber"], "fix_B": C["slate"]}

    fig, axes = plt.subplots(1, 2, figsize=(15.2, 5.8))

    ax = axes[0]
    x = np.arange(len(rules))
    w = 0.26
    for i, arm in enumerate(arms):
        vals = [d["arms"][arm]["per_rule"][r]["BR"] * 100 for r in rules]
        ax.bar(x + (i - 1) * w, vals, w, label=arm_label[arm],
               color=arm_col[arm], edgecolor="white", linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels([nice[r] for r in rules], fontsize=12.5)
    ax.set_ylabel("boundary robustness BR (%)")
    ax.set_ylim(0, 108)
    ax.set_title("(a) Both candidates repair BR to 100%", fontsize=13.5)

    ax2 = axes[1]
    for i, arm in enumerate(arms):
        vals = [d["arms"][arm]["hyphen_follows_probe"][r] * 100 for r in rules]
        ax2.bar(x + (i - 1) * w, vals, w, label=arm_label[arm],
                color=arm_col[arm], edgecolor="white", linewidth=0.8)
    ax2.set_xticks(x)
    ax2.set_xticklabels([nice[r] for r in rules], fontsize=12.5)
    ax2.set_ylabel("regression probe: still fires (%)")
    ax2.set_ylim(0, 108)
    ref_line = ax2.axhline(100, color=C["rule"], ls=":", lw=1.0,
               label="original \\b: handles this case correctly")
    ax2.set_title("(b) Candidate A silently regresses on a case the\noriginal \\b handled correctly; candidate B does not",
                  fontsize=13.5)

    handles = [mpatches.Patch(color=arm_col[a], label=arm_label[a]) for a in arms]
    handles.append(ref_line)
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, -0.06),
              ncol=2, fontsize=11.5, frameon=False, columnspacing=1.8)
    fig.tight_layout(rect=[0, 0.10, 1, 1])
    save(fig, "fig04b_repair.pdf")


# ══ FIG 5 — cross-tool ══════════════════════════════════════════════════════
def fig_crosstool():
    d = load("crosstool_bench.json")
    tools = list(d["results"])
    ctxs = ["canonical", "single_quoted", "unquoted", "yaml_value", "json_value",
            "env_file", "url_query", "trailing_comma", "in_parens",
            "leading_space", "trailing_under", "trailing_hyphen"]
    nice = {"canonical": "quoted\nassign", "single_quoted": "single\nquote",
            "unquoted": "unquoted", "yaml_value": "YAML", "json_value": "JSON",
            "env_file": "dotenv", "url_query": "URL\nquery",
            "trailing_comma": "list\nmember", "in_parens": "call\narg",
            "leading_space": "extra\nspace", "trailing_under": "ends '_'",
            "trailing_hyphen": "ends '-'"}
    M = np.full((len(tools), len(ctxs)), np.nan)
    for i, t in enumerate(tools):
        agg = d["results"][t]["aggregate_by_context"]
        for j, c in enumerate(ctxs):
            if c in agg:
                M[i, j] = agg[c]["rule_match_rate"]

    fig, ax = plt.subplots(figsize=(15.0, 4.4))
    im = ax.imshow(M, cmap=RATE_CMAP, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(ctxs)))
    ax.set_xticklabels([nice[c] for c in ctxs], fontsize=12)
    ax.set_yticks(range(len(tools)))
    ax.set_yticklabels([f"{t}\n{d['results'][t]['n_types_analysed']}/{d['n_types']} types covered"
                        for t in tools], fontsize=12)
    annotate(ax, M, fontsize=11.5)
    grid_lines(ax, M, lw=1.6)
    cb = fig.colorbar(im, ax=ax, fraction=0.019, pad=0.015)
    cb.set_label("rule-level detection", fontsize=12)
    cb.outline.set_linewidth(0.6)
    save(fig, "fig05_crosstool.pdf")


# ══ FIG 6 — delimiter probe ═════════════════════════════════════════════════
def fig_delimiter():
    d = load("delimiter_bench.json")
    tools = list(d["results"]); terms = list(d["terminator_templates"])
    M = np.full((len(tools), len(terms)), np.nan)
    for i, t in enumerate(tools):
        agg = d["results"][t]["aggregate_by_terminator"]
        for j, tm in enumerate(terms):
            if agg.get(tm) is not None:
                M[i, j] = agg[tm]
    pretty = {"end_of_line": "EOL", "double_quote": '"', "single_quote": "'",
              "whitespace": "sp", "semicolon": ";", "backtick": "`", "colon": ":",
              "comma": ",", "close_paren": ")", "close_bracket": "]",
              "close_brace": "}", "ampersand": "&", "slash": "/",
              "angle_bracket": ">", "pipe": "|", "question_mark": "?"}

    fig, ax = plt.subplots(figsize=(15.0, 4.6))
    im = ax.imshow(M, cmap=RATE_CMAP, vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(len(terms)))
    ax.set_xticklabels([pretty[t] for t in terms], fontsize=14)
    ax.set_yticks(range(len(tools)))
    ax.set_yticklabels([f"{t}\n{len(d['results'][t]['types_covered'])} types"
                        for t in tools], fontsize=12)
    annotate(ax, M, fontsize=11.5)
    grid_lines(ax, M, lw=1.6)
    ax.set_xlabel("character immediately following the credential", fontsize=13)
    cb = fig.colorbar(im, ax=ax, fraction=0.019, pad=0.015)
    cb.set_label("rule-level detection", fontsize=12)
    cb.outline.set_linewidth(0.6)
    save(fig, "fig06_delimiter.pdf")


# ══ FIG 7 — co-occurrence ═══════════════════════════════════════════════════
def fig_cooccurrence():
    d = load("cooccurrence_bench.json")
    conds = ["A_single_credential", "B_paired_with_companion",
             "C_multiple_credentials", "D_realistic_settings_module"]
    labels = ["A\nsingle\ncredential", "B\npaired with\ncompanion",
              "C\nmultiple\ncredentials", "D\nrealistic\nsettings module"]
    fig, axes = plt.subplots(1, 2, figsize=(15.2, 5.9),
                             gridspec_kw={"width_ratios": [1.12, 1]})
    ax = axes[0]; x = np.arange(len(conds)); w = 0.25
    for i, (tool, r) in enumerate(d["results"].items()):
        vals = [r["detection_rate_by_condition"][c] for c in conds]
        ax.bar(x + (i-1)*w, vals, w, label=tool.split()[0], color=TOOLCOL[tool],
               edgecolor="white", linewidth=0.8)
        for xi, v in zip(x, vals):
            ax.text(xi + (i-1)*w, v + 0.024, f"{v:.2f}", ha="center", fontsize=10)
    ax.set_xticks(x); ax.set_xticklabels(labels, fontsize=11.5)
    ax.set_ylabel("file-level detection rate"); ax.set_ylim(0, 1.16)
    ax.grid(axis="x", visible=False)
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.13))
    ax.set_title("(a) Detection across deployment conditions", fontsize=13.5)

    ax2 = axes[1]
    pt = d["results"]["TruffleHog 3.82.13"]["per_type_by_condition"]
    types = sorted(pt["A_single_credential"])
    M = np.array([[pt[c][t] for c in conds] for t in types])
    im = ax2.imshow(M, cmap=RATE_CMAP, vmin=0, vmax=1, aspect="auto")
    ax2.set_xticks(range(len(conds))); ax2.set_xticklabels(["A", "B", "C", "D"], fontsize=14)
    ax2.set_yticks(range(len(types))); ax2.set_yticklabels(types, fontsize=11)
    annotate(ax2, M, fontsize=11)
    grid_lines(ax2, M, lw=1.6)
    ax2.set_title("(b) TruffleHog per credential type", fontsize=13.5)
    fig.tight_layout()
    save(fig, "fig07_cooccurrence.pdf")


# ══ FIG 8 — real-world FPs ══════════════════════════════════════════════════
def fig_realworld():
    d = load("realworld_bench.json")
    projs = [p for p, e in d["projects"].items() if "error" not in e]
    labels = [p.replace(" (system under test)", "\n(subject)") for p in projs]
    lines = [d["projects"][p]["autopilot"]["n_lines"] for p in projs]
    series = {"GitHub Autopilot": [d["projects"][p]["autopilot"]["n_findings"] for p in projs],
              "Gitleaks 8.21.2": [d["projects"][p]["gitleaks"]["n_findings"] for p in projs],
              "TruffleHog 3.82.13": [d["projects"][p]["trufflehog"]["n_findings"] for p in projs]}
    fig, axes = plt.subplots(1, 2, figsize=(15.2, 6.0),
                             gridspec_kw={"width_ratios": [1.35, 1]})
    ax = axes[0]; x = np.arange(len(projs)); w = 0.25
    for i, (tool, vals) in enumerate(series.items()):
        ax.bar(x + (i-1)*w, vals, w, label=tool.split()[0], color=TOOLCOL[tool],
               edgecolor="white", linewidth=0.8)
        for xi, v in zip(x, vals):
            ax.text(xi + (i-1)*w, v + 1.1, str(v), ha="center", fontsize=10.5)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{l}\n{n:,} lines" for l, n in zip(labels, lines)], fontsize=11)
    ax.set_ylabel("findings on unmodified real code")
    ax.grid(axis="x", visible=False)
    ax.legend()
    ax.set_title("(a) All findings are false positives", fontsize=13.5)

    ax2 = axes[1]
    t = d["totals"]["findings_per_kloc"]
    vals = [t["autopilot"], t["gitleaks"], t["trufflehog"]]
    bars = ax2.bar(["GitHub\nAutopilot", "Gitleaks", "TruffleHog"], vals, 0.52,
                   color=[C["slate"], C["amber"], C["sage"]], edgecolor="white",
                   linewidth=0.8)
    ax2.set_ylabel("false positives per 1,000 lines")
    ax2.set_ylim(0, max(vals)*1.28); ax2.grid(axis="x", visible=False)
    for b, v in zip(bars, vals):
        ax2.text(b.get_x()+b.get_width()/2, v + max(vals)*0.035, f"{v:.3f}",
                 ha="center", fontsize=12)
    ax2.set_title(f"(b) Aggregate over {d['totals']['total_lines_scanned']:,} lines",
                  fontsize=13.5)
    fig.tight_layout()
    save(fig, "fig08_realworld.pdf")


# ══ FIG 9 — scaling ═════════════════════════════════════════════════════════
def fig_scaling():
    d = load("scaling_bench.json"); c = d["curve"]
    th = [p["threads"] for p in c]
    tp = [p["throughput_ops_per_s"]["median"] for p in c]
    lo = [p["throughput_ops_per_s"]["ci95_mean"][0] for p in c]
    hi = [p["throughput_ops_per_s"]["ci95_mean"][1] for p in c]
    p95 = [p["p95_ms"]["median"] for p in c]
    p99 = [p["p99_ms"]["median"] for p in c]

    fig, axes = plt.subplots(1, 2, figsize=(15.2, 5.8))
    for ax, title, ylab in ((axes[0], "(a) Throughput vs concurrency", "throughput (operations/s)"),
                            (axes[1], "(b) Tail latency vs concurrency", "per-operation latency (ms)")):
        ax.set_xscale("log", base=2); ax.set_xticks(th)
        ax.set_xticklabels([str(t) for t in th])
        ax.set_xlabel("concurrent threads"); ax.set_ylabel(ylab)
        ax.set_title(title, fontsize=13.5)
        ax.axvspan(5.3, 6.8, color=C["stone"], alpha=0.16, zorder=0, lw=0)
    axes[0].plot(th, tp, "o-", color=C["slate"], lw=1.9, ms=6.5, label="median")
    axes[0].fill_between(th, lo, hi, color=C["slate"], alpha=0.16, label="95% CI of mean")
    axes[1].plot(th, p95, "s--", color=C["amber"], lw=1.9, ms=6.5, label="p95")
    axes[1].plot(th, p99, "^-", color=C["brick"], lw=1.9, ms=6.5, label="p99")
    for ax in axes:
        ax.annotate("configured operating\npoint (6 workers)",
                    xy=(6.0, ax.get_ylim()[1]*0.58), xytext=(15, ax.get_ylim()[1]*0.80),
                    fontsize=11, color="#4A4A4A",
                    arrowprops=dict(arrowstyle="->", color="#6A6A6A", lw=1.0))
        ax.legend(loc="upper left")
    fig.tight_layout()
    save(fig, "fig09_scaling.pdf")


# ══ FIG 10 — payload sweep ══════════════════════════════════════════════════
def fig_payload():
    d = load("systems_bench.json")["S3_payload_sweep"]["sweep"]
    kb = [s["actual_bytes"]/1000 for s in d]
    fig, ax = plt.subplots(figsize=(13.2, 6.2))
    ax.plot(kb, [s["hmac_p50_us"]["median"] for s in d], "o-", color=C["slate"],
            lw=1.9, ms=6.5, label="HMAC-SHA256 verify (p50)")
    ax.plot(kb, [s["idempotency_p50_us"]["median"] for s in d], "s--", color=C["amber"],
            lw=1.9, ms=6.5, label="idempotency dedup (p50)")
    ax.plot(kb, [s["rate_limit_p50_us"]["median"] for s in d], "^-.", color=C["sage"],
            lw=1.9, ms=6.5, label="IP rate limit (p50)")
    ax.plot(kb, [s["end_to_end_p99_us"]["median"] for s in d], "d:", color=C["ink"],
            lw=1.9, ms=6.5, label="end-to-end (p99)")
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlabel("webhook payload size (kB, log scale)")
    ax.set_ylabel("latency (µs, log scale)")
    ax.legend(loc="upper left")
    ax.axvline(100, color="#9A9A9A", ls=":", lw=1.2)
    ax.annotate("HMAC overtakes the\nRedis-backed stages", xy=(100, 265),
                xytext=(190, 52), fontsize=11.5, color="#4A4A4A",
                arrowprops=dict(arrowstyle="->", color="#6A6A6A", lw=1.0))
    save(fig, "fig10_payload.pdf")


# ══ FIG 11 — fault injection ════════════════════════════════════════════════
def fig_faults():
    d = load("systems_bench.json")["S5_fault_injection"]
    fig, axes = plt.subplots(1, 3, figsize=(15.2, 5.4))
    f = d["redis_killed_midrun"]
    v = [f["accepted_before_failure"], f["degraded_gracefully"], f["uncaught_exceptions"]]
    axes[0].bar(["accepted\nbefore kill", "degraded\ngracefully", "uncaught\nexceptions"],
                v, 0.58, color=[C["slate"], C["amber"], C["brick"]],
                edgecolor="white", linewidth=0.8)
    for i, x in enumerate(v):
        axes[0].text(i, x + 4, str(x), ha="center", fontsize=12)
    axes[0].set_title("(a) Redis killed mid-run (300 enqueues)", fontsize=13)
    axes[0].set_ylabel("events"); axes[0].grid(axis="x", visible=False)

    s = d["sustained_storm"]; v2 = [s["accepted"], s["rejected_full"]]
    axes[1].bar(["accepted", "rejected (503)"], v2, 0.52,
                color=[C["slate"], C["brick"]], edgecolor="white", linewidth=0.8)
    axes[1].axhline(s["configured_cap"], color=C["rule"], ls="--", lw=1.3)
    axes[1].text(1.46, s["configured_cap"]+26, f"cap = {s['configured_cap']}",
                 fontsize=11, ha="right")
    for i, x in enumerate(v2):
        axes[1].text(i, x + 20, str(x), ha="center", fontsize=12)
    axes[1].set_title("(b) Sustained storm (1,000 arrivals)", fontsize=13)
    axes[1].grid(axis="x", visible=False)

    q = d["duplicate_delivery_suppression"]
    v3 = [q["processed"], q["suppressed_as_duplicate"]]
    axes[2].bar(["processed\nexactly once", "suppressed\nas duplicate"], v3, 0.52,
                color=[C["slate"], C["stone"]], edgecolor="white", linewidth=0.8)
    for i, x in enumerate(v3):
        axes[2].text(i, x + 0.2, str(x), ha="center", fontsize=12)
    axes[2].set_title("(c) Duplicate delivery (same event × 10)", fontsize=13)
    axes[2].grid(axis="x", visible=False)
    fig.tight_layout()
    save(fig, "fig11_faults.pdf")


# ══ FIG 12 — baseline quality ═══════════════════════════════════════════════
def fig_quality():
    cov = load("coverage.json"); pkg = {}
    for path, info in cov["files"].items():
        parts = path.replace("\\", "/").split("/")
        name = ("other/cli" if parts[0] != "app" or len(parts) < 2
                else (parts[1] if not parts[1].endswith(".py") else "app(root)"))
        s = info["summary"]; dd = pkg.setdefault(name, {"c": 0, "n": 0})
        dd["c"] += s["covered_lines"]; dd["n"] += s["num_statements"]
    names = sorted(pkg, key=lambda k: -pkg[k]["n"])
    pcts = [100*pkg[k]["c"]/pkg[k]["n"] for k in names]
    stmts = [pkg[k]["n"] for k in names]
    cc = load("radon_cc.json")
    vals = np.array([b["complexity"] for blocks in cc.values() for b in blocks])

    fig, axes = plt.subplots(1, 2, figsize=(15.2, 6.2),
                             gridspec_kw={"width_ratios": [1.15, 1]})
    ax = axes[0]
    ax.barh(range(len(names)), pcts, 0.6, color=C["slate"], edgecolor="white",
            linewidth=0.8)
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels([f"{n}  ({s:,})" for n, s in zip(names, stmts)], fontsize=11.5)
    ax.invert_yaxis(); ax.set_xlabel("statement coverage (%)"); ax.set_xlim(0, 116)
    ax.grid(axis="y", visible=False)
    ax.axvline(84, color=C["rule"], ls="--", lw=1.3, label="overall coverage: 84%")
    for i, v in enumerate(pcts):
        ax.text(110, i, f"{v:.0f}", va="center", ha="right", fontsize=11.5)
    ax.legend(loc="lower right")
    ax.set_title("(a) Coverage by package (2,054/2,054 tests pass)", fontsize=13.5)

    ax2 = axes[1]
    bins = [1, 3, 6, 11, 21, 31, 200]
    labels = ["1–2", "3–5", "6–10", "11–20", "21–30", "31+"]
    counts, _ = np.histogram(vals, bins=bins)
    shades = ["#37474F", "#4E6470", "#6B8290", "#93A6B0", "#B9C4CA", C["brick"]]
    b2 = ax2.bar(labels, counts, 0.64, color=shades, edgecolor="white", linewidth=0.8)
    for b, v in zip(b2, counts):
        ax2.text(b.get_x()+b.get_width()/2, v+6, str(v), ha="center", fontsize=11.5)
    ax2.set_xlabel("cyclomatic complexity"); ax2.set_ylabel("functions / methods")
    ax2.grid(axis="x", visible=False)
    ax2.set_title(f"(b) Complexity, n={len(vals)}, mean={vals.mean():.2f}", fontsize=13.5)
    fig.tight_layout()
    save(fig, "fig12_quality.pdf")


if __name__ == "__main__":
    fig_framework()
    fig_architecture(); fig_mutation_heatmap(); fig_fragile_outcomes()
    fig_mechanism(); fig_repair(); fig_crosstool(); fig_delimiter(); fig_cooccurrence()
    fig_realworld(); fig_scaling(); fig_payload(); fig_faults(); fig_quality()
    print("\nvector PDF :", FIGS)
    print("600 dpi PNG:", PNGS)
