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
  the baseline key-by-key. Peribolos only reads that section when
  ``scripts/run-peribolos.sh`` passes ``--fix-repos``, and even then it
  ignores the keys listed in PERIBOLOS_UNSUPPORTED.
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
# org state. Narrower-scope teams (pyvistaqt-admin, robots)
# keep their repo lists in the committed org.yaml.
#
# developers include the admin repo so anyone with write access to pyvista
# repos can push branches here and open PRs with a working dry-run. The admin
# team still holds the admin grant (enforced via CODEOWNERS for merges).
AUTO_TEAMS: dict[str, tuple[str, bool, bool]] = {
    "collaborators": ("triage", False, False),
    "developers": ("write", False, True),
    "maintainers": ("maintain", False, False),
    "admin": ("admin", True, True),
}
ADMIN_REPO = "admin"

# Baseline settings applied to every public non-archived repo. Anything in the
# committed org.yaml repos: section overrides these key-by-key. Keeps the
# committed config focused on real per-repo deviations.
#
# Peribolos applies these unevenly: has_wiki and the three allow_*_merge keys
# drive a change on their own, the two squash_merge_commit_* keys ride along
# with one, and the rest are records of intent it cannot act on at all. See
# PERIBOLOS_UNSUPPORTED and PERIBOLOS_PASSENGER_ONLY below.
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

# Keys peribolos has no config field for. Its repo model (org.Repo in
# kubernetes-sigs/prow, pkg/config/org/org.go) does not declare them, and
# config parsing is non-strict (yaml.Unmarshal, not UnmarshalStrict), so an
# unknown key produces no error at all: it is read and thrown away.
#
# These stay in REPO_BASELINE because they are the org's recorded intent and
# other tooling reads them. Set them at repo creation time or fix them by hand.
# warn_unsupported() below prints them on every run so the gap stays visible
# instead of looking like it is handled.
PERIBOLOS_UNSUPPORTED: frozenset[str] = frozenset(
    {
        "allow_auto_merge",
        "allow_update_branch",
        "delete_branch_on_merge",
        "web_commit_signoff_required",
    }
)

# Keys peribolos parses but only applies as a passenger on some other change.
#
# github.RepoRequest.Defined() decides whether peribolos sends the PATCH at
# all, and it checks eleven fields without including these two. A repo whose
# only deviation from the baseline is a squash_merge_commit_* value therefore
# produces a delta that reports itself undefined and is skipped. Let any other
# managed field drift on that same repo and the next apply sends both keys
# along in the same request.
#
# The practical effect is a split org: repos that needed some other fix get the
# squash format applied, repos that did not keep whatever they had, and each of
# those flips silently the first time anything else about them changes. Fixing
# the stragglers means changing them by hand, not editing this config.
PERIBOLOS_PASSENGER_ONLY: frozenset[str] = frozenset(
    {
        "squash_merge_commit_message",
        "squash_merge_commit_title",
    }
)


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
        # Merge auto-expanded repos with any explicit grants in org.yaml.
        # Explicit entries win so per-team overrides (e.g. developers getting
        # write on the admin repo) survive expansion.
        expanded = repo_list(live_repos, perm, archived, admin_repo)
        existing = team.get("repos") or {}
        team["repos"] = {**expanded, **existing}


def apply_repo_baseline(data: dict, live_repos: list[dict]) -> None:
    """Merge REPO_BASELINE into every public non-archived repo entry.

    For each live public non-archived repo, ensure it has an entry in
    ``data['repos']`` with the baseline settings. Any keys already present in
    a committed entry override the baseline. Archived repos are left alone
    entirely. GitHub rejects writes to an archived repo, and peribolos only
    skips one when the config says ``archived: true``, so an entry for an
    archived repo without that key makes every apply fail.

    Only live repos get an entry, which is half of the guard against
    peribolos creating repos under ``--fix-repos``. See
    :func:`prune_dead_repos` for the other half. Entries for archived repos
    are rejected outright by :func:`check_archived_repos`.
    """
    repos_section = data.setdefault("repos", {})
    for r in live_repos:
        if r["private"] or r["archived"]:
            continue
        name = r["name"]
        custom = repos_section.get(name) or {}
        merged = {**REPO_BASELINE, **custom}
        repos_section[name] = merged


def warn_unsupported(data: dict) -> tuple[list[str], list[str]]:
    """Report expanded ``repos:`` keys peribolos will not reliably apply.

    Two separate problems, so they print separately. Keys peribolos has no
    field for are dropped silently, because it parses config non-strictly.
    Keys it only applies as a passenger look enforced until a repo needs no
    other change. Saying both out loud keeps the config from reading as more
    authoritative than it is.
    """
    repos = (data.get("repos") or {}).values()
    dropped: set[str] = set()
    passenger: set[str] = set()
    for settings in repos:
        dropped.update(PERIBOLOS_UNSUPPORTED.intersection(settings or {}))
        passenger.update(PERIBOLOS_PASSENGER_ONLY.intersection(settings or {}))

    unsupported, passengers = sorted(dropped), sorted(passenger)
    if unsupported:
        print(
            "\nNOTE: peribolos has no config field for these repo settings and "
            "discards them:",
            file=sys.stderr,
        )
        for key in unsupported:
            print(f"  {key}", file=sys.stderr)
        print(
            "Set them when the repo is created, or fix them by hand. They stay "
            "in the config as a record of intent.",
            file=sys.stderr,
        )
    if passengers:
        print(
            "\nNOTE: peribolos applies these only when a repo already needs "
            "another change:",
            file=sys.stderr,
        )
        for key in passengers:
            print(f"  {key}", file=sys.stderr)
        print(
            "A repo that deviates on nothing else keeps its current value. "
            "Fix those by hand.",
            file=sys.stderr,
        )
    return unsupported, passengers


def prune_dead_repos(data: dict, live_names: set[str]) -> list[str]:
    """Remove entries from the top-level ``repos:`` section that no longer exist.

    Load-bearing under ``--fix-repos``: peribolos creates any repo named in
    ``repos:`` that is missing from GitHub. Together with
    :func:`apply_repo_baseline`, which only ever inserts names taken from the
    live repo list, this keeps the section a subset of what already exists, so
    a stale or mistyped entry gets dropped rather than turned into a new repo.
    The guard is those two properties, not the order they run in.
    """
    repos = data.get("repos", {})
    dead = [name for name in list(repos) if name not in live_names]
    for name in dead:
        del repos[name]
    return dead


def check_archived_repos(data: dict, live_repos: list[dict]) -> list[str]:
    """Return committed ``repos:`` entries for archived repos missing ``archived: true``.

    GitHub rejects writes to an archived repo. Peribolos only leaves one alone
    when the config says ``archived: true``; otherwise it builds a delta from
    the baseline and the PATCH fails, which breaks the daily apply until
    someone notices. ``apply_repo_baseline`` never adds archived repos, so the
    only way in is a hand-written entry. Catch it on the PR instead.
    """
    archived = {r["name"] for r in live_repos if r["archived"]}
    offenders = [
        name
        for name, settings in (data.get("repos") or {}).items()
        if name in archived and not (settings or {}).get("archived")
    ]
    return sorted(offenders)


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

    # Hard error, not a warning: peribolos would PATCH these and GitHub would
    # reject it, breaking every apply until someone edits org.yaml.
    archived = check_archived_repos(data, live_repos)
    if archived:
        print(
            "\nERROR: org.yaml has repos: entries for archived repos without "
            "`archived: true`:",
            file=sys.stderr,
        )
        for name in archived:
            print(f"  {name}", file=sys.stderr)
        print(
            "GitHub rejects writes to archived repos, so peribolos fails on "
            "every apply. Remove the entry, or add `archived: true` to it.",
            file=sys.stderr,
        )
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
    # follow-up cleanup in a PR. Under --fix-repos this ordering also keeps
    # peribolos from creating repos; see prune_dead_repos.
    prune_dead_repos(data, live_names)
    apply_repo_baseline(data, live_repos)
    expand(data, live_repos)
    warn_unsupported(data)

    # Peribolos expects config wrapped under orgs.<name>. The committed
    # org.yaml is stored flat for readability (matches what `peribolos --dump`
    # produces for a single org). Wrap it here for peribolos consumption.
    wrapped = {"orgs": {ORG: data}}

    if args.output:
        with args.output.open("w") as f:
            yaml.dump(wrapped, f)
    else:
        yaml.dump(wrapped, sys.stdout)

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
