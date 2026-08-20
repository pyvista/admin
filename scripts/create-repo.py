#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Create a repository in the pyvista org from a "New repository" issue form.

Driven by ``.github/workflows/new-repository.yml``. Runs in two modes:

    GITHUB_TOKEN=... uv run scripts/create-repo.py --check --body-file b.md --author user
    GITHUB_TOKEN=... uv run scripts/create-repo.py --body-file b.md --author user \
        --expect-name some-repo

``--check`` parses and validates the request and mutates nothing. It runs
*before* the approval gate so a malformed or unauthorized request fails without
paging an admin. The unguarded mode runs only after an admin approves the
``repo-creation`` environment deployment, and is the only mode that writes.

The request body is read from a file, never from the command line or a shell
interpolation. The workflow fetches it fresh from the API in both modes, rather
than reading the webhook payload, so what an admin approves is what gets built.
``--expect-name`` re-checks the name against what the pre-approval run saw and
refuses if the issue changed underneath the approval.

On a validation failure the human-readable reason is written to
``repo-request-error.md`` for the workflow to post back as an issue comment.
On success the summary goes to ``repo-created.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from repo_settings import NEW_REPO_SETTINGS

ORG = "pyvista"
API = "https://api.github.com"

ERROR_FILE = Path("repo-request-error.md")
SUCCESS_FILE = Path("repo-created.md")

# Section headings of the issue form, as GitHub renders them into the issue
# body. GitHub renders the field *label*, not its id, so these strings must
# stay in sync with the `label:` values in
# .github/ISSUE_TEMPLATE/new-repository.yml. Changing a label there without
# changing it here makes every request fail validation, loudly.
FIELD_NAME = "Repository name"
FIELD_DESCRIPTION = "Description"
FIELD_PURPOSE = "Purpose"
FIELD_RELEASE_ENV = "PyPI trusted publishing"
KNOWN_FIELDS = frozenset(
    {FIELD_NAME, FIELD_DESCRIPTION, FIELD_PURPOSE, FIELD_RELEASE_ENV}
)

# GitHub renders an untouched optional field as this literal.
NO_RESPONSE = "_No response_"

# Deliberately stricter than GitHub's own rule. Every repo in the org is
# lowercase-and-hyphens; allowing `MyRepo` would let a typo become permanent.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")

MAX_DESCRIPTION = 350


class RequestError(Exception):
    """A problem with the request that the requester can fix themselves."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx as an HTTPError instead of quietly following it.

    Two endpoints here answer with a redirect that means something specific:
    ``GET /orgs/{org}/members/{login}`` returns 302 to the public-members
    endpoint when the *caller* is not seen as an org member, and
    ``GET /repos/{org}/{name}`` returns 301 for a repo that has been renamed.
    Following either silently turns a meaningful answer into a wrong one.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def api(
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    allow_status: tuple[int, ...] = (),
) -> tuple[int, Any]:
    """Call the GitHub REST API.

    Returns ``(status, parsed_body_or_None)``. Statuses listed in
    ``allow_status`` are returned rather than raised, for the endpoints where a
    non-2xx is a meaningful answer instead of an error.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pyvista-admin-create-repo",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method, headers=headers
    )
    try:
        with _OPENER.open(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        if exc.code in allow_status:
            return exc.code, None
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} failed with {exc.code}: {detail}") from exc


def parse_issue_form(body: str) -> tuple[dict[str, str], set[str]]:
    """Split a rendered GitHub issue-form body into ``{heading: value}``.

    Issue forms render each field as a ``### <label>`` heading followed by the
    value. Unfilled optional fields render as ``_No response_``, normalized to
    an empty string here.

    The first occurrence of a heading wins and repeats are reported, because a
    free-text field can contain anything the requester types, including another
    ``### Repository name`` heading hidden inside an HTML comment. That renders
    invisibly on the issue an admin reads while still sitting in the body this
    parser sees. Silently letting a later section overwrite an earlier one would
    mean approving one name and creating another.
    """
    sections: dict[str, list[str]] = {}
    duplicates: set[str] = set()
    current: str | None = None
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            heading = line[4:].strip()
            if heading in sections:
                duplicates.add(heading)
                # Park the following lines so they cannot extend the original.
                current = None
                continue
            current = heading
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    out: dict[str, str] = {}
    for heading, lines in sections.items():
        value = "\n".join(lines).strip()
        out[heading] = "" if value == NO_RESPONSE else value
    return out, duplicates


def reject_duplicates(duplicates: set[str]) -> None:
    repeated = sorted(duplicates & KNOWN_FIELDS)
    if repeated:
        listed = ", ".join(f"`{name}`" for name in repeated)
        raise RequestError(
            f"The issue body repeats the {listed} heading. Only the form's own "
            f"headings may appear, once each. If you pasted a `### ` heading "
            f"into one of the text fields, remove it and try again."
        )


def require(fields: dict[str, str], heading: str) -> str:
    if heading not in fields:
        known = ", ".join(sorted(fields)) or "none"
        raise RequestError(
            f"The issue body has no `### {heading}` section, so this request "
            f"cannot be read.\n\nSections found: {known}.\n\nOpen a new issue "
            f"using the **New repository** form rather than editing the body "
            f"by hand."
        )
    value = fields[heading].strip()
    if not value:
        raise RequestError(f"`{heading}` is empty. Fill it in and re-apply the label.")
    return value


def validate_name(name: str) -> str:
    if "\n" in name:
        raise RequestError("`Repository name` must be a single line.")
    name = name.strip().strip("`")
    if not NAME_RE.match(name):
        raise RequestError(
            f"`{name}` is not a usable repository name. Use lowercase letters, "
            f"digits, `-`, `_` and `.`, starting with a letter or digit, at most "
            f"100 characters. Every repo in the org follows the lowercase "
            f"convention."
        )
    if name in {".", ".."} or name.endswith(".git"):
        raise RequestError(f"`{name}` is reserved by git and cannot be used.")
    return name


def validate_description(description: str) -> str:
    if "\n" in description:
        raise RequestError(
            "`Description` must be a single line. It becomes the repository's "
            "one-line GitHub description. Put the longer explanation in "
            "`Purpose`."
        )
    if len(description) > MAX_DESCRIPTION:
        raise RequestError(
            f"`Description` is {len(description)} characters; the limit is "
            f"{MAX_DESCRIPTION}."
        )
    return description


def wants_release_env(fields: dict[str, str]) -> bool:
    """True if the requester ticked the trusted-publishing checkbox.

    The section holds exactly one checkbox, so any ticked box in it means yes.
    Its absence is an error rather than a False: a missing section means the
    form's labels and this script have drifted apart, and quietly answering
    "no" would skip the `release` environment for someone who asked for it and
    only surface at their first release.
    """
    if FIELD_RELEASE_ENV not in fields:
        raise RequestError(
            f"The issue body has no `### {FIELD_RELEASE_ENV}` section. Open a "
            f"new issue using the **New repository** form."
        )
    section = fields[FIELD_RELEASE_ENV]
    return any(line.strip().lower().startswith("- [x]") for line in section.split("\n"))


def check_membership(login: str, token: str) -> None:
    status, _ = api(
        "GET", f"/orgs/{ORG}/members/{login}", token, allow_status=(302, 404)
    )
    if status == 302:
        raise RuntimeError(
            f"GET /orgs/{ORG}/members/{login} redirected, which means this token "
            f"is not treated as an org member and can only see public members. "
            f"The App installation needs the organization Members: read "
            f"permission."
        )
    if status != 204:
        raise RequestError(
            f"@{login} is not a member of the {ORG} org, so this request cannot "
            f"be automated. Org membership is the prerequisite; see the "
            f"[README](https://github.com/{ORG}/admin#join-the-pyvista-org-as-a-member)."
        )


def check_available(name: str, token: str, *, resume_partial: bool = False) -> bool:
    """Check the name is free. Returns True if the repo already exists.

    ``resume_partial`` allows an existing but never-pushed repo through, so a
    creation that died between ``POST`` and ``PATCH`` can be retried by
    re-approving the deployment. Without it the retry would abort here and tell
    the requester to pick a different name for a repo the automation itself had
    just made. A repo with any pushed content is never resumable.
    """
    status, repo = api("GET", f"/repos/{ORG}/{name}", token, allow_status=(301, 404))
    if status == 404:
        return False
    if status == 301:
        raise RequestError(
            f"`{name}` is the former name of a repo that was renamed. Reusing it "
            f"would break the redirect for the old URL. Pick a different name."
        )
    if resume_partial and repo and not repo.get("pushed_at") and not repo.get("size"):
        return True
    raise RequestError(
        f"[`{ORG}/{name}`](https://github.com/{ORG}/{name}) already exists. Pick "
        f"a different name."
    )


def create_repo(name: str, description: str, token: str, *, exists: bool) -> str:
    """Create the repo if needed, then apply the org baseline settings.

    Creation and settings are two calls on purpose: ``POST /orgs/{org}/repos``
    does not accept every field that ``PATCH /repos/{owner}/{repo}`` does, so
    the baseline is applied once, in one place, rather than split across both.
    Both halves are safe to repeat, which is what makes a retry after a partial
    failure work.

    ``auto_init`` is false. The requester almost always has an existing local
    repo to push, and an empty repo gives them GitHub's "push an existing
    repository" instructions instead of a README they have to merge around.
    """
    repo: dict[str, Any] = {}
    if not exists:
        _, created = api(
            "POST",
            f"/orgs/{ORG}/repos",
            token,
            body={
                "name": name,
                "description": description,
                "private": False,
                "auto_init": False,
                "has_issues": True,
            },
        )
        repo = created or {}
    api("PATCH", f"/repos/{ORG}/{name}", token, body=dict(NEW_REPO_SETTINGS))
    return repo.get("html_url", f"https://github.com/{ORG}/{name}")


def create_release_environment(name: str, token: str) -> None:
    """Add the ``release`` environment used by PyPI trusted publishing.

    No protection rules. The environment only has to exist and be named
    ``release`` for the OIDC trusted publisher on PyPI to match. The call is a
    PUT, so repeating it is a no-op.
    """
    api("PUT", f"/repos/{ORG}/{name}/environments/release", token, body={})


def write_output(**pairs: str) -> None:
    """Append key=value pairs to $GITHUB_OUTPUT.

    Every value written here has already passed validation as a single-line
    string, so no heredoc delimiter is needed.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in pairs.items():
            fh.write(f"{key}={value}\n")


def success_body(name: str, url: str, release_env: bool) -> str:
    lines = [
        f"Created **[{ORG}/{name}]({url})**.",
        "",
        "Applied the org baseline: squash-only merges, PR title as the commit "
        "subject, auto-merge on, head branch deleted on merge, no wiki, no "
        "projects.",
    ]
    if release_env:
        lines += [
            "",
            "Added the `release` environment. Configure the trusted publisher on "
            "PyPI to point at this repository, the workflow filename that "
            "publishes, and the `release` environment.",
        ]
    lines += [
        "",
        "Team access (`collaborators` triage, `developers` write, `maintainers` "
        "maintain, `admin` admin) is granted by the `apply` workflow, triggered "
        "next.",
        "",
        "The repository is empty. Push your existing history to it, or "
        "follow [pyvista/pyvista-manifold](https://github.com/pyvista/pyvista-manifold) "
        "for the standard package layout.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the request without creating anything.",
    )
    parser.add_argument(
        "--body-file",
        type=Path,
        required=True,
        help="File holding the rendered issue body.",
    )
    parser.add_argument(
        "--author", required=True, help="Login of the user who opened the issue."
    )
    parser.add_argument(
        "--expect-name",
        help=(
            "Repository name the pre-approval run validated. Refuse if the "
            "request no longer asks for this name."
        ),
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is required")

    try:
        fields, duplicates = parse_issue_form(
            args.body_file.read_text(encoding="utf-8")
        )
        reject_duplicates(duplicates)
        name = validate_name(require(fields, FIELD_NAME))
        description = validate_description(require(fields, FIELD_DESCRIPTION))
        require(fields, FIELD_PURPOSE)
        release_env = wants_release_env(fields)

        if args.expect_name and name != args.expect_name:
            raise RequestError(
                f"This request asked for `{args.expect_name}` when it was "
                f"approved and now asks for `{name}`. Nothing was created. "
                f"Re-apply the `new-repository` label to start a fresh review "
                f"of the current request."
            )

        check_membership(args.author, token)
        exists = check_available(name, token, resume_partial=not args.check)
    except RequestError as exc:
        ERROR_FILE.write_text(
            f"Could not act on this request.\n\n{exc}\n\n"
            f"Edit the issue, then remove and re-add the `new-repository` label "
            f"to try again.\n",
            encoding="utf-8",
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    write_output(repo_name=name, release_env=str(release_env).lower())

    if args.check:
        print(f"Request is valid: {ORG}/{name} (release environment: {release_env})")
        return 0

    if exists:
        print(f"{ORG}/{name} already exists and is empty; re-applying settings")
    url = create_repo(name, description, token, exists=exists)
    print(f"Created {url}")

    if release_env:
        create_release_environment(name, token)
        print("Created the `release` environment")

    SUCCESS_FILE.write_text(success_body(name, url, release_env), encoding="utf-8")
    write_output(repo_url=url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
