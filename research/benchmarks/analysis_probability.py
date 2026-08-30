"""
analysis_probability.py — Correct marginal miss-probability per credential
type, and the BR / SP metrics.

WHY THIS IS NOT ONE NUMBER
  An earlier draft reported "roughly one leaked JWT in nine is missed". That
  conflated two different quantities:

      P(missed | credential ends in '-')     measured: 0.108 for JWT
      P(missed)                              = P(ends in '-') x P(missed | -)

  Computing the marginal requires P(ends in '-'), and that probability is NOT
  1/64 for every credential type. Two distinct cases:

  (a) Types whose tail is a uniform draw from the 64-symbol base64url-style
      alphabet (SendGrid, GCP, OpenAI project keys). Here P(ends in '-') is
      genuinely 1/64.

  (b) Types whose tail is base64url ENCODING of a byte string (JWT
      signatures). Here the final character is constrained by how many bits
      remain: for a signature of L bytes,

          L mod 3 == 0  -> final char carries 6 bits -> 64 symbols reachable
          L mod 3 == 2  -> final char carries 4 bits -> 16 symbols reachable
          L mod 3 == 1  -> final char carries 2 bits ->  4 symbols reachable

      '-' has base64url index 62. 62 is not a multiple of 4 or 16, so '-' is
      reachable ONLY when L mod 3 == 0. HS256 uses a 32-byte signature
      (32 mod 3 = 2), so an HS256 JWT can NEVER end in '-' and the defect can
      never fire for it. HS384 uses 48 bytes (48 mod 3 = 0) and can.

  This is verified empirically below rather than asserted.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import string
from collections import Counter

# Package root, derived from this file's location so the package is
# portable. Override with BMT_ROOT if you relocate results/ elsewhere.
_ROOT = os.environ.get(
    "BMT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RES = os.path.join(_ROOT, "results")
B64URL = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"


def b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def empirical_last_char_support(sig_bytes, trials=200000):
    """Which characters can actually appear last, and with what frequency."""
    c = Counter(b64url(secrets.token_bytes(sig_bytes))[-1] for _ in range(trials))
    return {
        "signature_bytes": sig_bytes,
        "len_mod_3": sig_bytes % 3,
        "encoded_len": len(b64url(secrets.token_bytes(sig_bytes))),
        "distinct_final_chars_observed": len(c),
        "hyphen_observed": c.get("-", 0),
        "p_ends_hyphen_empirical": round(c.get("-", 0) / trials, 6),
        "p_ends_hyphen_theoretical": round(1 / 64, 6) if sig_bytes % 3 == 0 else 0.0,
    }


def uniform_tail_p_hyphen(trials=200000):
    """Types whose tail is a uniform draw over the 64-symbol alphabet."""
    c = Counter(secrets.choice(B64URL) for _ in range(trials))
    return round(c.get("-", 0) / trials, 6)


def main():
    mut = json.load(open(os.path.join(RES, "mutation_bench.json")))
    pp = mut["per_pattern"]

    # ── conditional miss / downgrade rates, measured ────────────────────────
    conditional = {}
    for name in ["SendGrid API Key", "GCP API Key", "Google OAuth Token",
                 "OpenAI API Key (new)", "JWT Token"]:
        c = pp[name]["cells"]["trailing_hyphen"]
        conditional[name] = {
            "p_rule_fires_given_hyphen": c["rule_match_rate"],
            "p_fallback_given_hyphen": c["fallback_rate"],
            "p_missed_given_hyphen": c["missed_rate"],
            "p_not_correctly_labelled_given_hyphen":
                round(c["fallback_rate"] + c["missed_rate"], 4),
        }

    # ── P(ends in '-') per type ─────────────────────────────────────────────
    p_uniform = uniform_tail_p_hyphen()
    jwt_sigs = {
        "HS256 (32-byte signature)": empirical_last_char_support(32),
        "HS384 (48-byte signature)": empirical_last_char_support(48),
        "HS512 (64-byte signature)": empirical_last_char_support(64),
        "RS256 (256-byte signature)": empirical_last_char_support(256),
    }

    # The headline P(ends in '-') is the EXACT theoretical value, not the
    # CSPRNG Monte Carlo estimate: a uniform draw over a 64-symbol alphabet
    # with exactly one hyphen symbol has P(hyphen) = 1/64 by construction,
    # and this is not reproducible-by-seed since `secrets` is deliberately
    # unseedable. The 200,000-trial empirical draws above (p_uniform,
    # jwt_sigs) are retained as an independent confirmation that the exact
    # value is not being misapplied, not as the number the paper reports.
    P_EXACT_1_IN_64 = 1 / 64
    p_hyphen = {
        "SendGrid API Key": P_EXACT_1_IN_64,
        "GCP API Key": P_EXACT_1_IN_64,
        "OpenAI API Key (new)": P_EXACT_1_IN_64,
        "Google OAuth Token": P_EXACT_1_IN_64,
        "JWT Token (HS384, 48-byte sig)": P_EXACT_1_IN_64,
        "JWT Token (HS256, 32-byte sig)": 0.0,
    }
    p_hyphen_monte_carlo_confirmation = {
        "SendGrid API Key": p_uniform, "GCP API Key": p_uniform,
        "OpenAI API Key (new)": p_uniform, "Google OAuth Token": p_uniform,
        "JWT Token (HS384, 48-byte sig)":
            jwt_sigs["HS384 (48-byte signature)"]["p_ends_hyphen_empirical"],
        "JWT Token (HS256, 32-byte sig)":
            jwt_sigs["HS256 (32-byte signature)"]["p_ends_hyphen_empirical"],
    }

    # ── marginal rates ──────────────────────────────────────────────────────
    marginal = {}
    for label, ph in p_hyphen.items():
        base = label.split(" (")[0]
        key = "JWT Token" if base.startswith("JWT") else base
        if key not in conditional:
            continue
        c = conditional[key]
        m_missed = ph * c["p_missed_given_hyphen"]
        m_bad = ph * c["p_not_correctly_labelled_given_hyphen"]
        marginal[label] = {
            "p_ends_hyphen": round(ph, 6),
            "p_missed_marginal": round(m_missed, 8),
            "one_in_n_missed": (round(1 / m_missed) if m_missed > 0 else None),
            "p_not_correctly_labelled_marginal": round(m_bad, 8),
            "one_in_n_not_correctly_labelled":
                (round(1 / m_bad) if m_bad > 0 else None),
        }

    # ── BR / SP metrics per rule ────────────────────────────────────────────
    metrics = {}
    for name, v in pp.items():
        if "error" in v:
            continue
        cells = {k: c for k, c in v["cells"].items() if c is not None}
        if not cells:
            continue
        n = len(cells)
        br = sum(c["rule_match_rate"] for c in cells.values()) / n
        worst = min(c["rule_match_rate"] for c in cells.values())
        detected = sum(1 - c["missed_rate"] for c in cells.values()) / n
        sp = (sum(c["rule_match_rate"] for c in cells.values())
              / max(sum(1 - c["missed_rate"] for c in cells.values()), 1e-9))
        metrics[name] = {
            "BR_mean_boundary_robustness": round(br, 4),
            "BR_worst_context": round(worst, 4),
            "detection_rate_any": round(detected, 4),
            "SP_severity_preservation": round(min(sp, 1.0), 4),
            "n_applicable_contexts": n,
        }

    fragile = {k: v for k, v in metrics.items() if v["BR_worst_context"] < 0.9}
    return {
        "note": ("Marginal miss probability requires P(final character is a "
                 "hyphen), which differs by credential construction and is "
                 "ZERO for base64url encodings whose byte length is not a "
                 "multiple of three."),
        "p_hyphen_uniform64": p_uniform,
        "jwt_signature_analysis": jwt_sigs,
        "conditional_rates_measured": conditional,
        "marginal_rates": marginal,
        "p_ends_hyphen_monte_carlo_confirmation": p_hyphen_monte_carlo_confirmation,
        "BR_SP_metrics_per_rule": metrics,
        "rules_with_worst_context_below_0.9": sorted(fragile),
        "n_rules_measured": len(metrics),
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
