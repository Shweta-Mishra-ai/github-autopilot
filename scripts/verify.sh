#!/usr/bin/env bash
#
# scripts/verify.sh — run every gate CI runs, locally, before you push.
#
#   ./scripts/verify.sh            everything
#   ./scripts/verify.sh fast       lint + tests only, no regeneration
#   ./scripts/verify.sh --help
#
# WHY THIS EXISTS
#   The gates are spread across five CI jobs and each has flags that matter:
#   ruff lints app/ only, the suite must be run more than once because parts
#   of it are randomised, and two files are GENERATED — a stale README region
#   or codebase map fails the build for a reason invisible in the diff. That
#   has cost this repository three red builds. One command, same checks, same
#   flags.
#
# Exit code is 0 only if every gate passed.

set -uo pipefail

MODE="${1:-all}"
case "$MODE" in
  -h|--help|help)
    # Print the header comment block and stop at the first line of code.
    # A hardcoded line range breaks silently the moment the header is edited,
    # and prints shell source into the help text when it does.
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    exit 0
    ;;
esac

# Prefer the project venv, fall back to whatever python is on PATH.
PY="./.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
FAILED=()

step() { printf '\n%s── %s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '   %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
bad()  { printf '   %s✗%s %s\n' "$RED" "$OFF" "$1"; FAILED+=("$1"); }
note() { printf '   %s•%s %s\n' "$YELLOW" "$OFF" "$1"; }

# ── Lint ─────────────────────────────────────────────────────────────────────
# Exactly what .github/workflows/ci.yml runs: app/ only, this rule set, these
# ignores. Running plain `ruff check` reports findings in tests/ that CI does
# not gate on, which trains you to ignore the output.
step "Lint (ruff) — app/ only, CI's rule set"
if "$PY" -m ruff check app/ --select E,F,W,B,C4,SIM --ignore E501,B008 --quiet; then
  ok "no lint findings"
else
  bad "ruff check"
fi
if "$PY" -m ruff format --check app/ >/dev/null 2>&1; then
  ok "formatting clean"
else
  bad "ruff format --check (run: $PY -m ruff format app/)"
fi

# ── Tests ────────────────────────────────────────────────────────────────────
# Twice by default. Test ordering is randomised and several suites generate
# their inputs, so a single green run is weaker evidence than it looks — a
# 1-in-30,000 scanner miss and an ordering-dependent failure both reached CI
# after passing locally once.
RUNS=2
[ "$MODE" = "fast" ] && RUNS=1
step "Tests — ${RUNS} run(s), random ordering"
for i in $(seq 1 "$RUNS"); do
  if OUT=$("$PY" -m pytest -q --no-header 2>&1); then
    ok "run $i: $(printf '%s' "$OUT" | tail -1)"
  else
    bad "pytest (run $i)"
    printf '%s\n' "$OUT" | grep -E '^(FAILED|ERROR)' | head -10 | sed 's/^/     /'
  fi
done

if [ "$MODE" = "fast" ]; then
  step "Skipped in fast mode"
  note "generated files not checked — run without 'fast' before pushing"
else
  # ── Generated files ────────────────────────────────────────────────────────
  # These are committed and CI fails when they drift. Checked, not silently
  # regenerated: a script that rewrites your tree while you are reading its
  # output is a script you stop trusting.
  step "Generated files — README regions and the codebase map"
  if "$PY" -m app.handlers.readme --check >/dev/null 2>&1; then
    ok "README regions current"
  else
    bad "README regions stale (run: $PY -m app.handlers.readme)"
  fi

  MAP="docs/diagrams/codegraph.json"
  BEFORE=$(sha256sum "$MAP" 2>/dev/null | cut -d' ' -f1)
  GRAPH=$("$PY" -m app.intelligence.codegraph app server.py worker.py \
    --entrypoint app --entrypoint server --entrypoint worker \
    --entrypoint app.dashboard --entrypoint app.graphview \
    --out "$MAP" 2>&1 | tail -1)
  AFTER=$(sha256sum "$MAP" 2>/dev/null | cut -d' ' -f1)
  if [ "$BEFORE" = "$AFTER" ]; then
    ok "codebase map current — $GRAPH"
  else
    bad "codebase map was stale; it has been regenerated — commit it"
  fi
  case "$GRAPH" in
    *"0 cycles, 0 orphans"*) ok "no import cycles, no unreachable modules" ;;
    *) bad "structure: $GRAPH" ;;
  esac
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
printf '\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '%s%s✓ all gates passed%s\n' "$BOLD" "$GREEN" "$OFF"
  exit 0
fi
printf '%s%s✗ %d gate(s) failed:%s\n' "$BOLD" "$RED" "${#FAILED[@]}" "$OFF"
for f in "${FAILED[@]}"; do printf '   - %s\n' "$f"; done
exit 1
