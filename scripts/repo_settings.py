"""Baseline repository settings for the pyvista org.

Single source of truth for "how should a pyvista repo be configured", shared by
the two things that care:

- ``scripts/sync-repos.py`` merges REPO_BASELINE into every public non-archived
  repo entry of the expanded org.yaml, for peribolos to reconcile.
- ``scripts/create-repo.py`` applies NEW_REPO_SETTINGS through the REST API when
  a new repo is created, so it starts out matching the baseline instead of
  waiting on a reconcile.

The convention: squash-only merges, PR title as the commit subject, auto-merge
on, head branch deleted on merge, no wiki, no per-repo projects.
"""

from __future__ import annotations

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
# The three that REPO_BASELINE sets stay there because they are the org's
# recorded intent and other tooling reads them. web_commit_signoff_required is
# not in the baseline at all; it is listed here because org.yaml sets it by
# hand on the admin repo, where it is equally inert. Set any of them at repo
# creation time or fix them by hand. warn_unsupported() below prints whichever
# ones appear in the expanded config on every run, so the gap stays visible
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

# What a brand-new repo gets. The baseline plus per-repo projects off.
#
# has_projects is deliberately not in REPO_BASELINE: the org already sets
# has_repository_projects: false org-wide, so adding it there would widen the
# diff of the first --fix-repos run across every existing repo for no
# behavioural gain. Setting it explicitly on a new repo costs nothing.
NEW_REPO_SETTINGS: dict[str, object] = {
    **REPO_BASELINE,
    "has_projects": False,
}
