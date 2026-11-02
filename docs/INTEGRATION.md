# Organization integration

This integration targets **self-hosted Sentry 26.10.0 in monolith mode** and **Gitea 28.1.0**.
It installs one Gitea owner namespace into a Sentry organization using OAuth, then supports
creating, searching, linking and unlinking issues; opt-in issue synchronization (status,
assignees, Sentry notes); repositories with a signed webhook, commits, pull requests and release
commit ranges; stack-trace links through code mappings; and email-only CODEOWNERS import.
Multiple owner namespaces may be installed. This is separate from the
[project plugins](PLUGINS.md), which use personal access tokens.

Advertised features: `ISSUE_BASIC`, `ISSUE_SYNC`, `COMMITS`, `STACKTRACE_LINK`, `CODEOWNERS`.
Opt-in blame-based suspect commits come from local git mirrors (see
[Blame-based suspect commits](#blame-based-suspect-commits)). There is no pull-request comment bot
and no alert-rule ticket action (see [Failures and limitations](#failures-and-limitations)).

## Register the Gitea OAuth application

In Gitea, open your user settings, **Applications**, and create an OAuth2 application for Sentry.
Use a **confidential client** and register this exact redirect URI, including its trailing slash:

```text
<Sentry system.url-prefix>/extensions/gitea/setup/
```

For example, `https://sentry.example/extensions/gitea/setup/`. If Sentry has a hosting subpath,
include it. Keep the client ID and client secret for `sentry.conf.py` (below). Use HTTPS for
production. The flow requests these explicit scopes and uses S256 PKCE:

```text
read:user read:organization write:issue write:repository
```

Gitea applies repository permissions separately from scopes. The authorizing user must be able to
read the selected repositories and create issues, labels and comments as needed. Organization installs
require **owner** membership; team administrator permission is insufficient. A personal install can
only select the authorizing user's own namespace.

## Configure and install

Install the package wheel into every Sentry web/worker image, following the
[README](../README.md#install-self-hosted-sentry) and the
[`enhance-image.sh`](../docker/enhance-image.sh) example. Sentry itself comes from
`ghcr.io/getsentry/sentry:26.10.0`; do not install a separate Sentry Python dependency.
Append the following to the self-hosted `sentry/sentry.conf.py`, retaining the other configured
providers (the same lines are in [`sentry.conf.py.snippet`](../docker/sentry.conf.py.snippet)):

```python
from datetime import timedelta

SENTRY_DEFAULT_INTEGRATIONS += ("sentry_gitea.integration.provider.GiteaIntegrationProvider",)
ROOT_URLCONF = "sentry_gitea.urls"
SENTRY_API_PAGINATION_ALLOWLIST = set(SENTRY_API_PAGINATION_ALLOWLIST) | {"GiteaSearchEndpoint"}
# Registers the push backfill and identity refresh tasks with the taskworker.
TASKWORKER_IMPORTS = (*TASKWORKER_IMPORTS, "sentry_gitea.integration.tasks")
TASKWORKER_SCHEDULES["gitea-identity-refresh"] = {
    "task": "integrations:sentry_gitea.integration.refresh_identities",
    "schedule": timedelta(hours=12),
}

GITEA_BASE_URL = "http://gitea:3000"
GITEA_PUBLIC_URL = "https://gitea.example"
GITEA_CLIENT_ID = "<oauth client id>"
GITEA_CLIENT_SECRET = "<oauth client secret>"
# Optional; defaults to Sentry's system.url-prefix (recommended).
# GITEA_WEBHOOK_URL_PREFIX = "https://sentry.example"
```

Write literal values. Self-hosted does not pass arbitrary environment variables into its Sentry
containers, so `os.environ["GITEA_CLIENT_ID"]` raises `KeyError` while the settings load and every
Sentry service then fails to start. If you do read values from the environment, add the variables
to **every** Sentry service (for example in `docker-compose.override.yml`) and use `.get()` with a
fallback. Self-hosted's `sentry/Dockerfile` copies the whole `sentry/` directory, including this
file, into the locally built image, so treat that image as sensitive.

| Setting | Purpose |
| --- | --- |
| `GITEA_BASE_URL` | Required. URL reachable from Sentry web and workers; API and token grants use this URL. |
| `GITEA_PUBLIC_URL` | Optional; defaults to `GITEA_BASE_URL`. Browser-reachable URL used for OAuth authorization and all newly generated Gitea issue and source links. |
| `GITEA_CLIENT_ID` | Required. ID of the confidential Gitea application. |
| `GITEA_CLIENT_SECRET` | Required. Secret of that application; keep it out of source control. |
| `GITEA_WEBHOOK_URL_PREFIX` | Optional; defaults to Sentry's `system.url-prefix`. Base URL **Gitea** uses to reach Sentry for repository webhooks. Normalized like `GITEA_BASE_URL`. See [Reaching Sentry from Gitea](#reaching-sentry-from-gitea). |
| `TASKWORKER_IMPORTS` | Must include `sentry_gitea.integration.tasks` in web, worker and scheduler processes, so the push backfill and identity refresh tasks (Sentry's existing `integrations` namespace) are registered. |
| `TASKWORKER_SCHEDULES["gitea-identity-refresh"]` | Recommended. Runs the identity refresh task every 12 hours; see [Tokens and refresh](#tokens-and-refresh). |
| `GITEA_BLAME_ENABLED` | Optional; default `False`. Turns on blame-based suspect commits; needs `git` in the image. See [Blame-based suspect commits](#blame-based-suspect-commits). |
| `GITEA_BLAME_MIRROR_DIR` | Optional; default `/data/sentry-gitea/mirrors`. Root of the bare git mirrors. It must be shared by web and taskworker processes; self-hosted mounts the `sentry-data` volume at `/data` in all of them. |
| `GITEA_PLUGIN_ALLOWED_BASE_URLS` | Used by the [project plugins](PLUGINS.md#allowed-base-urls) only. When unset, the origins of `GITEA_BASE_URL` and `GITEA_PUBLIC_URL` are allowed. |

URLs retain a hosting subpath and are normalized without a trailing slash. They cannot contain user
credentials, queries or fragments. Use the same settings for web and worker processes. Rebuild the
enhanced image and recreate all Sentry services (`./install.sh` does both).
`GET /extensions/gitea/ping/` answers with the package version once the package is loaded.

Gitea 1.26 and later default to `[server] PUBLIC_URL_DETECTION = auto`, which can
make API `html_url` values use the internal request hostname. Setting it to `never`
makes Gitea's own URLs use `ROOT_URL`. This integration builds its issue links from
`GITEA_PUBLIC_URL` (or `GITEA_BASE_URL` when unset), so it does not depend on that
Gitea setting or the host in `html_url`. API requests and token grants continue to
use `GITEA_BASE_URL`.

In Sentry, open **Organization Settings → Integrations → Gitea** and choose **Install**. Sentry 26.10
installs integrations through a dialog that only renders Sentry's built-in providers, and its generic
pipeline no longer runs provider-defined steps. Gitea therefore sets `canAdd: false` and an
`externalInstall` link to `/extensions/gitea/install/`, which runs the install pipeline itself and
continues at `/extensions/gitea/setup/`, the OAuth redirect URI. The install page asks for a Sentry
organization when you can install into more than one (`?organization=<slug>` skips the question).
Authorize in Gitea, then select your namespace or an organization you own. The last page tells you
to close it; reload the integration's page in Sentry to see the installation. The selection is verified again before
Sentry saves it. Sentry's normal `org:integrations` permission is required for setup; install as an
organization owner, not a Sentry superuser (see [Logs](#logs)).
Missing settings leave Gitea visible in the integrations directory and produce a configuration error
when starting setup. The normal `organizations:integrations-issue-basic` gate must also be enabled
for the organization to use issue actions.

The installation is identified by SHA-256 of `v1|<normalized base URL>|<user or org>|<owner numeric ID>`.
Renaming an owner does not change its installation identity. Reinstalling the same owner rotates the
stored OAuth identity and preserves the integration's webhook key.

To upgrade or remove the integration, follow [Upgrade](../README.md#upgrade) and
[Uninstall](../README.md#uninstall) in the README. Delete the repositories in Sentry before
uninstalling, so their Gitea hooks are removed.

### Secrets at rest

The OAuth access token, refresh token and expiry are stored in Sentry's `Identity.data`; the
installation's webhook key is stored in the integration's metadata. Both are Sentry encrypted JSON
fields, which store **plaintext unless** Sentry's `database.encryption.method` option is set to
`fernet` and Fernet keys are configured (`DATABASE_ENCRYPTION_SETTINGS`). The default method is
`plaintext`. The OAuth application secret stays in `sentry.conf.py`. Protect the Sentry database,
its backups and the built image accordingly.

### Tokens and refresh

Gitea issues access tokens that expire after one hour by default
(`[oauth2] ACCESS_TOKEN_EXPIRATION_TIME = 3600`) and refresh tokens that expire after
**730 hours** (about 30 days, `[oauth2] REFRESH_TOKEN_EXPIRATION_TIME`).

- Before each API call, an access token that expires within 5 minutes is refreshed first. An
  upstream 401 also causes one serialized refresh. Every refresh stores the rotated tokens.
- The `sentry_gitea.integration.refresh_identities` task refreshes every Gitea identity that has not
  been refreshed for 7 days. With the `gitea-identity-refresh` schedule above it runs every 12 hours,
  so an installation that is idle for weeks keeps a valid refresh token. Without the schedule, an
  installation unused for longer than the refresh-token lifetime must be reinstalled. Keep that
  lifetime well above 7 days.
- If the refresh token has expired or was revoked, operations fail with an error asking you to
  reinstall. Reinstalling the same owner keeps the integration, its links and its webhook key.
- Leave `[oauth2] INVALIDATE_REFRESH_TOKENS` at its default `false`. With `true`, installations
  authorized by the same Gitea user share one grant, and each refresh invalidates the refresh
  tokens of the others.

Changing `GITEA_BASE_URL` changes installation identity; use a new install for the new instance.
Existing installations refuse to refresh against a different configured server.

## Create and link issues

From a Sentry issue's integration actions, select the installed Gitea owner:

- **Create:** choose an owner/name repository, edit the generated title and description, optionally
  enter comma-separated labels and an assignee's Gitea username. Missing labels are created and their
  numeric IDs are submitted. The repository selection is remembered per Sentry project. If the
  remembered repository is no longer usable (deleted, moved, transferred to another owner, no
  longer accessible, or with issues disabled or moved to an external tracker), the form opens with
  no repository selected.
- **Link:** choose a repository and search by issue number (`12` or `#12`) or title text. Pull requests
  are excluded. The optional comment defaults to the Sentry issue URL and includes a hidden
  `<!-- sentry-gitea-link <group ID> -->` marker. Leave the comment blank to omit the backlink.
  The form carries signed context so editing the comment preserves its marker. Retries check existing
  comments under a lock to avoid duplicating the backlink.
- **Unlink:** remove the association in Sentry. This does not delete the Gitea issue or its comments.

Values taken from the event (title, culprit, release, environment and message) are rendered as
inline code in the generated description, so they cannot @mention Gitea users or create automatic
links. The description ends with a hidden `sentry-gitea-meta v1` block that identifies the Sentry
issue. Before creating, the integration searches the selected repository for an issue with that
block; if it finds one, it links that issue instead of filing a duplicate. A create that failed
after Gitea had already filed the issue can therefore simply be retried. This also applies after
an unlink: to file a separate issue for the same Sentry issue, remove the hidden block from the
description. Recovery depends on Gitea's issue search index and can miss an issue that is not
indexed yet.

Markers are trusted only when the installation's own Gitea user wrote them: recovery considers
only issues authored by that user, and existing backlink comments and synced-note comments count
only when that user authored them. A marker copied into someone else's issue or comment is ignored.
If a different Gitea user reinstalls the same owner, markers written by the previous user are no
longer trusted.

Issue records use `<repository numeric ID>#<issue number>` as the immutable key and
`owner/name#number` as the display name. Create, link, lookup and search build their
`url` and `metadata.url` as `<GITEA_PUBLIC_URL or GITEA_BASE_URL>/<full_name>/issues/<number>`,
with the repository path quoted for URLs. Repository and issue identities are still
validated; `html_url` does not select the destination. Sidebar rendering uses stored
URLs without upstream HTTP. Existing links remain readable; re-link to refresh a
stored URL after changing `GITEA_PUBLIC_URL`. Explicit issue lookup without a selected
path can resolve the repository's current path by its ID; re-linking refreshes its
stored URL/display name after a rename. Issue bodies and backlink comments continue
to use Sentry URLs.

The generic API is:

```text
/api/0/organizations/<org>/issues/<group_id>/integrations/<integration_id>/
```

GET `?action=create` or `?action=link` returns the form. POST creates, PUT links using `externalIssue`
and `repo`, and DELETE unlinks with `?externalIssue=<Sentry ExternalIssue row ID>`.
For a programmatic backlink, submit the link form's `linkContext` with the comment, or retain its
marker. The DELETE identifier is a Sentry database ID, while the PUT selection is a canonical Gitea key.

Authenticated async choices use:

```text
GET /extensions/gitea/search/<organization_id_or_slug>/<integration_id>/?field=repo&query=example
GET /extensions/gitea/search/<organization_id_or_slug>/<integration_id>/?field=externalIssue&query=12&repo=owner/name
```

Both return lists of `{label, value}` objects and require normal Sentry organization membership and
access to the installation. Missing/blank queries and unsupported fields return 400, as required by
Sentry's search base. Selectors are bounded to two pages of 50 results; narrow the search to find more.
Wrong-owner repository selections are rejected even if the OAuth user has access to them.

## Repositories and webhooks

Sentry 26.10 has no Add Repository form. Its **Organization Settings → Repositories** page lists
what Sentry's repository sync imports: hover the Gitea owner's repository count and click **Sync
now**, or wait for the daily sync. The sync imports every repository of the installed owner that the
authorizing user can see, and disables repositories that disappeared from it. To add one repository,
use the API:

```text
POST /api/0/organizations/<org>/repos/
{"provider": "integrations:gitea", "installation": <integration id>, "identifier": "owner/name"}
```

The repository must belong to the installed owner, and the **authorizing Gitea user must have
admin rights on it**; otherwise the request fails with
`Gitea repository admin permission is required to install the Sentry webhook.` Repositories
without Gitea's built-in issue tracker can be added; they are only hidden from issue forms.

Adding a repository creates exactly one Gitea repository hook: type `gitea`, JSON, active, all
branches, for the events `push`, `issues_only`, `issue_assign` and `pull_request`, delivered to

```text
<GITEA_WEBHOOK_URL_PREFIX or system.url-prefix>/extensions/gitea/webhook/<integration id>/<Gitea repository id>/
```

Each hook has its own secret: the hex HMAC-SHA256 of `repo:<Gitea repository id>`, keyed with the
installation's webhook key. Gitea shows a hook's secret to the repository's admins in its web UI,
so a repository admin can read it. Because the endpoint accepts only payloads for the repository
named in the URL, a leaked secret can forge deliveries for that one repository only.

Adding the same repository again repairs its hook. A hook this integration recorded is kept if it
already uses the per-repository URL; its events and active flag are refreshed. Gitea's API never
returns a hook's secret and cannot change it, so any other hook the integration recorded (for
example one with an older URL format or prefix), and any unrecorded hook pointing at this
integration's webhook URLs, is deleted and created again with the per-repository secret. Adding a
repository again also repairs rows created by bulk code mappings (path-valued external ID, no
hook). The Sentry `Repository` stores the numeric Gitea repository ID as `external_id`,
`owner/name` as `name`, and the public repository URL. Deleting the Sentry repository removes its hook once no other Sentry
organization sharing the installation still uses it; a hook or repository already gone in Gitea
counts as success. Repository deletion in Sentry is scheduled, so the hook disappears when the
deletion runs.

After adding a repository, push a commit (or use **Test Delivery** on the hook) and check the
delivery's response code in the repository's **Settings → Webhooks** page in Gitea:

| Response | Meaning |
| --- | --- |
| `204` | Accepted, or authenticated but ignored (unhandled event/action, repository not added, repository moved to another owner). |
| `400` | Malformed JSON or payload, a body larger than 2 MiB, or a payload whose repository ID differs from the one in the URL. |
| `401` | Missing or invalid `X-Gitea-Signature` (hex HMAC-SHA256 of the raw body with the repository's secret). |
| `404` | Unknown, disabled or uninstalled integration, or an older hook URL without the repository ID (add the repository again to repair its hook). |
| `500` | An unexpected error while processing the delivery for at least one Sentry organization (logged as `sentry-gitea: sync failed`). |
| `503` | A Gitea API call needed for synchronization failed. Redelivering is safe. |

The signature is verified over the exact request bytes, with a constant-time comparison and a
non-empty secret, before the body is parsed. Rejections are logged without the body or secret.
When several Sentry organizations share an installation and the repository, the delivery is
processed for each of them; a failure for one does not stop the others, and the error status is
returned only after all of them were processed. Handlers are idempotent, so redelivering from
Gitea's webhook page is safe.

### Reaching Sentry from Gitea

Gitea 28 refuses webhook targets on private and loopback addresses unless its egress policy allows
them. Prefer the default target, Sentry's public `system.url-prefix`: public addresses need no
entry. If Gitea can reach Sentry only over a private network, for example a shared Docker network:

- Do **not** set `[security] ALLOWED_HOST_LIST = private`. Any Gitea user who can create a webhook
  could then send requests to every private service that Gitea can reach.
- Create a dedicated Docker network that contains only Gitea and Sentry's `nginx` (or `web`)
  container, and allow exactly its subnet in Gitea's `app.ini`. A port suffix narrows it further,
  here to nginx's port 80:

  ```ini
  [security]
  ALLOWED_HOST_LIST = 172.30.0.0/24:80
  ```

  Use the CIDR that `docker network inspect <network>` reports. Gitea 28 needs an explicit IP or
  CIDR entry for private targets; a host name entry alone is not enough. The older
  `[webhook] ALLOWED_HOST_LIST` is deprecated.
- Set `GITEA_WEBHOOK_URL_PREFIX` to Sentry's address on that network, for example `http://nginx`.

Whatever you allow is reachable by the webhooks of every Gitea user, not only by this integration.

### Commits, pull requests and releases

- **Push:** every delivered commit is stored with its message, author name/email, timestamp and
  file changes (`A`/`M`/`D`); deliveries are idempotent, and commits whose message contains
  `#skipsentry` are skipped. Gitea truncates `commits[]` to `[ui] FEED_MAX_COMMIT_NUM` (default 5)
  but reports the real `total_commits`; when commits are missing, the `backfill_push` task pages
  `GET /repos/{owner}/{repo}/commits?sha=<after>&not=<before>&files=true` and imports the rest,
  newest first, **bounded to 1,000 commits**. Hitting the bound logs `sentry-gitea: backfill
  incomplete` with the counts. Transient Gitea failures are retried three times (after 1, 5 and
  15 minutes). Branch creation imports only the delivered commits; branch deletion is ignored.
- **Pull requests:** `pull_request` events create or update Sentry pull requests keyed by number
  (title, body, author, merge commit SHA once merged). Gitea webhooks hide private author emails
  (`<id>+<login>@<noreply>`); such authors are not recorded.
- **Releases:** for release refs (`repository` = `owner/name`), Sentry reads the complete range
  between the previous and the new commit, with file changes, through the same commits API. Ranges
  over **5,000 commits fail** with an explicit error instead of importing a truncated range. Without
  a previous commit, the last 20 commits up to the release commit are used.

## Issue synchronization

Every option is off by default. Set them on the integration's configuration page or with
`POST /api/0/organizations/<org>/integrations/<integration id>/`, for example
`{"sync_status_forward": true, "user_mappings": {"dev@example.com": "dev"}}`. Sentry's
`organizations:integrations-issue-sync` feature (on by default) must be enabled.

| Option | Behavior |
| --- | --- |
| `sync_status_forward` | Resolving a linked Sentry issue closes the Gitea issue; unresolving reopens it. No write when Gitea is already in that state. |
| `sync_status_reverse` | Closing/reopening the Gitea issue resolves/unresolves the linked Sentry issues. The issue is re-read first, so a replayed or late delivery applies Gitea's current state. |
| `sync_comments` | A new Sentry note becomes a Gitea comment `**<name>** wrote in Sentry:` plus the note text and a hidden `<!-- sentry-gitea-note <note id> -->` marker; editing the note edits that same comment. Gitea comments are **not** imported into Sentry. |
| `sync_assignee_forward` | Assigning a Sentry user sets the Gitea assignees to that user's mapped username (replacing others); unassigning clears them. Unmapped users and team assignments change nothing (logged). |
| `sync_assignee_reverse` | Assigning/unassigning in Gitea re-reads the issue; the lowest-ID Gitea assignee whose mapping resolves to a verified, active member of the organization with access to the issue's project is assigned in Sentry. An empty assignee set unassigns. |
| `user_mappings` | JSON object `{"<Sentry user email>": "<Gitea username>"}`, at most 500 entries. Sentry emails must be verified. Gitea webhook emails are never trusted. |

Reverse options need the repository to be added (the webhook delivers the events); merely linking
an issue is not enough. Changes applied from Gitea are tagged with this integration as their source,
so they are not sent back to Gitea. Each Sentry organization sharing an installation uses its own
options and mappings. Notes are tracked per linked issue; a comment is edited only if it was written
by the installation's Gitea user and still ends with its marker, and marker-like text inside notes
is escaped.

## Code mappings, stack-trace links and CODEOWNERS

Create code mappings for an added repository in **Settings → Integrations → Gitea → Code
Mappings** (stack-trace root, source-code root, branch). Sentry's
`GET /api/0/projects/<org>/<project>/stacktrace-link/?file=…` then checks that the file exists
through Gitea's metadata-only `GET /repos/{owner}/{repo}/contents-ext/{path}?ref=…` endpoint, first
at the event's release commit and then at the mapping's branch, and returns a link on the
**public** URL:

```text
<GITEA_PUBLIC_URL>/<owner>/<repo>/src/commit/<sha>/<path>     (40/64-hex versions)
<GITEA_PUBLIC_URL>/<owner>/<repo>/src/branch/<branch>/<path>
```

Sentry's frontend appends `#L<line>`, which Gitea understands. Paths are percent-encoded and can
never leave the repository; missing files yield no link, and `403` responses surface Sentry's
"forbidden" state. Pasting a Gitea source URL to derive a mapping is supported for `src/branch`,
`src/tag` (branch and tag names may contain slashes) and `src/commit` links on the public URL.

CODEOWNERS files are looked up in this order: `CODEOWNERS`, `.gitea/CODEOWNERS`,
`.github/CODEOWNERS`, `docs/CODEOWNERS`, and retrieved with
`GET /api/0/organizations/<org>/code-mappings/<id>/codeowners/`. **Only email owners are
supported.** A file containing `@user` or `@org/team` owners is rejected with
`Gitea CODEOWNERS supports email owners only; found @handles: <first few>`, because Sentry would
otherwise resolve those names against other providers' external users. Previously imported rules
are kept when a refresh fails. Pushes that change any of these four files trigger Sentry's
CODEOWNERS auto-sync for imported mappings.

## Blame-based suspect commits

Gitea has no blame API. With `GITEA_BLAME_ENABLED = True`, the provider adds `gitea` to Sentry's
list of commit-context providers when Sentry loads it, in web and worker processes alike
(`COMMIT_CONTEXT_INTEGRATION_PROVIDERS` in `sentry.tasks.post_process`), and the installation
implements Sentry's commit-context interface by running `git blame` on a local mirror. The Sentry
image has no `git`; install it in `sentry/enhance-image.sh` (see
[`enhance-image.sh`](../docker/enhance-image.sh) and the README).

For a new issue in a project with a Gitea code mapping, Sentry maps each in-app frame to a file and
line on the mapping's branch and asks the integration for blame. The integration then works per
repository:

1. **Mirror.** One bare mirror per Sentry repository, at
   `<GITEA_BLAME_MIRROR_DIR>/<integration id>/<Gitea repository id>.git`. The Gitea repository ID
   survives renames. If the mirror does not exist yet, the
   `sentry_gitea.integration.mirror_repository` task clones it, and this blame skips the
   repository. Adding a repository in Sentry queues the same task.
2. **Revision.** A full commit SHA the mirror already has is used directly. Anything else (a branch
   or tag name, or an unknown commit) fetches all branches and tags first, at most once every
   5 minutes per mirror, then resolves the name. A push webhook for the repository lifts that limit
   for the next fetch, so an issue right after a push is not blamed on the branch's old head. An
   unknown revision skips its frames.
3. **Blame.** `git blame --line-porcelain` runs once per file, with one `-L n,n` per frame line.
   Author name and email, committer time and the commit summary become Sentry's `CommitInfo`.
   Missing files and lines past the end of a file are skipped.

Sentry then keeps the most recent commit within 60 days before the issue's first event, stores it
as a commit of the repository, and records it as the issue's suspect commit.

Clones and fetches use `<GITEA_BASE_URL>/<owner>/<name>.git`, where `owner/name` is looked up by
repository ID through the API first. The installation's OAuth access token is refreshed if needed
and passed as `http.extraHeader=Authorization: Bearer …` through git's `GIT_CONFIG_*` environment
variables. It never appears in argv, in the mirror's `config` (which stores no remote at all), or
in logs. Git runs with prompts, system and global configuration and hooks disabled. Timeouts kill
git and its helper processes: 10 seconds for a local command, 30 seconds for a fetch during blame,
25 minutes for a clone. One blame request stops starting git commands after 45 seconds, inside
Sentry's 90-second task deadline. A fetch that fails during blame queues the mirror task, which
fetches again with the clone's time limit. A file lock next to each mirror (`<id>.lock`, kept after
deletion) serializes clone, fetch and delete across processes. A clone goes to a temporary
directory and becomes the mirror through an atomic rename, so an interrupted clone never looks like
a mirror; the next clone removes its leftovers.

Deleting a repository in Sentry deletes its mirror once no other live Sentry repository of the same
installation uses it. Uninstalling the integration does not; delete
`<GITEA_BLAME_MIRROR_DIR>/<integration id>/` yourself. Failures are logged as `sentry-gitea: blame failed` with an `operation` and
IDs; Sentry then records no suspect commit for that issue.

Sentry calls pull-request comment hooks only for providers that implement its PR comment workflow.
Gitea does not, so no pull-request comments are ever queued.

For a project with a Gitea code mapping, Sentry 26.10 uses blame instead of release-based suspect
commits once the provider is on its list. That is why the feature is off by default.

## Failures and limitations

Reads retry once after a token refresh; failed writes return an error and are never automatically
replayed. If Gitea creates an issue but omits requested labels or assignees, check permissions, then
link that existing issue and correct it in Gitea.

There is one operator-approved Gitea instance and OAuth application per deployment, with multiple
owner installs. Sentry SaaS, control/cell deployments, and a PAT fallback are not supported by this
integration.

Not supported:

- **Suspect-PR comments.** Blame-based suspect commits are opt-in (above); Sentry's comments on
  the pull request that introduced a suspect commit are not supported.
- **Pull-request comment bot** and **alert-rule ticket actions**.
- **Inbound comments:** Gitea comments are never imported into Sentry notes.
- **Bounds:** push backfills stop at 1,000 commits (logged); release ranges over 5,000 commits fail.
  Webhook bodies over 2 MiB are rejected. Gitea's issue-comment list is unpaginated; responses over
  2 MiB fail and are logged.
- **CODEOWNERS:** email owners only. Rules submitted directly to Sentry's project CODEOWNERS API
  (rather than fetched through the code mapping) are validated by Sentry alone.
- **Assignees:** team assignments are not synchronized; forward sync sets a single Gitea assignee.

Sentry's prebuilt frontend has fixed provider-name/icon mappings. **The issue sidebar can show
a blank provider name and a generic icon for Gitea.** The backend catalog and generic forms still use
the registered provider. Python package installation cannot add a branded entry to those frontend
mappings or to fixed source-code-management provider filters.

Legacy plugin links are separate from modern integration links. To move a project, explicitly link
its existing Gitea issues through this integration and disable any duplicate legacy automatic filing.
No automatic migration is performed.

## Logs

Our logs use `sentry_gitea.integration.*` and the prefixes `sentry-gitea: install failed`,
`sentry-gitea: oauth failed`, `sentry-gitea: issue operation failed`,
`sentry-gitea: token refresh failed`, `sentry-gitea: identity keep-alive` (INFO, with counts),
`sentry-gitea: webhook rejected` (with `reason` `url`, `integration`, `signature`, `payload` or
`repository`), `sentry-gitea: webhook ignored`, `sentry-gitea: backfill incomplete` and
`sentry-gitea: sync failed` (with `operation`), `sentry-gitea: blame failed` (with `operation`) and
`sentry-gitea: blame skipped, mirror not ready` (INFO).
Diagnostic extras contain IDs, status and error type; they omit request and response bodies,
tokens, authorization codes, state, verifiers and secrets.

The OAuth callback URL carries a single-use authorization `code` and `state`. Both are bound to
this integration's PKCE verifier and cannot be replayed, but avoid writing them to logs:

- **Install as an organization owner, not a Sentry superuser.** Sentry's `sentry.superuser` audit
  logger records the full URL of every request a superuser makes.
- **Reverse proxies log the callback query.** Self-hosted's nginx writes the full request line,
  including the query string, to its access log (the container's output). Treat those logs as
  sensitive, or configure your proxy not to log query strings for `/extensions/gitea/setup/`.

## Tests

The Sentry-level suite exercises the provider, OAuth identity and refresh, issues, search,
repositories, webhooks, sync, source links, CODEOWNERS and blame (against a real repository served
over HTTP, through Sentry's `process_commit_context` task) inside the pinned Sentry image. The
end-to-end suite runs the real OAuth flow, issue actions, webhooks and sync against a self-hosted
Sentry and Gitea. See [TESTING.md](TESTING.md).
