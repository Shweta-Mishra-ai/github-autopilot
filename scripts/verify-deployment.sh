#!/usr/bin/env bash
#
# scripts/verify-deployment.sh — check a running deployment, not the code.
#
#   BASE_URL=https://your-app.onrender.com \
#   METRICS_AUTH_TOKEN=... ./scripts/verify-deployment.sh [owner/repo] [installation_id]
#
# WHY THIS EXISTS
#   The tests prove the code is correct. They cannot tell you the deployment
#   is answering, that its provider still serves the model ids it asks for, or
#   that the GitHub App can read what the commands need. Those fail silently
#   and independently of any green build — a retired model id took every AI
#   command down while CI stayed green for four days.
#
# Reads only. Nothing here changes anything.

set -uo pipefail

usage() {
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
esac

BASE_URL="${BASE_URL:-}"
TOKEN="${METRICS_AUTH_TOKEN:-}"
REPO="${1:-}"
INSTALLATION_ID="${2:-}"

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
FAILED=()

step() { printf '\n%s── %s%s\n' "$BOLD" "$1" "$OFF"; }
ok()   { printf '   %s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
bad()  { printf '   %s✗%s %s\n' "$RED" "$OFF" "$1"; FAILED+=("$1"); }
note() { printf '   %s•%s %s\n' "$YELLOW" "$OFF" "$1"; }

if [ -z "$BASE_URL" ]; then
  printf '%sBASE_URL is required.%s\n\n' "$RED" "$OFF"
  usage
  exit 2
fi
BASE_URL="${BASE_URL%/}"

auth=()
[ -n "$TOKEN" ] && auth=(-H "Authorization: Bearer $TOKEN")

# ── Is it up at all? ─────────────────────────────────────────────────────────
# /ping is unauthenticated on purpose: it is what the platform health check
# calls. On a free tier a cold start takes ~50s, so a single timeout here
# means "asleep", not "broken".
step "Reachable"
# curl already prints 000 for a connection failure, so a `|| echo 000`
# fallback concatenates onto it and reports "HTTP 000000".
CODE=$(curl -sS -o /tmp/vd_ping -w '%{http_code}' --max-time 75 "$BASE_URL/ping" 2>/dev/null)
CODE="${CODE:-000}"
if [ "$CODE" = "200" ]; then
  ok "/ping → 200 $(head -c 80 /tmp/vd_ping)"
else
  bad "/ping → HTTP $CODE (cold start takes ~50s on a free tier; retry once)"
  printf '\n%s✗ deployment is not answering — nothing below can be checked%s\n' "$RED" "$OFF"
  exit 1
fi

# ── Can it still call a model? ───────────────────────────────────────────────
step "Provider configuration"
if [ -z "$TOKEN" ]; then
  note "METRICS_AUTH_TOKEN not set — /health is auth-gated, skipping"
else
  HEALTH=$(curl -sS --max-time 30 "${auth[@]}" "$BASE_URL/health" || echo "")
  if [ -z "$HEALTH" ]; then
    bad "/health returned nothing"
  else
    STATUS=$(printf '%s' "$HEALTH" | jq -r '.status // "?"')
    CONFIG_ERR=$(printf '%s' "$HEALTH" | jq -r '.checks.llm_configuration_error // ""')
    MODELS=$(printf '%s' "$HEALTH" | jq -rc '.checks.llm_models // {}')

    case "$STATUS" in
      ok)           ok "status: ok" ;;
      misconfigured) bad "status: misconfigured" ;;
      *)            note "status: $STATUS" ;;
    esac
    ok "models in use: $MODELS"

    if [ -n "$CONFIG_ERR" ]; then
      # This is the failure that looks like an outage and is not one.
      bad "provider rejected the configured model or key"
      printf '     %s\n' "$CONFIG_ERR"
    else
      ok "no provider configuration fault recorded"
    fi

    OPEN=$(printf '%s' "$HEALTH" | jq -r \
      '[.checks.llm_providers // {} | to_entries[] | select(.value.state != "closed") | .key] | join(", ")')
    [ -n "$OPEN" ] && note "circuit breakers not closed: $OPEN" || ok "all circuit breakers closed"
  fi
fi

# ── Can the App do what the commands need? ───────────────────────────────────
step "GitHub App capabilities"
if [ -z "$TOKEN" ]; then
  note "METRICS_AUTH_TOKEN not set — /setup/doctor is auth-gated, skipping"
elif [ -z "$REPO" ] || [ -z "$INSTALLATION_ID" ]; then
  # The deployment settings need neither, and they are what fails quietly.
  DOC=$(curl -sS --max-time 30 "${auth[@]}" "$BASE_URL/setup/doctor" || echo "")
  printf '%s' "$DOC" | jq -r '.environment[]? | "   • \(.name): \(.state) — \(.detail)"' \
    | cut -c1-160 || note "no environment section returned"
  note "pass 'owner/repo installation_id' to also probe App permissions"
else
  DOC=$(curl -sS --max-time 60 "${auth[@]}" \
    "$BASE_URL/setup/doctor?repo=$REPO&installation_id=$INSTALLATION_ID" || echo "")
  if [ -z "$DOC" ]; then
    bad "/setup/doctor returned nothing"
  else
    HEALTHY=$(printf '%s' "$DOC" | jq -r '.healthy // false')
    printf '%s' "$DOC" | jq -r '.probes[]? | "   \(if .ok then "✓" else (if .required then "✗" else "•" end) end) \(.capability): \(.status)"'
    if [ "$HEALTHY" = "true" ]; then
      ok "every required capability works"
    else
      bad "some required capability is missing — see the rows marked ✗"
    fi
  fi
fi

# ── Are the configured model ids ones the providers still serve? ─────────────
#
# Asked HERE, of the running service, because the answer needs an API key and
# the key is a deployment secret. CI cannot check a provider it has no
# credential for -- the Gemini catalogue is unreadable in CI for exactly that
# reason -- but this deployment holds the key, so it can simply ask.
step "Model ids the providers still serve"
if [ -z "$TOKEN" ]; then
  note "METRICS_AUTH_TOKEN not set — /setup/doctor is auth-gated, skipping"
else
  MODELS=$(curl -sS --max-time 45 "${auth[@]}" "$BASE_URL/setup/doctor" \
    | jq -c '.models // empty' 2>/dev/null || echo "")
  if [ -z "$MODELS" ]; then
    note "this deployment predates the model report — redeploy to enable it"
  else
    printf '%s' "$MODELS" | jq -r '
      .providers | to_entries[] |
      (if .value.catalogue_readable
         then "   \(.key): \(.value.models_served) models served"
         else "   \(.key): catalogue unreadable (no API key set for it?)" end),
      (.value.slots[] |
        "     \(if .state == "ok" then "✓" elif .state == "retired" then "✗" else "?" end) " +
        "\(.slot) → \(.configured)" +
        (if .state == "retired" then "  RETIRED — best available: \(.suggested // "none")" else "" end) +
        (if (.substituted_to // "") != "" then "  (answering on \(.substituted_to))" else "" end))'
    if printf '%s' "$MODELS" | jq -e '[.providers[].slots[] | select(.state == "retired")] | length > 0' >/dev/null 2>&1; then
      bad "a configured model id is no longer served — see the rows marked ✗"
    else
      ok "every checkable model id is still served"
    fi
  fi
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
printf '\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '%s%s✓ deployment looks healthy%s\n' "$BOLD" "$GREEN" "$OFF"
  exit 0
fi
printf '%s%s✗ %d check(s) failed:%s\n' "$BOLD" "$RED" "${#FAILED[@]}" "$OFF"
for f in "${FAILED[@]}"; do printf '   - %s\n' "$f"; done
exit 1
