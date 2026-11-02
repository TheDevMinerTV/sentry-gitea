# Project plugins

These plugins target self-hosted Sentry **26.3.1** and Gitea **28.1.0**. The `gitea`
plugin creates, links and unlinks issues from Sentry's legacy issue sidebar. The
`gitea_alerts` plugin files issues when an issue alert fires and comments on linked
open issues. Both use the same project credentials and the same stored link.

## Install

Build a wheel from this checkout with `uv build --wheel`. Copy the resulting wheel
into the self-hosted checkout's `sentry/` directory. Add an installation command to
that checkout's executable `sentry/enhance-image.sh`, preserving any existing image
customizations. For version 0.2.0 the command is:

```sh
python -m pip install --no-deps --no-index --force-reinstall \
    /usr/src/sentry/sentry_gitea-0.2.0-py3-none-any.whl
```

Use the exact filename produced by the build when installing a different version.
Sentry itself comes from `ghcr.io/getsentry/sentry:26.3.1`; do not install it with pip.
The image already provides `requests` and `urllib3`. Rebuild the enhanced image using
the self-hosted checkout's installation procedure (`./install.sh`) and recreate every
Sentry service that runs application code, including web, taskworkers and the
scheduler. Installing only in the web container leaves alert delivery unavailable in
workers.

Distribution entry points register both legacy plugins automatically. The plugins
need no OAuth app, webhook, custom alert action class or organization integration.
The only `sentry.conf.py` setting they read is the optional
[`GITEA_PLUGIN_ALLOWED_BASE_URLS`](#allowed-base-urls). The optional
`ROOT_URLCONF = "sentry_gitea.urls"` shim also exposes the package ping route and
preserves Sentry's routes. To upgrade or remove the plugins, follow
[Upgrade](../README.md#upgrade) and [Uninstall](../README.md#uninstall) in the README.

## Enable and configure Gitea

In each Sentry project's settings, open **Legacy Integrations / Plugins**, select
**Gitea**, and enable it. The settings URL ends in
`/settings/<organization>/projects/<project>/plugins/gitea/`.

Create a Gitea personal access token with **read:user**, **read:repository** and
**write:issue** scopes. Its user also needs access to the repository and permission
to write issues and labels there; token scopes alone do not grant repository rights.
Gitea must use its built-in issue tracker for that repository.

| Field | Value |
| --- | --- |
| Base URL | Gitea URL reachable from Sentry containers, e.g. `http://gitea:3000` or `https://forge.example/git`. A hosting subpath is retained; trailing slashes are removed. Userinfo, query strings and fragments are rejected. A private or loopback address must be allowed (see below). |
| Gitea URL for links (`public_url`) | Optional browser-facing Gitea URL, e.g. `https://forge.example/git`. Leave empty when `base_url` is already reachable by users. Normalized and validated exactly like the base URL. |
| Personal access token | The PAT. It is never returned in configuration responses, including a prefix. Leave it blank after saving to keep it; enter it again whenever you change the base URL. |
| Repository | `owner/name`; saving verifies the token and fetches the repository. |
| Default labels | Optional comma-separated names applied to every newly created issue, including alerts. |

HTTP is supported for self-hosted internal networks; use HTTPS when traffic crosses
an untrusted network. Rotate the PAT by saving a new value. Do not paste tokens into
URLs or issue fields. If saving fails, the error names the HTTP status or a short
category (for example, unreachable or unauthorized). Gitea's response body is never
shown.

**The PAT is stored in plaintext** as a Sentry project option. Sentry's optional
database field encryption does not cover project options. Anyone with access to the
Sentry database, its backups or Sentry's administrative shell can read it, so use a
dedicated Gitea user whose rights are limited to the repositories it files into.

### Allowed base URLs

A project admin chooses the base URL, and Sentry's web and worker containers call it
from inside your network. To keep that from reaching internal services, the plugins
send requests in one of two ways:

- If the base URL's origin (`scheme://host[:port]`) is listed in
  `GITEA_PLUGIN_ALLOWED_BASE_URLS`, requests go out directly and may reach private
  or loopback addresses.
- Every other base URL goes through Sentry's outbound HTTP guard. It refuses
  addresses in `SENTRY_DISALLOWED_IPS` (by default private, loopback, link-local and
  other reserved ranges) and does not follow redirects.

By default the allowlist contains the origins of `GITEA_BASE_URL` and
`GITEA_PUBLIC_URL` when the organization integration is configured, and is empty
otherwise. Setting `GITEA_PLUGIN_ALLOWED_BASE_URLS` replaces that default, so list
every origin you need. Invalid entries are ignored and logged as a WARNING
(`sentry-gitea: ignoring an invalid GITEA_PLUGIN_ALLOWED_BASE_URLS entry`). If your
Gitea has a private address, list its origin in `sentry/sentry.conf.py` and rebuild:

```python
GITEA_PLUGIN_ALLOWED_BASE_URLS = ("http://gitea:3000", "https://forge.internal.example")
```

A base URL that is not allowed fails with "Sentry refused to connect: the Gitea URL
resolves to a private or reserved address", both when saving the configuration and
when an alert or sidebar action runs.

When the base URL changes, the saved token is not reused: enter it again in the same
save. This keeps a token from being sent to a Gitea URL it was not created for.

### Links

New issue links use `<public_url or base_url>/<owner>/<repo>/issues/<number>` with
the repository path quoted for URLs. This applies to manual create/link, alert filing
and recovery. API calls still use `base_url`; Gitea's returned `html_url` is ignored.
Gitea 1.26 and later default to `[server] PUBLIC_URL_DETECTION = auto`, so API responses
can contain an internal hostname when Sentry uses one. Setting it to `never` makes
Gitea's own URLs use `ROOT_URL`, but links generated by these plugins do not depend
on that setting. Issue bodies and backlink comments continue to point to Sentry.

### Sidebar actions

From a Sentry issue's legacy sidebar, **Create** opens title, description, assignee
and label fields. The default title includes the qualified Sentry short ID; the body
includes the issue URL, level, release, environment, location and last event ID.
Values taken from the event (title, culprit, release, environment and message) are
rendered as inline code, so they cannot @mention Gitea users or create automatic
links. Assignee choices are repository usernames. Project default labels are always
added to submitted labels, then resolved to numeric Gitea label IDs. Missing
repository labels are created on first use. Organization labels are not searched.

**Link** accepts a positive issue number in the configured repository and an optional
backlink comment. Pull requests cannot be linked as issues. **Unlink** removes only
the Sentry link; it does not delete or close the Gitea issue. The pinned Sentry sidebar
uses its inherited GET unlink route; API clients can also use POST.

Links display as `owner/name#number`. All six string values are stored in GroupMeta:
`gitea:issue_id`, `gitea:issue_title`, `gitea:issue_url`, `gitea:issue_repo`,
`gitea:issue_repo_id`, and `gitea:issue_base_url`. The number is local to the repository.
Changing configuration preserves existing link display; relink an issue to refresh
its stored URL after changing `public_url`. Changing only `public_url` does not change
the repository binding or interrupt repeat alerts. Automatic writes stop if the
stored base URL, repository path or numeric repository identity no longer matches.
Explicitly relink to the intended repository before resuming alerts.

## Enable Gitea Alerts and create an alert rule

Enable **Gitea Alerts** (`gitea_alerts`) on the same project. Keep **Gitea** enabled
and configured; the alerts plugin has no credentials of its own. Enabling or
disabling either plugin takes effect in running taskworkers without a restart.

| Field | Default | Effect |
| --- | --- | --- |
| Alert labels | `source:sentry` | Additional comma-separated names for new alert issues. An empty string disables these extra labels. |
| Severity and class labels | On | Adds `severity:critical/high/medium/low` and `class:crash/other`. |
| Comment on repeat events | On | Adds the firing event's details to a linked open issue. |
| Advisory where-to-look hint | Off | Adds a bounded creation-time comparison when a commit-ish release and source location are available. |
| New issue when a closed issue regresses (`refile_on_regression`) | On | When the linked Gitea issue is closed and Sentry recorded a regression for the group **after** that close, files a new issue that references the closed one and moves the link to it. |
| New issue for any event an hour after closing (`refile_after_close`) | Off | Also files a new issue when the Sentry issue was never resolved: any alert more than an hour after the linked Gitea issue was closed. Use it when people close Gitea issues without resolving Sentry. |

Create a Sentry **issue alert** for the desired error condition and project. Select
the action for sending a notification through a specific legacy service and choose
**Gitea Alerts**, whose service identifier is **`gitea_alerts`**. Its legacy rule
representation is:

```json
{
  "id": "sentry.rules.actions.notify_event_service.NotifyEventServiceAction",
  "service": "gitea_alerts"
}
```

Sentry's newer action picker can categorize this legacy plugin as **WEBHOOK**; that
does not mean a Gitea webhook URL must be configured. The alternative is **Send a
notification (for all legacy integrations)**, represented by
`sentry.rules.actions.notify_event.NotifyEventAction`. That alternative also invokes
other enabled legacy notification plugins. Choose one action path for this purpose;
adding both can deliver duplicate comments.

The first firing event creates an issue and saves the same link the sidebar reads.
Subsequent events fetch the linked issue: open issues receive comments when enabled.
A closed issue is never reopened or commented on. Instead, a **new** issue is filed
when the problem has come back since the close (see "Closed issues" below); otherwise
the event is skipped. Comments include event ID, level,
environment, release, the Sentry event URL and a hidden event marker. Multiple rules
may comment independently. Sentry's rule repeat interval and its inherited plugin
limit of **10 notifications per project per 60 seconds** are separate controls.

**Choosing the trigger (Sentry 26.x).** Sentry 26 runs issue alerts through its
workflow engine. When a rule is converted, the trigger **"The event occurs"**
(`EveryEventCondition`) is silently dropped if it is combined with any other trigger,
so "A new issue is created" + "The event occurs" behaves like "A new issue is created"
alone and **never fires for repeat events**. Pick one:

| Goal | Trigger(s) |
|---|---|
| File an issue once per Sentry issue, no repeat comments | "A new issue is created" |
| File on first event **and** comment on later events | "The event occurs" **as the only trigger** (add filters to narrow it) |

Remember the rule's action interval (minimum 5 minutes) still limits how often one
rule acts on the same Sentry issue.

**Low-traffic instances.** Sentry's post-process step waits on Snuba's commit log.
On a nearly idle self-hosted instance, the alert for the most recent event can run
only when the next event arrives. Busy instances are not affected.

The plugin's **Test Configuration** button only reads the current Gitea user and
repository. It never files a ticket. Verify actual delivery with a real test error
and the chosen rule after checking the configuration.

## Classification and advisory hints

| Event level | Proven unhandled | Proven handled | Unknown handled state |
| --- | --- | --- | --- |
| fatal | critical | medium | medium |
| error | high | low | medium |
| warning | medium | low | medium |
| info, debug, other | low | low | low |

Only an explicit unhandled exception produces `class:crash`. Any false handled
verdict wins; all explicit true verdicts mean handled; a mixture of true and missing
verdicts means unknown. Plain log events with no exceptions count as handled.
Unknown classification is explained in the body. The event release takes precedence
over the group's first release; otherwise the body says `(unknown)`.

Default label colors are `source:*` #5319e7, critical/crash #b60205, high #d93f0b,
medium #fbca04, low #0e8a16, other #c5def5 and custom labels #ededed. Existing labels
keep their colors.

Hints accept a 7–40 character lowercase hexadecimal release SHA, optionally after a
package `@` prefix. They infer up to three file paths, inspect up to 250 commits in a
direct `release..default-branch` comparison, and display at most five matching commits.
Missing file evidence, unsupported releases, failures and exhausted hint budgets
omit the hint. Hints reserve time for the issue creation request. They never alter
classification, labels, closure or assignment and do not implement native blame or
run a fix agent.

## Recovery, diagnostics and limits

Manual mutations and alert delivery share a 60-second per-group Sentry lock. Every
operation rereads the link after acquiring it. If the lock is busy, a manual action
returns 409 ("Another Gitea operation is in progress. Retry shortly."). An alert
retries for up to 10 seconds and is then **skipped**, logging the WARNING
`sentry-gitea: alert skipped; group lock busy`; it is not retried later, but the next
matching event is delivered normally. Link fields are saved in a database transaction.

New issue bodies contain a hidden `sentry-gitea-meta v1` JSON block with a stable
dedup ID derived from the Sentry URL prefix and numeric project/group IDs. Preserve
that block when editing in Gitea. The Sentry create form does not show the block:
the plugin appends the authoritative one when it files the issue, and removes any
block typed into the description.

If remote creation succeeds but its response or local save fails, the next attempt
searches open and closed issues for the exact metadata ID before creating again,
preferring an open match (a re-filed group has several marked issues).
Only issues **authored by the token's Gitea user** are accepted, so a marker copied
into someone else's issue is ignored and a new issue is filed. A recovered issue is
linked without an additional comment. This is recovery, not an exactly-once
guarantee: Gitea search indexing delays, removed metadata, changed Sentry URL
prefixes, a different token user, expired locks or ambiguous network outcomes can
still require manual reconciliation. Unlinking an automatically filed issue does not
erase its remote marker; a later alert can recover that same issue, including a
closed one.

The client uses connect/read timeouts of **5/15 seconds**, a **2 MiB** cap per
response, and at most **20 pages of 50 requested rows**. Each operation is also capped
at **32 MiB** and **5,000 rows** in total across its responses. Server-clamped page sizes are
supported; exhausting a budget fails visibly rather than treating partial results as
complete. Repository assignees are unpaginated. The client requests identity encoding
and rejects unexpected compressed responses. A shared 43-second operation budget
checks available body reads and leaves room within the lock lease for a final socket
read and persistence. DNS resolution and HTTP header parsing still depend on the host
networking stack; these are not a process-level hard deadline. No HTTP write is
retried automatically.

At most 20 distinct labels, each up to 100 characters, can be requested. Concurrent
first use from different Sentry groups can create duplicate same-named labels because
Gitea does not enforce uniqueness; resolution chooses the lowest existing ID. Precreate
shared labels to avoid that race. If Gitea returns 201 but discards labels or assignees
for insufficient repository permissions, delivery reports an error. Recovery can link
that created issue, but does not repair its missing labels/assignees; correct permissions
and adjust the remote issue manually.

**Closed issues.** The plugins have no two-way status sync, so closing a Gitea issue does
not resolve the Sentry issue, and before 0.2.0 a closed link silenced every later alert
for that group. Now, when the linked issue is closed, the next alert files a new issue if:

- `refile_on_regression` (default on): Sentry marked the group as regressed after the
  Gitea issue's `closed_at`. That is the signal Sentry itself uses after a resolve,
  including release- and commit-aware resolution, so a fix still waiting for its deploy
  does not trigger it; or
- `refile_after_close` (default off): the alert runs more than an hour after `closed_at`,
  by Sentry's clock. This covers the common case of a Gitea issue closed while the Sentry
  issue stays unresolved, which can never regress.

The new issue has the same title and labels, a `**Recurrence of:** #N (closed YYYY-MM-DD)`
line (Gitea shows it as a cross-reference on the old issue), and `recurrence_of` in its
metadata block. The link moves to it, so later events comment there: at most one new
issue per close. Nothing is filed when `closed_at` is missing, or when the organization
**integration** links the same Gitea issue to the group, since its status sync owns that
issue. Recovery after a lost link only adopts an **open** issue carrying the marker, never
the closed one being replaced. To silence a group for good, archive it in Sentry.

Diagnostics use `sentry_gitea.plugins.issue` and `sentry_gitea.plugins.alerts`. Failed
alert calls log **ERROR** with `sentry-gitea: alert delivery failed` and `project_id`,
`group_id`, `status`. A status of zero denotes a local/transport/budget failure.
Successful delivery logs **INFO** with `sentry-gitea: filed issue` or
`sentry-gitea: commented on issue`; a re-filed issue's `filed issue` line also carries
`recurrence_of` and `refile_reason` (`regressed` or `after_close`). A repeat event that is
not commented on (closed issue, or repeat comments turned off) logs
`sentry-gitea: skipped repeat event` with a `reason`; for a closed issue,
`refile_skipped` says why no new issue was filed (`disabled`, `no closed_at`,
`integration linked` or `no recurrence signal`). An optional hint failure can log an error
followed by successful filing. No token, headers, raw request exception or
credential-bearing URL is logged. Sentry's legacy callback swallows exceptions, so
consult these logs; the framework does not promise a retry. Event markers support
inspection of ambiguous comment outcomes, but the plugins do not deduplicate comments
across separate rule callbacks.

The plugins do not provide OAuth, organization-level installation, two-way
status/assignee sync, repository webhooks, commit ingestion, CODEOWNERS or source
links; the [organization integration](INTEGRATION.md) does.

## Tests

Host unit tests cover the pure client and rendering; the Sentry-level suite in the
pinned image covers create/link/unlink, configuration validation and serialization,
action routing, locking and persistence; the end-to-end suite runs
the plugins against a real self-hosted Sentry and Gitea. See [TESTING.md](TESTING.md).
