#!/usr/bin/env bash
# Run peribolos against the pyvista org.
#
# Called from both GitHub Actions workflows and the Makefile. Takes a single
# argument: ``dry-run`` (default) or ``apply``.
#
# Auth modes:
#   1. In CI, set APP_ID and APP_PRIVATE_KEY. Peribolos talks to GitHub as the
#      peribolos-admin GitHub App natively, which sidesteps the GET /user call
#      that installation tokens can't make.
#   2. Locally, set GITHUB_TOKEN to a personal PAT (or `gh auth token`).
#      Peribolos authenticates as you.
#
# GITHUB_TOKEN is always required because scripts/sync-repos.py uses it for
# repo listing and outside-collaborator cleanup. In CI, pass the installation
# token from actions/create-github-app-token as GITHUB_TOKEN plus the App
# credentials as APP_ID / APP_PRIVATE_KEY.
#
# Usage:
#   GITHUB_TOKEN=... scripts/run-peribolos.sh dry-run
#   APP_ID=... APP_PRIVATE_KEY=... GITHUB_TOKEN=... scripts/run-peribolos.sh apply

set -euo pipefail

MODE="${1:-dry-run}"
if [[ $MODE != "dry-run" && $MODE != "apply" ]]; then
  echo "ERROR: mode must be 'dry-run' or 'apply', got '$MODE'" >&2
  exit 2
fi

PERIBOLOS_IMAGE="us-docker.pkg.dev/k8s-infra-prow/images/peribolos:latest"

if [[ -z ${GITHUB_TOKEN:-} ]]; then
  echo "ERROR: GITHUB_TOKEN environment variable is required (used by sync-repos.py)." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker must be installed and on PATH." >&2
  exit 1
fi

CRED_DIR="$(mktemp -d)"
LOG_FILE="$(mktemp)"
trap 'rm -rf "$CRED_DIR" "$LOG_FILE"' EXIT

# Expand the committed org.yaml with live repo state and the per-repo baseline.
# On apply, also remove drifted outside collaborators. sync-repos.py uses
# GITHUB_TOKEN from the environment.
SYNC_ARGS=(--output org-expanded.yaml)
if [[ $MODE == "apply" ]]; then
  SYNC_ARGS+=(--remove-outside-collaborators)
fi
uv run scripts/sync-repos.py "${SYNC_ARGS[@]}"

# Pick the peribolos auth mode. Native App auth avoids the /user 403 that
# installation tokens hit; fall back to a PAT for local use.
PERIBOLOS_AUTH_ARGS=()
DOCKER_AUTH_VOL=()
if [[ -n ${APP_ID:-} && -n ${APP_PRIVATE_KEY:-} ]]; then
  printf '%s' "$APP_PRIVATE_KEY" >"$CRED_DIR/app.pem"
  chmod 600 "$CRED_DIR/app.pem"
  PERIBOLOS_AUTH_ARGS=(
    "--github-app-id=$APP_ID"
    --github-app-private-key-path=/etc/github/app.pem
  )
  DOCKER_AUTH_VOL=(-v "$CRED_DIR/app.pem:/etc/github/app.pem:ro")
else
  printf '%s' "$GITHUB_TOKEN" >"$CRED_DIR/token"
  chmod 600 "$CRED_DIR/token"
  PERIBOLOS_AUTH_ARGS=(--github-token-path=/etc/github/token)
  DOCKER_AUTH_VOL=(-v "$CRED_DIR/token:/etc/github/token:ro")
fi

# Peribolos arguments. Add --confirm only for apply mode.
# --min-admins=2 matches our intentional two-admin posture (akaszynski,
# banesullivan). Peribolos's default minimum is 5 as a lockout safeguard;
# we accept the tighter floor knowing admin membership is PR-gated here.
PERIBOLOS_ARGS=(
  --config-path=org-expanded.yaml
  --min-admins=2
  --fix-org
  --fix-org-members
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
  "${DOCKER_AUTH_VOL[@]}" \
  -w /workspace \
  "$PERIBOLOS_IMAGE" "${PERIBOLOS_AUTH_ARGS[@]}" "${PERIBOLOS_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
EXIT=${PIPESTATUS[0]}
set -e

# Post a readable dry-run / apply summary to the GitHub Actions job summary.
# Peribolos logs in JSON; extract level + msg per line for humans.
if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
  {
    printf '## peribolos %s\n\n' "$MODE"
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
  } >>"$GITHUB_STEP_SUMMARY"
fi

exit "$EXIT"
