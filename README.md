# sentry-gitea

[![CI](https://github.com/ADCDS/sentry-gitea/actions/workflows/ci.yml/badge.svg)](https://github.com/ADCDS/sentry-gitea/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A [Gitea](https://about.gitea.com/) integration for **self-hosted [Sentry](https://github.com/getsentry/self-hosted)**,
installed into the Sentry image with pip. You can create and link Gitea issues from Sentry issues and
open them automatically when alert rules fire. An organization integration based on OAuth adds
owner-scoped install and search, issue sync, commit tracking, stack-trace links and CODEOWNERS.

> **Status: alpha** (version `0.2.0`). The integration's Sentry-level tests run against **Sentry
> 26.10.0**; the end-to-end suite last ran against **self-hosted Sentry 26.3.1** (errors-only profile)
> and **Gitea 28.1.0**. Gitea forks such as Forgejo are untested. Expect breaking changes before 1.0.
> It is not on PyPI yet.
>
> **Sentry 26.10 removed the legacy plugin API**, so the project plugins load only on Sentry 26.3.1 and
> older. The organization integration works on both.

## Why

Sentry's built-in source-code and issue-tracking integrations cover GitHub, GitLab, Bitbucket, Azure
DevOps and Perforce, but not Gitea. We found no maintained Sentry↔Gitea integration for current Sentry
releases. This package fills that gap for self-hosted installs. It uses Sentry's existing extension
points (legacy plugin entry points, `SENTRY_DEFAULT_INTEGRATIONS`, URL configuration and taskworker
imports) and does not patch Sentry.

## Two ways to use it

The package has two modes: the **plugins** and the **integration**. You can use either one on its own
or both together. Their issue links are stored separately and are not migrated between them.

| | **Project plugins** | **Organization integration** |
|---|---|---|
| Scope | One Sentry project → one Gitea repository | One Sentry organization → one Gitea owner (user or org) per install, multiple installs allowed |
| Gitea credentials | Personal access token, per project | OAuth2 app (confidential client, S256 PKCE), one per Sentry deployment |
| Where to configure | Project Settings → Legacy Integrations | Organization Settings → Integrations |
| `sentry.conf.py` changes | Only for Gitea on a private address (`GITEA_PLUGIN_ALLOWED_BASE_URLS`) | Required |
| Docs | [docs/PLUGINS.md](docs/PLUGINS.md) | [docs/INTEGRATION.md](docs/INTEGRATION.md) |

**Project plugins.** Two legacy plugins are registered through Python entry points:

- **Gitea** (`gitea`) adds Create, Link and Unlink actions to the legacy issue sidebar, using a project
  token and repository. New issues get a rendered title and body, optional assignee and labels, and
  default labels. Missing labels are created on first use.
- **Gitea Alerts** (`gitea_alerts`) is an issue-alert action. On the first event it files a Gitea issue,
  labelled by default with `source:sentry`, `severity:<critical|high|medium|low>` and
  `class:<crash|other>`. Later events add a comment while the linked issue is open. When the linked issue is closed and
  the problem comes back (a Sentry regression after the close, or optionally any event an hour after
  it), a new issue that references the closed one is filed and linked. An optional advisory "where to look" hint is available.

**Organization integration.** A Gitea provider for Sentry's integrations directory:

- OAuth install and choice of owner (your namespace or an org you own). Create issues with a
  repository picker that is remembered per project, plus labels and assignee. Link issues by number or
  title search, with an optional backlink comment. Unlink issues.
- Opt-in two-way sync of status and assignee, Sentry notes → Gitea comments, a signed webhook per added
  repository, commit and pull-request import with bounded backfill, release commit ranges, stack-trace
  source links through code mappings, and CODEOWNERS import (email owners only).

## Feature matrix

| Feature | Plugins | Integration |
|---|---|---|
| Create a Gitea issue from a Sentry issue | Yes | Yes |
| Link an existing issue (optional backlink comment) | Yes, by number | Yes, search by number or title |
| Unlink (the Gitea issue is left untouched) | Yes | Yes |
| Repository / issue search | No (one fixed repository) | Yes, scoped to the installed owner |
| Auto-file issues from alert rules | Yes | No (use the plugin) |
| Comment on repeat events | Yes (open issues only) | No |
| Severity / class labels | Yes (alerts) | No |
| Status sync, Sentry → Gitea (resolve ↔ close) | No | Opt-in |
| Status sync, Gitea → Sentry (via webhook) | No | Opt-in, repository must be added |
| Assignee sync, both directions | No (assignee at creation only) | Opt-in, explicit email → username map |
| Sentry notes → Gitea comments (incl. edits) | No | Opt-in |
| Gitea comments → Sentry | No | **Not supported** |
| Repositories, push/PR webhooks, commits | No | Yes |
| Release commit ranges | No | Yes, up to 5,000 commits, errors beyond that |
| Stack-trace links to Gitea source | No | Yes, via code mappings |
| CODEOWNERS with email owners | No | Yes |
| CODEOWNERS with `@user` / `@team` owners | No | **Not supported** (file rejected) |
| Blame / commit-context suspect commits | No | Opt-in (`GITEA_BLAME_ENABLED`), `git blame` on local mirrors |
| Pull-request comment bot | No | **Not supported** |
| Alert-rule "create ticket" action | n/a | **Not supported** |
| Gitea name and icon in Sentry's UI | n/a | **Not supported** (blank name, generic icon) |
| Sentry SaaS, or control/cell (hybrid cloud) deployments | **Not supported** | **Not supported** |

### Limitations

- **Self-hosted Sentry in monolith mode only.** This package cannot be installed into Sentry SaaS.
- **Blame-based suspect commits need git and disk.** Gitea's REST API has no blame endpoint, so with
  `GITEA_BLAME_ENABLED` the integration keeps a bare git mirror of each added repository and runs
  `git blame` itself (see [Blame-based suspect commits](#blame-based-suspect-commits)). There is no
  PR-comment bot. Release-based suspect commits built from imported commit data have not been
  verified.
- **CODEOWNERS supports email owners only.** Sentry maps `@user`/`@team` names only for its built-in
  providers. Files with such owners are rejected with an explicit error instead of being partly
  applied.
- **Comments made in Gitea are not imported** into Sentry.
- **The Gitea provider shows up with a blank name and a generic icon** in parts of Sentry's
  prebuilt frontend, such as the issue sidebar. Those mappings are compiled into the frontend and a
  Python package cannot change them. Backend forms and APIs work normally.
- **One Gitea instance and OAuth app per Sentry deployment** for the integration. The plugins can point
  each project at any allowed Gitea URL. There is no PAT fallback for the integration, and no migration
  of links between the plugins and the integration.
- Gitea truncates push payloads to 5 commits, so the rest are backfilled from the API, up to 1,000 per
  push. When the bound is hit, the package logs `sentry-gitea: backfill incomplete`.

## Requirements

- A [self-hosted Sentry](https://develop.sentry.dev/self-hosted/) **26.10.0** installation (Docker Compose).
- **Gitea 28.1.0** with its built-in issue tracker enabled on the repositories you use.
- Network paths: Sentry web **and** worker containers → Gitea API. For the integration, users' browsers
  → Gitea (OAuth), and Gitea → Sentry for repository webhooks.

## Install (self-hosted Sentry)

### 1. Get a wheel

PyPI publication is planned but has not happened yet. Download the wheel attached to a
[GitHub release](https://github.com/ADCDS/sentry-gitea/releases) (once one is published), or build it
from source with [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/ADCDS/sentry-gitea.git
cd sentry-gitea
uv build --wheel          # writes dist/sentry_gitea-0.2.0-py3-none-any.whl
```

Copy the wheel into the self-hosted checkout's `sentry/` directory. That directory is the image build
context, and it is copied to `/usr/src/sentry` inside the image.

### 2. Install it in the image: `sentry/enhance-image.sh`

Create `sentry/enhance-image.sh` from `enhance-image.example.sh` if needed, keep any existing
customizations, make it executable, and add:

```sh
python -m pip install --no-deps --no-index --force-reinstall \
    /usr/src/sentry/sentry_gitea-0.2.0-py3-none-any.whl
```

The `--no-deps` flag is deliberate: the Sentry image already provides `requests` and `urllib3`, and
Sentry itself must never be installed with pip. You can also install from a tagged source archive. This
needs network access during the image build:
`python -m pip install --no-deps https://github.com/ADCDS/sentry-gitea/archive/refs/tags/<tag>.tar.gz`.

### 3. Configure Sentry: `sentry/sentry.conf.py`

The **plugins** register through entry points. They need one setting only if your Gitea has a private
or loopback address (see [Plugin base URLs](#security-notes)):

```python
# Gitea origins that plugin base URLs may use on private networks:
GITEA_PLUGIN_ALLOWED_BASE_URLS = ("http://gitea:3000",)
```

For the **organization integration**, append the following (see also
[`docker/sentry.conf.py.snippet`](docker/sentry.conf.py.snippet)). Write the values as literals:
self-hosted does not pass arbitrary environment variables into Sentry containers, and an
`os.environ["..."]` lookup that fails raises `KeyError` and keeps every Sentry service from starting.

```python
# --- sentry-gitea: organization integration ---
from datetime import timedelta

SENTRY_DEFAULT_INTEGRATIONS += ("sentry_gitea.integration.provider.GiteaIntegrationProvider",)
ROOT_URLCONF = "sentry_gitea.urls"  # adds /extensions/gitea/... routes, keeps all Sentry routes
SENTRY_API_PAGINATION_ALLOWLIST = set(SENTRY_API_PAGINATION_ALLOWLIST) | {"GiteaSearchEndpoint"}
TASKWORKER_IMPORTS = (*TASKWORKER_IMPORTS, "sentry_gitea.integration.tasks")
# Keeps OAuth refresh tokens of idle installations from expiring:
TASKWORKER_SCHEDULES["gitea-identity-refresh"] = {
    "task": "integrations:sentry_gitea.integration.refresh_identities",
    "schedule": timedelta(hours=12),
}

GITEA_BASE_URL = "http://gitea:3000"  # Gitea API as reached from Sentry containers
GITEA_PUBLIC_URL = "https://gitea.example"  # optional: Gitea URL users' browsers reach
GITEA_CLIENT_ID = "<oauth client id>"
GITEA_CLIENT_SECRET = "<oauth client secret>"
# GITEA_WEBHOOK_URL_PREFIX = "https://sentry.example"  # optional: how Gitea reaches Sentry
```

| Setting | Required | Meaning |
|---|---|---|
| `GITEA_BASE_URL` | yes | Server-to-server URL used for API calls and token exchange. A hosting subpath is allowed. Credentials, query strings and fragments are rejected. |
| `GITEA_PUBLIC_URL` | no | Browser-facing URL used for the OAuth authorize redirect and for every generated issue/source link. Defaults to `GITEA_BASE_URL`. |
| `GITEA_CLIENT_ID` / `GITEA_CLIENT_SECRET` | yes | The Gitea OAuth2 application (see below). Keep the secret out of version control. |
| `GITEA_WEBHOOK_URL_PREFIX` | no | Base URL **Gitea** uses to deliver webhooks to Sentry. Defaults to Sentry's `system.url-prefix`, which is the recommended target. |
| `TASKWORKER_IMPORTS` entry | yes | Registers the push backfill and identity refresh tasks with Sentry's taskworker. |
| `TASKWORKER_SCHEDULES["gitea-identity-refresh"]` | recommended | Every 12 hours, refreshes Gitea identities that were not refreshed for 7 days, so refresh tokens of idle installations do not expire. |
| `GITEA_BLAME_ENABLED` | no | `True` turns on blame-based suspect commits from local git mirrors. Default `False`. Needs `git` in the image (below). |
| `GITEA_BLAME_MIRROR_DIR` | no | Where the mirrors live. Default `/data/sentry-gitea/mirrors`, on self-hosted's `sentry-data` volume, which web, taskworker and consumers all mount. |
| `GITEA_PLUGIN_ALLOWED_BASE_URLS` | no | Plugins only. Origins (`scheme://host[:port]`) that a project's plugin base URL may use even when they resolve to private or loopback addresses. Defaults to the origins of `GITEA_BASE_URL` and `GITEA_PUBLIC_URL` when those are set, otherwise empty; setting it replaces that default. |

If a required setting is missing, Gitea still appears in the integrations directory, and starting setup
shows a configuration error. Self-hosted's `sentry/Dockerfile` copies the whole `sentry/` directory,
including `sentry.conf.py` and the client secret, into the locally built image, so treat that image as
sensitive.

### 4. Rebuild and restart

Rebuild the Sentry images and recreate **every** service that runs Sentry code (web, taskworker,
taskscheduler, consumers). Re-running `./install.sh` in the self-hosted checkout does this. Alert
delivery runs in taskworkers, so installing only into `web` is not enough. After a restart,
`GET <sentry>/extensions/gitea/ping/` answers with the package version when `ROOT_URLCONF` is set.

## Configure

### Project plugins

1. In Gitea, create a personal access token with the scopes **`read:user`, `read:repository`,
   `write:issue`**. The token's user also needs repository rights to create issues and labels: scopes
   alone do not grant them.
2. In Sentry, go to **Project Settings → Legacy Integrations → Gitea**, enable it, and set the base URL
   (as reached from Sentry containers), an optional **Gitea URL for links**, the token, the repository
   (`owner/name`) and optional default labels. Saving verifies the token and the repository. A base
   URL on a private or loopback address must be listed in `GITEA_PLUGIN_ALLOWED_BASE_URLS`.
3. Enable **Gitea Alerts** on the same project. It uses the Gitea plugin's credentials.
4. Create an **issue alert** whose action sends a notification through the legacy service
   **Gitea Alerts** (`gitea_alerts`). For repeat-event comments, see the trigger gotcha below.

Details, label colors, recovery semantics and logging: [docs/PLUGINS.md](docs/PLUGINS.md).

### Organization integration

1. In Gitea, open **Settings → Applications**, create an OAuth2 application as a **confidential client**,
   and set the redirect URI to `<Sentry URL>/extensions/gitea/setup/`. Keep the trailing slash, and
   include any subpath. The integration requests `read:user read:organization write:issue write:repository`.
2. Put the client ID and secret in `sentry.conf.py` (above), then rebuild and restart.
3. As an **organization owner**, go to **Organization Settings → Integrations → Gitea → Install**.
   Sentry 26.10's install dialog only knows Sentry's built-in providers, so the button opens this
   package's own page, `<Sentry URL>/extensions/gitea/install/`, in a new tab. If you belong to several
   organizations, choose one there. Authorize in Gitea, then pick your own namespace or a Gitea
   organization **you own**. Team admin is not enough. The tab closes itself when the installation is
   done; reload the integration's page in Sentry.
4. On any Sentry issue, use the Gitea issue actions to **Create** or **Link**.
5. Optional features:
   - **Repositories:** Sentry 26.10 has no Add Repository form. It imports every repository of the
     installed owner through its repository sync: in **Organization Settings → Repositories**, hover
     the Gitea owner's repository count and click **Sync now**, or wait for Sentry's daily sync.
     Repositories that disappear from the owner are disabled at the next sync. Each repository gets
     one webhook (push, issue, issue-assignment and pull-request events) at
     `<prefix>/extensions/gitea/webhook/<integration id>/<Gitea repository id>/`, signed with a secret
     derived for that repository, which needs **repository admin** rights. To add a single repository
     instead, use the API: `POST /api/0/organizations/<org>/repos/` with
     `provider=integrations:gitea`, `installation=<integration id>` and `identifier=owner/name`. Then
     check a delivery's response code in the repository's **Settings → Webhooks** page in Gitea.
     Deleting the repository in Sentry removes the hook.
   - **Sync:** in the integration's configuration, turn on any of `sync_status_forward`,
     `sync_status_reverse`, `sync_comments`, `sync_assignee_forward` and `sync_assignee_reverse`. All are
     off by default. Assignee sync uses `user_mappings`, a JSON object
     `{"<verified Sentry email>": "<gitea username>"}`. Inbound sync needs the repository webhook.
   - **Source links and CODEOWNERS:** create a code mapping for a Gitea repository. Stack frames then
     link to `<GITEA_PUBLIC_URL>/<owner>/<repo>/src/{branch|commit}/<ref>/<path>`. CODEOWNERS is read
     from `CODEOWNERS`, `.gitea/CODEOWNERS`, `.github/CODEOWNERS` or `docs/CODEOWNERS`.

Details: [docs/INTEGRATION.md](docs/INTEGRATION.md).

### Blame-based suspect commits

Sentry finds suspect commits by blaming the in-app stack frames of a new issue, but only for
providers on its built-in list. With `GITEA_BLAME_ENABLED = True`, Gitea joins that list, and the
integration answers blame requests from a bare git mirror per added repository.

1. **Install git in the image.** The Sentry image has no `git`. Add this line to
   `sentry/enhance-image.sh`, which self-hosted runs as root on Debian:

   ```sh
   apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
   ```

2. **Turn it on** in `sentry.conf.py` (`GITEA_BLAME_ENABLED = True`), rebuild, and recreate all
   services. Each repository needs a code mapping, as for stack-trace links.

How it behaves:

- **Disk.** Each mirror is a full bare clone with all branches and tags, at
  `<GITEA_BLAME_MIRROR_DIR>/<integration id>/<Gitea repository id>.git`. Plan for roughly the size
  of the repositories in Gitea. Deleting the repository in Sentry deletes its mirror, unless
  another organization still uses the same repository through the same installation.
- **Cloning.** Adding a repository, or the first blame for it, queues the
  `sentry_gitea.integration.mirror_repository` task, which may run for up to 25 minutes. Until the
  clone finishes, blame skips that repository, so the first new issues after you turn the feature
  on can miss a suspect commit.
- **Fetching.** A blame at a commit the mirror already has needs no network. A branch name, such as
  the code mapping's default branch, fetches at most once every 5 minutes per mirror, and always on
  the first blame after a push webhook for that repository.
- **Credentials.** Git authenticates with the installation's OAuth token in an
  `Authorization: Bearer` header taken from the environment. The token is never written to the
  mirror's configuration, and never appears in a process's arguments.
- **Trade-off.** For a project with a Gitea code mapping, Sentry 26.10 then uses blame only and
  no longer falls back to release-based suspect commits. Sentry caches which providers an
  organization has for up to 4 hours, so turning the feature on or off takes effect within that
  time.

## Security notes

- **Secrets at rest.** Plugin personal access tokens are stored as plaintext Sentry project options.
  The integration's OAuth tokens (in `Identity.data`) and its webhook key (in the integration's
  metadata) use Sentry's encrypted JSON fields, which store **plaintext unless** Sentry's
  `database.encryption.method` option is `fernet` and Fernet keys are configured
  (`DATABASE_ENCRYPTION_SETTINGS`); the default method is `plaintext`. Protect the Sentry database,
  its backups and administrative access accordingly.
- **Webhook secrets.** Each repository hook is signed with its own secret, derived from the
  installation's webhook key and the Gitea repository ID. Gitea shows a hook's secret to the
  repository's admins in its web UI. A leaked secret can only forge deliveries for that one
  repository: the endpoint rejects a payload whose repository does not match the URL.
- **Plugin base URLs.** A project admin chooses the plugin's base URL, and Sentry calls it from inside
  your network. Base URLs whose origin is in `GITEA_PLUGIN_ALLOWED_BASE_URLS` may resolve to private or
  loopback addresses. Every other base URL goes through Sentry's outbound HTTP guard, which blocks
  `SENTRY_DISALLOWED_IPS` (private, loopback and link-local ranges by default) and does not follow
  redirects. Changing a base URL requires entering the token again, and configuration errors report
  only a status or category, never Gitea's response body.
- **Event text is neutralized.** Values taken from events (title, culprit, release, environment,
  message) are rendered as inline code in Gitea issues and comments, so they cannot @mention Gitea
  users or create automatic links.
- **OAuth callback in logs.** The callback URL carries a single-use `code` and `state`. Reverse proxies
  record it in their access logs (self-hosted's nginx logs the full request line), and Sentry's
  `sentry.superuser` audit logger records it for superusers. The code is bound to the integration's
  PKCE verifier and cannot be replayed, but treat these logs as sensitive and install as an
  organization owner rather than a superuser.

Report vulnerabilities privately: [SECURITY.md](SECURITY.md).

## Operator gotchas (Sentry 26 / Gitea 28)

- **"The event occurs" must be the only trigger for repeat comments.** Sentry 26 runs issue alerts through
  its workflow engine. When the engine converts a rule, it silently drops "The event occurs" if any other
  trigger is present. So "A new issue is created" + "The event occurs" never fires on repeat events. To
  file an issue once, use "A new issue is created". To file and also comment on later events, use
  "The event occurs" alone and narrow it with filters. The rule's action interval (minimum 5 minutes) and
  the legacy plugin limit (10 notifications per project per minute) still apply.
- **Alerts lag on idle instances.** Post-processing waits on Snuba's commit log. On a nearly idle
  self-hosted instance, the alert for the newest event can run only when the next event arrives. Busy
  instances are not affected.
- **Install the integration as an org owner, not as a Sentry superuser** (see
  [Security notes](#security-notes)).
- **Internal vs. public Gitea URLs.** Gitea 28 defaults to `[server] PUBLIC_URL_DETECTION = auto`, so API
  `html_url` values can carry the internal hostname Sentry used (for example `http://gitea:3000`). This
  package ignores `html_url` and builds links from the plugin's **Gitea URL for links** or
  `GITEA_PUBLIC_URL`. Set these whenever the base URL is not reachable from browsers. Setting
  `PUBLIC_URL_DETECTION = never` makes Gitea's own URLs use `ROOT_URL`. Existing links keep their stored
  URL until you re-link them.
- **Token scopes are not permissions.** Plugin PAT scopes: `read:user`, `read:repository`, `write:issue`.
  The PAT's user must also be able to write issues and labels in the repository. For the integration,
  organization installs need Gitea org **owner**, and adding repositories (webhooks) needs repository
  **admin**.
- **Refresh tokens expire.** Gitea's `[oauth2] REFRESH_TOKEN_EXPIRATION_TIME` defaults to 730 hours
  (about 30 days). The integration refreshes an access token shortly before it expires, and the
  `gitea-identity-refresh` schedule keeps idle installations alive; without it, an installation
  unused for longer than that lifetime must be reinstalled. Keep the lifetime well above 7 days. Leave
  `[oauth2] INVALIDATE_REFRESH_TOKENS` at its default `false`: with `true`, installations authorized
  by the same Gitea user invalidate each other's refresh tokens.
- **Webhooks to a private Sentry address** are blocked by Gitea 28's egress policy. Prefer the default
  target, Sentry's public `system.url-prefix`. If Gitea can reach Sentry only over a private network,
  do **not** allow the whole `private` range: any Gitea user who can create a webhook could then reach
  every private service. Put Gitea and Sentry's `nginx` (or `web`) container on a dedicated Docker
  network that contains nothing else, allow exactly its subnet in Gitea's `app.ini`
  (`[security] ALLOWED_HOST_LIST = 172.30.0.0/24`, using your network's CIDR), and point
  `GITEA_WEBHOOK_URL_PREFIX` at Sentry's address on it. Gitea 28 needs an explicit IP or CIDR entry
  for private targets (a host name entry is not enough) and deprecates the older
  `[webhook] ALLOWED_HOST_LIST`.
- **Install the wheel into every image** used by web, taskworker, taskscheduler and consumers, and set
  the same `GITEA_*` settings for all of them.

## Upgrade

1. Read the release notes for changed settings.
2. Build or download the new wheel. In the self-hosted `sentry/` directory, delete the old
   `sentry_gitea-*.whl` files, copy in the new one and update its file name in
   `sentry/enhance-image.sh`.
3. Apply any `sentry.conf.py` changes (compare with
   [`docker/sentry.conf.py.snippet`](docker/sentry.conf.py.snippet)).
4. Rebuild the images and recreate **all** Sentry services, for example with `./install.sh`. Restarting
   only some of them leaves web and workers on different versions.
5. Check that `GET <sentry>/extensions/gitea/ping/` reports the new version.

If a release changes the webhook URL or signing, add each Gitea repository to Sentry again (the same
`POST /api/0/organizations/<org>/repos/` call): adding an existing repository repairs its hook.

## Uninstall

Remove things in this order, so that nothing is left pointing at code that is gone:

1. **Repositories.** Delete each Gitea repository in Sentry (**Organization Settings →
   Repositories**, or `DELETE /api/0/organizations/<org>/repos/<id>/`). Deletion is scheduled; when it
   runs, the repository's Gitea hook is removed. Afterwards, check each repository's **Settings →
   Webhooks** in Gitea and delete any hook that still points at `/extensions/gitea/webhook/`.
2. **Integration.** Uninstall every Gitea installation in **Organization Settings → Integrations →
   Gitea**.
3. **OAuth.** Each Gitea user who authorized Sentry revokes the grant in **Settings → Applications**.
   Then delete the OAuth2 application.
4. **Plugins.** Remove alert-rule actions that use Gitea Alerts. In each project's plugin settings,
   use **Reset Configuration** (this clears the stored token) and disable both plugins. Delete the
   personal access tokens in Gitea.
5. **Mirrors.** If you used `GITEA_BLAME_ENABLED`, delete `GITEA_BLAME_MIRROR_DIR`
   (`/data/sentry-gitea/mirrors` by default) from the `sentry-data` volume.
6. **Configuration.** Remove every sentry-gitea line from `sentry/sentry.conf.py` (`ROOT_URLCONF`,
   `SENTRY_DEFAULT_INTEGRATIONS`, the pagination allowlist, `TASKWORKER_IMPORTS`, the
   `TASKWORKER_SCHEDULES` entry and all `GITEA_*` settings) and the `pip install` line from
   `sentry/enhance-image.sh`. Delete the wheel from `sentry/`. Leaving these lines without the package
   breaks Sentry, because the URL configuration and the task module can no longer be imported.
7. **Rebuild** the images and recreate all Sentry services, for example with `./install.sh`.

## Documentation

| Document | Contents |
|---|---|
| [docs/PLUGINS.md](docs/PLUGINS.md) | Project plugins: install, fields, allowed base URLs, alert rules, classification, recovery, limits, logging |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | Organization integration: OAuth app, settings, install, issues, repositories and webhooks, sync, source links, failures |
| [docs/TESTING.md](docs/TESTING.md) | Test layers, the Docker harness and the end-to-end stack |

## Development and testing

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/) and GNU make. The Sentry-level and E2E
layers also need Docker with **Compose ≥ 2.32.2**.

```sh
uv sync            # create .venv with the dev tools
make unit          # host unit tests (never import Sentry)
make lint          # ruff check + ruff format --check
make sentry-tests  # tests/sentry inside ghcr.io/getsentry/sentry:26.10.0 with throwaway Postgres/Redis
```

| Layer | Command | Runs where | In CI |
|---|---|---|---|
| Unit | `make unit` | Host. Pure `api/` and `render` code, with HTTP mocked by `responses` | Yes |
| Harness | `uv run pytest tests/e2e -m "not e2e"` | Host. E2E helper and installer logic, no stack needed | Yes |
| Sentry-level | `make sentry-tests` | The pinned Sentry image plus disposable Postgres 14 and Redis 6.2, using Sentry's pytest plugin. No Snuba/Kafka | Yes |
| End-to-end | see below | A real self-hosted Sentry 26.3.1 (errors-only) plus `gitea/gitea:28.1.0` | No, local/manual |

E2E tests need a dedicated self-hosted Sentry 26.3.1 checkout. Do not use a production one: the
installer edits marked blocks in that checkout's `enhance-image.sh` and `sentry.conf.py` and rebuilds its
images. Put `SELF_HOSTED_DIR=/path/to/self-hosted` (and `DOCKER_CONFIG` if your Compose is too old) in
the git-ignored `.e2e/local.env`, then run:

```sh
make e2e-gitea-up && make e2e-bootstrap && make e2e-install && make e2e-test
uv run pytest tests/e2e -m e2e -v -rs   # full suite; export SELF_HOSTED_DIR etc. first
```

See [docs/TESTING.md](docs/TESTING.md) for prerequisites, resource needs, credentials handling and
cleanup (`make e2e-down`, `make e2e-uninstall`). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
Security reports: [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution. The only third-party Sentry code
adapted here comes from **Sentry 24.10.0**, which is available under Apache-2.0, and each adapted file is
listed in `NOTICE`. Sentry 26.x is FSL-1.1 and served only as a reference for interfaces; no code is
copied from it. No code is copied from the archived, BSL-1.1 `sentry-plugins` repository either.

This project is not affiliated with or endorsed by Sentry (Functional Software, Inc.) or the Gitea
project. Their names are used only to describe compatibility.
