#!/usr/bin/env bash
#
# scripts/delete-merged-branches.sh — remove remote branches whose every commit
# is already on the default branch.
#
#   ./scripts/delete-merged-branches.sh            list what would be deleted
#   ./scripts/delete-merged-branches.sh --delete   actually delete them
#
# WHY THIS EXISTS AS A SCRIPT
#   Deleting the wrong branch loses work that exists nowhere else. The check
#   that makes it safe is `git merge-base --is-ancestor <branch> <default>`:
#   it is true only when the branch contributes no commit the default branch
#   does not already have, so deleting it can discard nothing.
#
#   A branch that merely "looks merged" — same name as a merged PR, or no
#   recent activity — is not the same thing and is never deleted here.
#
# WHAT IS NEVER TOUCHED
#   - the default branch
#   - any branch with at least one commit not on it
#   - anything in KEEP below
#
# Dry run by default, because a delete you did not intend is not recoverable
# from this script.

set -uo pipefail

REMOTE="${REMOTE:-origin}"

# Branches that must survive even if they ever look merged.
#
# `badges` holds the test-count JSON the README badge reads through
# shields.io, and CI force-pushes to it on every green run of the default
# branch. Deleting it breaks the badge for everyone and CI will not notice,
# because publishing the badge succeeds either way.
KEEP=("badges")

BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'

MODE="list"
case "${1:-}" in
  --delete) MODE="delete" ;;
  -h|--help|help)
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    exit 0
    ;;
  "") ;;
  *) printf 'unknown argument: %s (try --help)\n' "$1" >&2; exit 2 ;;
esac

printf '%sFetching %s…%s\n' "$BOLD" "$REMOTE" "$OFF"
git fetch --prune "$REMOTE" >/dev/null 2>&1 || {
  printf '%s✗ could not fetch %s%s\n' "$RED" "$REMOTE" "$OFF" >&2
  exit 1
}

DEFAULT="$(git symbolic-ref --quiet --short "refs/remotes/$REMOTE/HEAD" 2>/dev/null | sed "s#^$REMOTE/##")"
DEFAULT="${DEFAULT:-main}"
printf 'Default branch: %s%s%s\n\n' "$BOLD" "$DEFAULT" "$OFF"

MERGED=()
KEPT=()

while read -r branch; do
  [ -z "$branch" ] && continue
  [ "$branch" = "$DEFAULT" ] && continue
  [ "$branch" = "HEAD" ] && continue

  skip=""
  for k in "${KEEP[@]}"; do
    [ "$branch" = "$k" ] && skip="protected"
  done
  if [ -n "$skip" ]; then
    KEPT+=("$branch	$skip")
    continue
  fi

  if git merge-base --is-ancestor "$REMOTE/$branch" "$REMOTE/$DEFAULT" 2>/dev/null; then
    MERGED+=("$branch")
  else
    ahead="$(git rev-list --count "$REMOTE/$DEFAULT..$REMOTE/$branch" 2>/dev/null || echo '?')"
    KEPT+=("$branch	$ahead commit(s) not on $DEFAULT")
  fi
done < <(git branch -r --format='%(refname:short)' | sed "s#^$REMOTE/##")

if [ ${#KEPT[@]} -gt 0 ]; then
  printf '%sKeeping:%s\n' "$BOLD" "$OFF"
  for row in "${KEPT[@]}"; do
    printf '   %s•%s %-46s %s\n' "$YELLOW" "$OFF" "${row%%	*}" "${row#*	}"
  done
  printf '\n'
fi

if [ ${#MERGED[@]} -eq 0 ]; then
  printf '%s✓ nothing to delete — every branch has unmerged work%s\n' "$GREEN" "$OFF"
  exit 0
fi

printf '%sFully merged into %s%s\n' "$BOLD" "$DEFAULT" "$OFF"
for b in "${MERGED[@]}"; do
  printf '   %s✓%s %-46s %s\n' "$GREEN" "$OFF" "$b" "$(git rev-parse --short "$REMOTE/$b")"
done
printf '\n'

if [ "$MODE" = "list" ]; then
  printf '%s%d branch(es) would be deleted. Re-run with --delete to do it.%s\n' \
    "$BOLD" "${#MERGED[@]}" "$OFF"
  exit 0
fi

FAILED=()
for b in "${MERGED[@]}"; do
  if git push "$REMOTE" --delete "$b" >/dev/null 2>&1; then
    printf '   %s✓%s deleted %s\n' "$GREEN" "$OFF" "$b"
  else
    printf '   %s✗%s could not delete %s\n' "$RED" "$OFF" "$b"
    FAILED+=("$b")
  fi
done

printf '\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '%s%s✓ deleted %d branch(es)%s\n' "$BOLD" "$GREEN" "${#MERGED[@]}" "$OFF"
  printf 'Tip: Settings → General → "Automatically delete head branches" stops\n'
  printf 'these accumulating after every merged pull request.\n'
  exit 0
fi

printf '%s%s✗ %d branch(es) could not be deleted%s\n' "$BOLD" "$RED" "${#FAILED[@]}" "$OFF"
printf 'A 403 here means the credential may push commits but not delete refs.\n'
printf 'Delete them from the web UI instead: Code → Branches → All branches.\n'
exit 1
