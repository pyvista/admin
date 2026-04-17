#!/usr/bin/env bash
# Run peribolos against the pyvista org.
#
# Called from both GitHub Actions workflows and the Makefile. Takes a single
# argument: ``dry-run`` (default) or ``apply``. Reads GITHUB_TOKEN from the
# environment and writes it to a temporary file that peribolos can read.
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

PERIBOLOS_IMAGE="us-docker.pkg.dev/k8s-infra-prow/images/peribolos:latest"

if [[ -z ${GITHUB_TOKEN:-} ]]; then
  echo "ERROR: GITHUB_TOKEN environment variable is required." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker must be installed and on PATH." >&2
  exit 1
fi

# Write the token to a temp file; peribolos reads credentials from disk.
TOKEN_DIR="$(mktemp -d)"
LOG_FILE="$(mktemp)"
trap 'rm -rf "$TOKEN_DIR" "$LOG_FILE"' EXIT
printf '%s' "$GITHUB_TOKEN" >"$TOKEN_DIR/token"
chmod 600 "$TOKEN_DIR/token"

# Expand the committed org.yaml with live repo state and the per-repo baseline.
# On apply, also remove drifted outside collaborators.
SYNC_ARGS=(--output org-expanded.yaml)
if [[ $MODE == "apply" ]]; then
  SYNC_ARGS+=(--remove-outside-collaborators)
fi
uv run scripts/sync-repos.py "${SYNC_ARGS[@]}"

# Peribolos arguments. Add --confirm only for apply mode.
PERIBOLOS_ARGS=(
  --config-path=org-expanded.yaml
  --github-token-path=/etc/github/token
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
  -v "$TOKEN_DIR/token:/etc/github/token:ro" \
  -w /workspace \
  "$PERIBOLOS_IMAGE" "${PERIBOLOS_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
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
