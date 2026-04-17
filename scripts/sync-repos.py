#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "ruamel.yaml>=0.18",
# ]
# ///
"""Expand org.yaml with live-derived team repo access and baseline settings.

The committed ``org.yaml`` is intentionally small:

- The broad-access teams (``collaborators``, ``developers``, ``maintainers``,
  ``admin``) list no repos. Their coverage is every public repo in the org,
  so pinning the list by hand every time a repo is added or archived is
  exactly the kind of cruft we want to avoid.
- Per-repo settings (merge rules, has_wiki, etc.) are driven by REPO_BASELINE
  below, applied to every public non-archived repo. The top-level ``repos:``
  section only lists deliberate per-repo deviations from that baseline. If a
  repo needs a non-baseline setting, add an entry for that repo with the
  overriding fields.

This script queries GitHub for the current repo state, then produces an
*expanded* ``org.yaml`` that peribolos can consume:

- ``developers`` / ``maintainers`` / ``admin`` / ``collaborators`` teams get
  their ``repos:`` lists filled in from the live repo list.
- Every public non-archived repo gets a ``repos:`` entry populated with
  REPO_BASELINE. Any fields present in the committed ``org.yaml`` override
  the baseline key-by-key.
- It then checks membership consistency (no orphan members, no phantom team
  users) and fails fast if the committed config is broken.

Usage::

    GITHUB_TOKEN=... uv run scripts/sync-repos.py                       # → stdout
    GITHUB_TOKEN=... uv run scripts/sync-repos.py -o org-expanded.yaml
    GITHUB_TOKEN=... uv run scripts/sync-repos.py --check                # exit 1 if dead repos referenced
    GITHUB_TOKEN=... uv run scripts/sync-repos.py --remove-outside-collaborators
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

ORG = "pyvista"
ORG_YAML = Path(__file__).resolve().parent.parent / "org.yaml"

# Team → (permission, include archived, include admin repo). These teams are
# broad-access teams; their repo lists are fully auto-generated from the live
# org state. Narrower-scope teams (pyvistaqt-admin, dev-tools-admin, robots)
# keep their repo lists in the committed org.yaml.
AUTO_TEAMS: dict[str, tuple[str, bool, bool]] = {
    "collaborators": ("triage", False, False),
    "developers": ("write", False, False),
    "maintainers": ("maintain", False, False),
    "admin": ("admin", True, True),
}
ADMIN_REPO = "admin"

# Baseline settings applied to every public non-archived repo. Anything in the
# committed org.yaml repos: section overrides these key-by-key. Keeps the
# committed config focused on real per-repo deviations.
REPO_BASELINE: dict[str, object] = {
    "has_wiki": False,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
    "allow_squash_merge": True,
    "squash_merge_commit_title": "PR_TITLE",
    "squash_merge_commit_message": "PR_BODY",
    "allow_auto_merge": True,
    "allow_update_branch": True,
    "delete_branch_on_merge": True,
}


def _gh_request(url: str, token: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pyvista-admin-sync",
        },
    )


def gh(url: str, token: str) -> list[dict[str, Any]]:
    """Paginated GET against the GitHub REST API."""
    items: list[dict[str, Any]] = []
    while url:
        with urllib.request.urlopen(_gh_request(url, token)) as resp:
            payload = json.loads(resp.read())
            link = resp.headers.get("Link", "")
        items.extend(payload)
        url = _next_link(link)
    return items


def gh_delete(url: str, token: str) -> None:
    urllib.request.urlopen(_gh_request(url, token, method="DELETE")).read()


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        if 'rel="next"' in section[1]:
            return section[0].strip().strip("<>")
    return None


def find_team(teams: dict, name: str) -> dict | None:
    """Walk the nested teams tree and return the team with this name."""
    for team_name, team in teams.items():
        if team_name == name:
            return team
        nested = team.get("teams")
        if nested:
            found = find_team(nested, name)
            if found is not None:
                return found
    return None


def repo_list(
    repos: list[dict], permission: str, include_archived: bool, include_admin: bool
) -> dict[str, str]:
    names = []
    for r in repos:
        if r["private"]:
            continue
        if r["archived"] and not include_archived:
            continue
        if r["name"] == ADMIN_REPO and not include_admin:
            continue
        names.append(r["name"])
    return {name: permission for name in sorted(names)}


def expand(data: dict, live_repos: list[dict]) -> None:
    teams = data.get("teams", {})
    for team_name, (perm, archived, admin_repo) in AUTO_TEAMS.items():
        team = find_team(teams, team_name)
        if team is None:
            print(f"WARN: team '{team_name}' not found in org.yaml", file=sys.stderr)
            continue
        team["repos"] = repo_list(live_repos, perm, archived, admin_repo)


def apply_repo_baseline(data: dict, live_repos: list[dict]) -> None:
    """Merge REPO_BASELINE into every public non-archived repo entry.

    For each live public non-archived repo, ensure it has an entry in
    ``data['repos']`` with the baseline settings. Any keys already present in
    a committed entry override the baseline. Archived repos are left alone
    entirely. They are read-only on GitHub and do not need baseline merge
    rules or wiki settings applied.
    """
    repos_section = data.setdefault("repos", {})
    for r in live_repos:
        if r["private"] or r["archived"]:
            continue
        name = r["name"]
        custom = repos_section.get(name) or {}
        merged = {**REPO_BASELINE, **custom}
        repos_section[name] = merged


def prune_dead_repos(data: dict, live_names: set[str]) -> list[str]:
    """Remove entries from the top-level ``repos:`` section that no longer exist."""
    repos = data.get("repos", {})
    dead = [name for name in list(repos) if name not in live_names]
    for name in dead:
        del repos[name]
    return dead


def check_dead_repos(data: dict, live_names: set[str]) -> list[str]:
    """Return names in org.yaml's top-level repos section that no longer exist."""
    configured = set(data.get("repos", {}).keys())
    return sorted(configured - live_names)


def _walk_team_users(teams: dict, out: set[str]) -> None:
    for _, t in teams.items():
        out.update(t.get("maintainers") or [])
        out.update(t.get("members") or [])
        if t.get("teams"):
            _walk_team_users(t["teams"], out)


def check_membership_consistency(data: dict) -> tuple[list[str], list[str]]:
    """Find orphan members and phantom team users.

    Returns (orphans, phantoms):
      - orphans: org members with no team placement (would end up with zero
        repo access since default_repository_permission is none).
      - phantoms: users listed in a team but not in the top-level members list
        (peribolos removes them from the org, then fails to add them back to
        the team because they're no longer members).
    """
    members = set(data.get("members") or [])
    admins = set(data.get("admins") or [])
    team_users: set[str] = set()
    _walk_team_users(data.get("teams") or {}, team_users)

    orphans = sorted(members - team_users - admins, key=str.lower)
    phantoms = sorted(team_users - members - admins, key=str.lower)
    return orphans, phantoms


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-o", "--output", type=Path, help="Write expanded config here (default: stdout)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any dead repos are referenced in org.yaml",
    )
    parser.add_argument(
        "--remove-outside-collaborators",
        action="store_true",
        help=(
            "After expanding config, remove every outside collaborator from the "
            "org. No org.yaml entry lists them, so they're always drift."
        ),
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is required")

    live_repos = gh(
        f"https://api.github.com/orgs/{ORG}/repos?per_page=100&type=all", token
    )
    live_names = {r["name"] for r in live_repos}

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=0, offset=0)
    yaml.width = 4096
    with ORG_YAML.open() as f:
        data = yaml.load(f)

    dead = check_dead_repos(data, live_names)
    if dead:
        for name in dead:
            print(
                f"WARN: repo '{name}' in org.yaml no longer exists in the org",
                file=sys.stderr,
            )
        if args.check:
            return 1

    orphans, phantoms = check_membership_consistency(data)
    if orphans or phantoms:
        if orphans:
            print(
                "\nERROR: org members with no team placement:",
                file=sys.stderr,
            )
            for u in orphans:
                print(f"  {u}", file=sys.stderr)
            print(
                "Assign each to a team (collaborators, developers, maintainers, "
                "or a specialized team) or remove from the top-level members list.",
                file=sys.stderr,
            )
        if phantoms:
            print(
                "\nERROR: users listed in teams but not in the top-level members list:",
                file=sys.stderr,
            )
            for u in phantoms:
                print(f"  {u}", file=sys.stderr)
            print(
                "Add each to the top-level members list, or remove from the team.",
                file=sys.stderr,
            )
        return 1

    # Prune dead entries from the expanded output so peribolos doesn't error
    # on them. The committed org.yaml may still reference them; that's a
    # follow-up cleanup in a PR.
    prune_dead_repos(data, live_names)
    apply_repo_baseline(data, live_repos)
    expand(data, live_repos)

    if args.output:
        with args.output.open("w") as f:
            yaml.dump(data, f)
    else:
        yaml.dump(data, sys.stdout)

    # Outside collaborators are non-org-members with per-repo access. No
    # org.yaml entry grants this, so every one we see is drift. Warn on PR
    # dry-run (so reviewers notice), remove on apply.
    outside = gh(
        f"https://api.github.com/orgs/{ORG}/outside_collaborators?per_page=100", token
    )
    if outside:
        print(
            f"\nFound {len(outside)} outside collaborator(s) on the {ORG} org:",
            file=sys.stderr,
        )
        for c in outside:
            print(f"  {c['login']}", file=sys.stderr)
        if args.remove_outside_collaborators:
            for c in outside:
                gh_delete(
                    f"https://api.github.com/orgs/{ORG}/outside_collaborators/{c['login']}",
                    token,
                )
                print(f"Removed outside collaborator: {c['login']}", file=sys.stderr)
        else:
            print(
                "Pass --remove-outside-collaborators to delete them.",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
