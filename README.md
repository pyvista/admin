# pyvista/admin

Declarative config for the PyVista GitHub org. Membership, teams, repository access, and security policies live here as YAML configurations.

If you contribute to PyVista projects and want to understand how access works, propose a change, or get promoted, this is the place.

## Why this exists

PyVista runs on a lot of repositories and a growing community. Managing that in admins' DMs does not scale and leaves no audit trail. Also its really hard to manage all of this through the GitHub UI and a lot of things were slipping through the cracks.

- **Democratized management.** Any contributor can open a PR proposing a change to how the org is run. Decisions happen here, in public.
- **Auditable governance.** Every change to team membership, repo access, or org settings ships as a commit. "Who added this person, and when?" always has an answer.
- **Tight security posture.** We're working to make sure we have a centralized place to know who has write and maintain access to various repositories (this was previously only visible to org admins)

## Team structure

GitHub nests teams parent-to-child. A member of a child team is automatically a member of the parent, and inherited permissions stack. We use that to build a promotion ladder and to stamp narrower roles onto it.

```
collaborators            triage on every public repo
└── kitware              + write on pyvista and pyvista-xarray (@Kitware folks)

developers               write on every public repo
├── maintainers          maintain on every public repo
│   ├── admin            admin on every repo, sole access to the admin repo
│   ├── ci-reviewers     mention target for CI review routing, no repo grants
│   └── leaders          @pyvista/leaders mention target, no repo grants
└── pyvistaqt-admin      + admin on the PyVistaQt stack (larsoner)

robots                   write on pyvista (pyvista-bot)
```

### How repo access is granted

The three broad-access teams (`collaborators`, `developers`, `maintainers`) cover every public repo in the org automatically. They have no `repos:` list in the committed `org.yaml`. Before every peribolos run, `scripts/sync-repos.py` queries the GitHub API for the live set of public repos and expands the config in memory. New repos are picked up by the next daily apply. Archived repos drop off collaborators/developers/maintainers and stay with admin.

The narrower-scope teams list their repos explicitly in `org.yaml`: `kitware`, `pyvistaqt-admin`, and `robots`. Those lists are hand-maintained because they express a choice about who should have access to what.

`ci-reviewers` and `leaders` have no repo grants at all. They exist as mention targets (`@pyvista/ci-reviewers`, `@pyvista/leaders`) and to make their members inherit maintainers access via team nesting.

### Roles at a glance

| Role                              | Access on every public repo        |
| --------------------------------- | ---------------------------------- |
| Org owner (in top-level `admins`) | Full org-owner powers              |
| `admin` team member               | `admin`                            |
| `maintainers` team member         | `maintain`                         |
| `developers` team member          | `write`                            |
| `collaborators` team member       | `triage`                           |
| Org member, no team               | none (enforced by CI orphan check) |
| Outside contributor               | fork, open PRs, open issues        |

The authoritative, current membership of each team is always in [`org.yaml`](./org.yaml). If this README disagrees with it, `org.yaml` wins, because `org.yaml` is what GitHub actually applies.

### Promotion ladder

A contributor starts as a **collaborator** on the repos they work on, which grants triage everywhere (label, close, assign issues and PRs). As their code reach grows they move to **developer**, picking up write on every public repo. When they are already doing releases, triage across multiple repos, and cross-repo review, they become a **maintainer**.

`admin`, `ci-reviewers`, and `leaders` are not tiers on the promotion ladder. Each is nested under `maintainers` for a specific purpose (org control, CI review routing, community leadership mention target). Someone can be added to any of them without being "promoted."

## How changes happen

Every change flows through a PR.

1. **Propose.** Open a PR editing `org.yaml`. Say what and why.
2. **Dry-run.** A GitHub Action runs [peribolos](https://github.com/kubernetes-sigs/prow/tree/main/cmd/peribolos) on the PR in read-only mode and reports the exact diff against live GitHub state.
3. **Review.** The `@pyvista/admin` team reviews. Permission changes and new members get discussed here, publicly.
4. **Merge.** Merging to `main` triggers apply. Peribolos reconciles GitHub to match within seconds.

The dry-run removes guesswork. The reviewer sees _"this PR adds @user to developers and grants her write on 49 repos"_ before the merge happens.

PRs opened from a fork are the one exception. GitHub withholds repository secrets from fork workflow runs, so the App token that the dry-run needs is unavailable and the dry-run step is skipped rather than failed. An admin reproduces the preview by pushing the same branch inside this repo, or locally with `make check`.

A daily cron also runs the apply workflow so that repos created, archived, or deleted on GitHub in between PRs flow into team access without anyone having to open a PR.

Dependabot keeps the pinned Action SHAs and the peribolos image digest current. Those PRs get auto-merge switched on when they open, which means they land on their own once an `@pyvista/admin` reviewer approves and the checks go green — the approval is still required, nothing merges unreviewed.

## Common tasks

Don't see yours? Open an issue.

### Join the pyvista org as a member

Org membership goes to regular contributors and active community participants. Open an issue titled _"Request org membership"_ and `@` mention the user (if not yourself) with a short note on what why. An admin will respond.

Org members appear in `org.yaml` under `members:`. Adding someone is a PR. Anyone added to `members:` **must** also be added to at least one team in the same PR. The sync script fails CI otherwise.

### Get access to a specific repository

Most access flows through the team ladder. If you are already in `collaborators`, `developers`, or `maintainers`, your access matches the table above. For access to a repo outside your team's scope (focused collaborator on one package, for example), open an issue describing what you need and why, and tag `@pyvista/admin`.

### Get promoted to the next team

Promotion is recognition of what you are already doing. There is no rigid checklist. The broad pattern:

- **To collaborators:** you've been contributing regularly to one area and a maintainer wants to streamline your access.
- **To developers:** you review and merge on multiple repos across the org.
- **To maintainers:** you're already doing releases, triage, and cross-repo coordination. Formalizing the role removes friction.

Anyone can self-nominate by opening a PR that moves their handle into the target team. You don't need to wait to be offered. The `@pyvista/admin` team reviews on the PR.

Nominating someone else works the same way. Open the PR, tag the person, discuss in the open.

### Add a new repository to the organization

Repo creation is restricted to org admins. Open an issue on this repo with the proposed name, what it's for, and why it belongs under `pyvista/`. An admin creates the repo on GitHub once agreed. No PR against `org.yaml` is needed for team access; within 24 hours the daily apply grants `collaborators` triage, `developers` write, `maintainers` maintain, and `admin` admin automatically. An admin can trigger the apply workflow manually to skip the wait.

For custom settings beyond the baseline (branch protection, non-default team grants, specific merge rules), add an entry to the top-level `repos:` section of `org.yaml` via PR. The org baseline covers squash-only merges and wikis off without a per-repo entry. Auto-merge, update-branch, delete-branch-on-merge and the squash commit message format are part of the baseline too, but peribolos applies them unreliably or not at all; see [How this works under the hood](#how-this-works-under-the-hood).

### Archive, transfer, or delete a repository

Higher-stakes. Open an issue with the proposal. Admins discuss, then make the change on GitHub. For deletions, the next sync-repos.py run prunes the dead repo from the expanded config automatically. If the repo had a custom entry in the top-level `repos:` section, follow up with a PR to remove it.

### Remove myself from the org or a team

Open a PR against `org.yaml` removing your handle from both `members:` and any team. No justification required; we will merge it. You can also leave through the GitHub UI at any time.

### Report a security vulnerability in a pyvista project

**Do not open a public issue.** Use the project's private vulnerability reporting channel (Security tab on the affected repo) or email `info@pyvista.org`. See `SECURITY.md` in the affected repo.

### Report a code-of-conduct concern

Email the maintainers per the procedure in the project's `CODE_OF_CONDUCT.md`. Reports are handled privately and are not discussed in PRs on this repo.

### Ask a general question

Most pyvista discussion happens in [pyvista/pyvista/discussions](https://github.com/pyvista/pyvista/discussions). For questions specifically about how the org is run, open an issue here.

## Operating principles

Rules we hold to so this setup stays useful instead of becoming its own friction.

- **Decisions about the org happen as PRs here.** DMs, Slack threads, and verbal agreements leave no trail.
- **Least privilege by default.** `default_repository_permission: none` on the org itself. People get the access their role requires, no more. Granting more later is easier than clawing it back.
- **No long-lived credentials.** Automation uses a GitHub App that mints short-lived installation tokens. No PATs in CI. Flag any you see; it's a bug.
- **One place per person.** A user is listed in the single deepest team that matches their role. Nested teams inherit up. The sync script fails CI if it finds an orphan (in `members:` with no team) or a phantom (in a team but not in `members:`).
- **No outside collaborators, ever.** GitHub allows repo admins to add non-org-members directly to a repo. The apply workflow removes any it finds on every run. If an external contributor needs ongoing access, invite them as an org member and place them in a team.
- **Admin repo reviewed by someone other than the author.** `CODEOWNERS` requires `@pyvista/admin` approval on every PR here, admins' own changes included.

## Local development

Peribolos itself only runs in CI. Locally, you can lint the config and check membership consistency against the live org before opening a PR.

```
make help            Show all available targets
make install         Install pre-commit git hooks
make lint            Run all pre-commit hooks on all files
make check           Validate org.yaml against live GitHub state (requires GITHUB_TOKEN)
make ci              lint + check combined
```

For `make check`, export `GITHUB_TOKEN` first. Any PAT with `read:org` or `gh auth token` works.

## How this works under the hood

The org is managed by [peribolos](https://github.com/kubernetes-sigs/prow/tree/main/cmd/peribolos), a tool from the Kubernetes project. It reads an expanded `org.yaml`, compares it against GitHub, and reconciles drift. Two layers sit on top:

**`scripts/sync-repos.py`** queries GitHub for the live repo list on every run, then:

1. Fills in `repos:` lists for the four broad-access teams (`collaborators` / `developers` / `maintainers` / `admin`) so we don't have to maintain them by hand.
2. Applies `REPO_BASELINE` (squash-only merges, wikis off, auto-merge on, delete head branch on merge) to every public non-archived repo. Per-repo entries in committed `org.yaml` override baseline key-by-key.
3. Runs a consistency audit: no orphan members, no phantom team users. Exits non-zero if the committed config is broken, failing CI before peribolos runs.
4. On apply, also removes any outside collaborators from the org. Since no `org.yaml` entry lists outside collaborators, every one found is drift.

**Repo settings are only partly enforceable.** Peribolos reads the top-level `repos:` section only when it gets `--fix-repos`, which `run-peribolos.sh` passes. Even then it splits the baseline three ways:

- **Enforced.** `has_wiki`, `allow_squash_merge`, `allow_merge_commit`, `allow_rebase_merge`. A deviation on any of these produces a change on its own.
- **Applied only as a passenger.** `squash_merge_commit_title` and `squash_merge_commit_message`. Peribolos parses them, but `RepoRequest.Defined()` upstream decides whether to send the update at all and does not look at either field. A repo that deviates on nothing else is skipped and keeps its current value; a repo that needs some other fix gets these rewritten alongside it.
- **Discarded.** `allow_auto_merge`, `allow_update_branch`, `delete_branch_on_merge`, `web_commit_signoff_required`. Peribolos has no config field for them and parses config non-strictly, so they are read and thrown away with no error.

The last two groups stay in `REPO_BASELINE` as the org's recorded intent. Set them when a repo is created, or fix them by hand. `sync-repos.py` prints both lists on every run and `run-peribolos.sh` folds that output into the job summary, so the gap is visible next to the diff rather than looking handled.

The practical consequence of the passenger case is a split org: squash commit format tracks the baseline on repos that needed some other change and lags on the rest, and a lagging repo flips the first time anything else about it drifts. Closing that gap means touching those repos by hand, not editing this config.

`--fix-repos` also means peribolos creates any repo named in `repos:` that is missing from GitHub. Two properties of `sync-repos.py` keep that from firing: it only ever inserts names taken from the live repo list, and it prunes entries whose repo is gone. The section it hands peribolos is therefore always a subset of what already exists, so a stale or mistyped entry is dropped rather than turned into a new repo.

**`scripts/run-peribolos.sh`** is the single entry point for running peribolos. Both GitHub Actions workflows (`dry-run.yml`, `apply.yml`) call it. It runs the sync script, writes App credentials to a temp file, and invokes the peribolos docker image against the expanded config, then posts a readable summary to the Actions job summary so reviewers can see what would change.

**Authentication** is via a GitHub App called `peribolos-admin`, installed on the pyvista org and scoped to the permissions it needs (team and repository administration, member management). The App's private key is stored as a repo secret and rotated on a schedule. It is the only long-lived credential in the system. It is stored twice, as an Actions secret and as a Dependabot secret, because GitHub hands Dependabot-triggered runs the Dependabot store instead of the Actions one; a rotation that updates only the Actions copy breaks the dry-run on every Dependabot PR.

**Apply is branch-gated.** The apply workflow runs only from `main`. Two independent checks enforce this:

1. The apply job is bound to a `production` GitHub Environment (repo **Settings → Environments → production**) whose deployment branch rule is set to `main`. GitHub refuses to start the job on any other ref, including a `workflow_dispatch` triggered from a branch.
2. `scripts/run-peribolos.sh` refuses apply mode in CI when `GITHUB_REF` is not `refs/heads/main`.

Together these make it impossible for a PR to mutate the live org, even if someone pushes a PR that swaps the dry-run command for apply or rewrites the workflow. The dry-run workflow can still read org state through the App token on PRs, which is required for the preview diff.

**Pre-commit** enforces code quality and security hygiene: [zizmor](https://github.com/zizmorcore/zizmor) for GitHub Actions security, [gitleaks](https://github.com/gitleaks/gitleaks) for secret leaks, [check-jsonschema](https://github.com/python-jsonschema/check-jsonschema) for workflow schema, ruff for the Python script, prettier for YAML/Markdown, codespell for typos.

---

Questions, suggestions, or disagreements? Please open an issue here.
