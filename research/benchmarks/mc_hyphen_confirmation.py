"""
mc_hyphen_confirmation.py — independent confirmation that P(ends in '-') = 1/64
for a uniform draw over the 64-symbol base64url-style alphabet.

This is NOT reproducible by seed: `secrets` is deliberately unseedable
(it is a cryptographic RNG). The paper reports the EXACT theoretical value,
1/64, derived from the alphabet's construction (64 symbols, exactly one is
'-'), not this Monte Carlo estimate. This script exists only to confirm that
the exact value is not being misapplied: five independent 200,000-draw
checks should cluster tightly around 0.015625.
"""
import json
import secrets
import string
from collections import Counter

B64URL = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
TRIALS = 200000
N_CHECKS = 5

checks = []
for _ in range(N_CHECKS):
    c = Counter(secrets.choice(B64URL) for _ in range(TRIALS))
    checks.append(round(c.get("-", 0) / TRIALS, 6))

print(json.dumps({
    "theoretical": round(1 / 64, 6),
    "checks": checks,
    "trials_per_check": TRIALS,
    "min": min(checks), "max": max(checks),
}, indent=1))
