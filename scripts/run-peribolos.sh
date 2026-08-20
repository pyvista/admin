#!/usr/bin/env bash
# Run peribolos against the pyvista org.
#
# Called from both GitHub Actions workflows and the Makefile. Takes a single
# argument: ``dry-run`` (default) or ``apply``. Reads GITHUB_TOKEN from the
# environment (installation token from the peribolos-admin GitHub App in CI,
# a PAT locally) and hands it to peribolos via a temp file.
#
# Usage:
#   GITHUB_TOKEN=... scripts/run-peribolos.sh dry-run
#   GITHUB_TOKEN=... scripts/run-peribolos.sh apply

set -euo pipefail

MODE="${1:-dry-run}"
if [[ $MODE != "dry-run" && $MODE != "apply" ]]; then
  echo "ERROR: mode must be 'dry-run' or 'apply', got '$MODE'" >&2
  exit 2
fi

# In CI, apply mode is only allowed on main. The environment gate in apply.yml
# is the primary defense; this is a belt-and-suspenders check in case someone
# calls the script directly from a workflow that bypassed the environment.
if [[ $MODE == "apply" && ${GITHUB_ACTIONS:-} == "true" && ${GITHUB_REF:-} != "refs/heads/main" ]]; then
  echo "ERROR: apply mode in CI is restricted to main (GITHUB_REF=${GITHUB_REF:-unset})" >&2
  exit 1
fi

# Peribolos image is pinned by digest in docker/peribolos/Dockerfile so
# Dependabot's `docker` ecosystem (see .github/dependabot.yml) can open PRs
# when a new digest is available. Parse the FROM line so the digest lives
# in exactly one place.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PERIBOLOS_DOCKERFILE="$SCRIPT_DIR/../docker/peribolos/Dockerfile"
PERIBOLOS_IMAGE="$(awk '/^FROM / {print $2; exit}' "$PERIBOLOS_DOCKERFILE")"
if [[ -z $PERIBOLOS_IMAGE ]]; then
  echo "ERROR: failed to parse FROM line from $PERIBOLOS_DOCKERFILE" >&2
  exit 1
fi

if [[ -z ${GITHUB_TOKEN:-} ]]; then
  echo "ERROR: GITHUB_TOKEN environment variable is required." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker must be installed and on PATH." >&2
  exit 1
fi

CRED_DIR="$(mktemp -d)"
LOG_FILE="$(mktemp)"
SYNC_LOG="$(mktemp)"
trap 'rm -rf "$CRED_DIR" "$LOG_FILE" "$SYNC_LOG"' EXIT

# Write the token to a file peribolos can read.
printf '%s' "$GITHUB_TOKEN" >"$CRED_DIR/token"
chmod 600 "$CRED_DIR/token"

# Expand the committed org.yaml with live repo state and the per-repo baseline.
# On apply, also remove drifted outside collaborators.
SYNC_ARGS=(--output org-expanded.yaml)
if [[ $MODE == "apply" ]]; then
  SYNC_ARGS+=(--remove-outside-collaborators)
fi
# Capture stderr to a file and replay it. It carries the notices about repo
# settings peribolos cannot enforce and about outside collaborators, which
# belong in the job summary next to the peribolos diff rather than buried in
# the raw step log. Redirect rather than tee through a process substitution so
# the file is complete before the summary reads it.
set +e
uv run scripts/sync-repos.py "${SYNC_ARGS[@]}" 2>"$SYNC_LOG"
SYNC_EXIT=$?
set -e
cat "$SYNC_LOG" >&2
if [[ $SYNC_EXIT -ne 0 ]]; then
  exit "$SYNC_EXIT"
fi

# Peribolos arguments. Add --confirm only for apply mode.
#
# --min-admins=2 matches our intentional two-admin posture. Peribolos's
# default (5) is a lockout safeguard we don't need here since admin team
# membership is PR-gated.
#
# --require-self=false disables peribolos's "ensure the authenticated user
# is an org admin" check. That check calls GET /user, which GitHub App
# installation tokens (how CI authenticates) cannot access.
#
# --maximum-removal-delta=0.5 loosens the default 0.25 (25%) ceiling on
# member removals. The initial bootstrap removes ~30% of current members
# (cleanup of drift + off-boarding tkoyama010 as org owner). Safe to
# tighten back to 0.25 after the first apply settles the drift.
#
# --github-hourly-tokens / --github-allowed-burst raise peribolos's
# client-side throttle above the default (300/100). 1500/100 stays under
# GitHub's secondary rate limit for write-heavy workloads (~1500/hr plus
# per-endpoint burst caps), which the raw primary quota doesn't respect.
# 4000 tripped the secondary limit and caused 429s on bulk team-repo
# mutations during the initial restructure.
#
# --fix-repos gates the entire top-level repos: block. Without it peribolos
# reads that section and discards it, so REPO_BASELINE in scripts/sync-repos.py
# was computed on every run and never applied to anything. Note that this flag
# also lets peribolos *create* any repo named in repos: that does not exist on
# GitHub. sync-repos.py builds that section from the live repo list and prunes
# entries whose repo is gone, so the expanded section is always a subset of
# what already exists. Keep it that way.
PERIBOLOS_ARGS=(
  --config-path=org-expanded.yaml
  --github-token-path=/etc/github/token
  --min-admins=2
  --require-self=false
  --maximum-removal-delta=0.5
  --github-hourly-tokens=1500
  --github-allowed-burst=100
  --fix-org
  --fix-org-members
  --fix-repos
  --fix-teams
  --fix-team-members
  --fix-team-repos
)
if [[ $MODE == "apply" ]]; then
  PERIBOLOS_ARGS+=(--confirm)
fi

# Run peribolos, stream output to the job log and capture to a file for the
# summary. Disable set -e for the docker invocation so a peribolos failure
# still flows into the summary; restore the exit code at the end.
set +e
docker run --rm --platform linux/amd64 \
  -v "$PWD:/workspace" \
  -v "$CRED_DIR/token:/etc/github/token:ro" \
  -w /workspace \
  "$PERIBOLOS_IMAGE" "${PERIBOLOS_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
EXIT=${PIPESTATUS[0]}
set -e

# Tolerate the new-team-with-repos case in dry-run.
#
# Without --confirm, peribolos does not create teams, so a team that is new
# in org.yaml has no GitHub ID yet. --fix-team-repos then tries to list that
# team's repos by ID and GitHub returns 404, which peribolos treats as fatal:
#
#   failed to list team 0(<name>) repos: return code not 2XX: 404 Not Found
#
# The apply run on main does not hit this: --fix-teams creates the team first,
# so --fix-team-repos finds it in the same invocation. That makes the failure
# a dry-run-only artifact, not a real problem with the change. Rather than
# force a two-PR dance (create team, then a follow-up PR to grant repos), we
# downgrade this specific failure to a pass and flag it in the summary.
#
# Guard: this only triggers when *every* fatal/error line is the benign
# new-team-repos 404. Any other fatal/error still fails the run.
TOLERATED=0
if [[ $MODE == "dry-run" && $EXIT -ne 0 ]]; then
  PROBLEMS="$(grep -E '"level":"(fatal|error)"' "$LOG_FILE" || true)"
  BENIGN="$(grep -E '"level":"(fatal|error)"' "$LOG_FILE" |
    grep -E 'failed to list team [0-9]+\(.+\) repos:.*404' || true)"
  if [[ -n $PROBLEMS && $PROBLEMS == "$BENIGN" ]]; then
    EXIT=0
    TOLERATED=1
    echo "NOTICE: dry-run hit the new-team-with-repos 404 only; treating as" \
      "pass. The apply on main creates the team before assigning repos." >&2
  fi
fi

# Post a readable dry-run / apply summary to the GitHub Actions job summary.
# Peribolos logs in JSON; extract level + msg per line for humans.
if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
  {
    printf '## peribolos %s\n\n' "$MODE"
    if [[ $TOLERATED -eq 1 ]]; then
      printf '> [!WARNING]\n'
      printf '> A new team in org.yaml has a repos block. peribolos cannot\n'
      printf '> preview repo grants for a team that does not exist yet, so this\n'
      printf '> dry-run was downgraded to a pass. The apply on main creates the\n'
      printf '> team and assigns its repos in one run.\n\n'
    fi
    if [[ $MODE == "dry-run" ]]; then
      printf 'Changes peribolos _would_ make against the live pyvista org:\n\n'
    else
      printf 'Changes peribolos applied against the live pyvista org:\n\n'
    fi
    printf '```\n'
    if command -v jq >/dev/null 2>&1; then
      jq -Rr 'try (fromjson | "\(.level | ascii_upcase): \(.msg)") catch .' <"$LOG_FILE"
    else
      cat "$LOG_FILE"
    fi
    printf '```\n'
    if [[ -s $SYNC_LOG ]]; then
      printf '\n<details><summary>Config notes from sync-repos.py</summary>\n\n'
      printf '```\n'
      cat "$SYNC_LOG"
      printf '```\n\n</details>\n'
    fi
  } >>"$GITHUB_STEP_SUMMARY"
fi

exit "$EXIT"
