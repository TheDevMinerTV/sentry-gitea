# Testing sentry-gitea

The Sentry-level tests target **Sentry 26.10.0** (Python 3.13 in its image); the end-to-end
suite last ran against **Sentry 26.10.0** and **Gitea 28.2.0**. Sentry 26.x is FSL-1.1 licensed: it is used only as the runtime image and as a reference for
interfaces, and no code is copied from it. The autouse fixtures in `tests/sentry/conftest.py`
are adapted from **Sentry 24.10.0** (Apache-2.0) and listed in [NOTICE](../NOTICE). The
`fixtures` package and everything else in the test tree are original.

## Test layers

| Layer | Command | Runs where | In CI |
| --- | --- | --- | --- |
| Unit | `make unit` | Host. Pure `api/`, `render` and webhook code, HTTP mocked with `responses`. Never imports Sentry. | Yes |
| Harness | `make harness-tests`, or `uv run pytest tests/e2e -m "not e2e"` for all of them | Host. Installer, bootstrap, browser and E2E helper logic, HTTP and Docker mocked. | Yes (all) |
| Sentry-level | `make sentry-tests` | `ghcr.io/getsentry/sentry:26.10.0` with disposable Postgres and Redis, using Sentry's pytest plugin. No Snuba or Kafka. | Yes |
| End-to-end | see [End-to-end tests](#end-to-end-tests) | A real self-hosted Sentry 26.10.0 (errors-only) plus `gitea/gitea:28.2.0`. | No, local |

`make lint` runs `ruff check .` and `ruff format --check .`; ruff also checks the Python code
blocks in Markdown files. E2E HTTP clients and CLI failures never print credential values or
response bodies. Do not run credential-bearing tests with `--showlocals`, HTTP debug logging
or shell tracing.

## Prerequisites

- Python 3.12+, [uv](https://docs.astral.sh/uv/), GNU make and a synced `.venv` (`uv sync`).
  The Makefile calls `.venv/bin/python` and `.venv/bin/ruff` directly.
- For the Sentry-level and E2E layers: Docker Engine and **Docker Compose ≥ 2.32.2**.
  `DOCKER_CONFIG` is inherited by every Docker call, including calls from E2E tests; point it at
  a directory with a newer Compose plugin if the system one is too old.
- Run one heavy workload at a time. Make targets serialize even with `-j`, but separate make
  invocations do not coordinate. The Sentry runner accepts only one or two xdist workers, and the
  E2E suites must run sequentially, without xdist.

The Makefile reads an optional, git-ignored `.e2e/local.env` in **GNU make assignment syntax**
(no `export`, no quotes around ordinary paths):

```make
SELF_HOSTED_DIR=/path/to/self-hosted-26.10.0
DOCKER_CONFIG=/path/to/docker-config-with-new-compose
GITEA_HTTP_PORT=13000
SENTRY_TEST_WORKERS=2
```

Create the directory with `mkdir -p .e2e`. Assignments in this file take precedence over
inherited environment variables, and make command-line assignments take precedence over both.
Direct script and pytest invocations read the environment, not `.e2e/local.env`.

## Host checks

```sh
make unit
make harness-tests
make lint
```

## Sentry-level tests

```sh
make sentry-tests
make sentry-tests SENTRY_TEST_WORKERS=1   # on a busy host
```

This builds a wheel with `uv build --wheel`, starts disposable Postgres and Redis, installs the
wheel in the test container with `pip --no-deps --no-index --force-reinstall`, and runs
`tests/sentry` with the image's bundled pytest. The wheel is a regular install, so Sentry
discovers the real package entry points; `PYTHONPATH=/work` supplies only the `fixtures` stub and
the tests, not `src`. The repository mount is read-only. The built wheel goes to the git-ignored
`.e2e/sentry-tests-wheel/` directory, and pytest caches go to `/tmp` inside the container. Success
is a pytest summary with no failures or errors, followed by teardown of the `sentry-gitea-tests`
Compose project.

`docker/test-sentry.compose.yml` uses the same `postgres:14.19-bookworm` and
`redis:6.2.20-alpine` images as self-hosted, publishes no ports, keeps Postgres data on tmpfs and
uses trust authentication on that isolated network only. `DATABASE_URL` points at this Postgres,
and Sentry's pytest plugin creates its region, control and secondary database aliases there.
Sentry's pytest configuration connects to Redis on localhost, so the test container shares
Redis's network namespace (`network_mode: service:redis`). `SENTRY_SKIP_SELENIUM_PLUGIN=1` avoids
browser dependencies, and `SENTRY_SKIP_SERVICE_VALIDATION=1` skips validation of Kafka, Snuba and
other services these tests do not use.

The Sentry image has no `git`, which the blame tests need, so `docker/run-sentry-tests.sh` installs
it with `apt-get` before the run (this needs network access from the test container). Those tests
serve a real repository over git's HTTP protocol from inside the container.

`tests/sentry/conftest.py` loads `sentry.testutils.pytest`. Do not preinitialize Django or pass
`--ds`: Sentry adjusts apps and databases before `django.setup()`. The tests construct events in
memory and stub latest-event lookups instead of querying Snuba. Integration tests use Sentry's
`no_silo_test` decorator to run in monolith mode.

If a run is interrupted before cleanup:

```sh
docker compose -p sentry-gitea-tests -f docker/test-sentry.compose.yml down
```

## End-to-end tests

### Requirements

- A **dedicated** self-hosted Sentry 26.3.1 checkout, installed and running. Never use a
  production installation: the installer edits marked blocks in its `sentry/enhance-image.sh`
  and `sentry/sentry.conf.py`, rebuilds its images and recreates its services.
- In that checkout's `.env.custom`: `COMPOSE_PROFILES=errors-only` and
  `SENTRY_BIND=127.0.0.1:9000`. A fresh checkout can be installed with
  `./install.sh --skip-user-creation` and started with
  `docker compose --env-file .env --env-file .env.custom up -d --wait`.
- Resources: the self-hosted installer requires at least 2 CPUs and 7000 MB RAM for
  `errors-only`. Leave room for Gitea and the image builds; check `free -h` and `uptime` first.

| Variable | Default / purpose |
| --- | --- |
| `SELF_HOSTED_DIR` | `../self-hosted-26.10.0` next to this repository; the self-hosted checkout. |
| `DOCKER_CONFIG` | Inherited unchanged; Docker credentials and plugin directory. |
| `COMPOSE_PROFILES` | Self-hosted `.env`, then `.env.custom`; an exported value wins. Use `errors-only`. |
| `SELF_HOSTED_NETWORK` | Derived from the self-hosted Compose config, normally `sentry-self-hosted_default`. |
| `SELF_HOSTED_SUBNETS` | Derived with `docker network inspect` from that network; the exact CIDRs Gitea may send webhooks to. Override only with exact, non-public CIDRs. |
| `E2E_COMPOSE_PROJECT` | `sentry-gitea-e2e`, the separate Gitea Compose project. |
| `GITEA_HTTP_PORT` | `13000`, always published on `127.0.0.1`. |
| `GITEA_URL` | `http://127.0.0.1:${GITEA_HTTP_PORT}`; the host and browser URL. |
| `GITEA_ADMIN_USERNAME` / `GITEA_ADMIN_EMAIL` | `e2e-admin` / `e2e-admin@example.invalid`. |
| `GITEA_OWNER` / `GITEA_REPO` | `e2e` / `demo`, the plugin fixture repository. |
| `SENTRY_URL` | `http://127.0.0.1:9000`. |
| `SENTRY_ADMIN_EMAIL` | `e2e-admin@example.invalid`, the bootstrap superuser. |
| `SENTRY_ORG` / `SENTRY_PROJECT` | `sentry` / `demo`. |
| `GITEA_WEBHOOK_URL_PREFIX` | `http://web:9000`; how Gitea reaches Sentry on the shared network. |
| `SENTRY_TEST_WORKERS` | `2`; only `1` or `2` accepted. |

### Run

From this repository, in this order. Bootstrap comes **before** install, because the installer
reads the OAuth client credentials that bootstrap creates:

```sh
make e2e-gitea-up
make e2e-bootstrap
make e2e-install
make e2e-test                            # smoke suite only
.venv/bin/pytest tests/e2e -m e2e -v -rs  # every E2E suite
```

Direct pytest does not read `.e2e/local.env`: export `SELF_HOSTED_DIR`, `DOCKER_CONFIG` and any
other overrides first. A single suite runs with, for example,
`.venv/bin/pytest tests/e2e/test_sync.py -m e2e -v -rs`. Rerun `make e2e-install` after every code
change, even when the version is unchanged, and rerun `make e2e-bootstrap` after harness changes.

**`make e2e-gitea-up`** starts `gitea/gitea:28.2.0` (SQLite, a named volume, no SSH) on Sentry's
Docker network under the aliases `gitea` and `gitea-unlisted`, published on
`127.0.0.1:${GITEA_HTTP_PORT}`. `ROOT_URL` is that browser URL, so Gitea's own links point at it,
while Sentry uses `http://gitea:3000`. Gitea's `[security] ALLOWED_HOST_LIST` is set to exactly
the subnet(s) of Sentry's network, so webhooks can reach `web:9000` but no private address
outside that network; a stale `[webhook] ALLOWED_HOST_LIST` from an older harness is removed from
the persistent `app.ini`. This still exposes every container on the self-hosted network to Gitea
webhooks, which is acceptable for a disposable test stack only; production installs should use a
dedicated network ([INTEGRATION.md](INTEGRATION.md#reaching-sentry-from-gitea)).
Compose also sets `ui.FEED_MAX_COMMIT_NUM=5` (the default the push-truncation case relies on),
`webhook.DELIVER_TIMEOUT=20`, `oauth2.ACCESS_TOKEN_EXPIRATION_TIME=90` seconds and
`oauth2.INVALIDATE_REFRESH_TOKENS=false`. Recreate Gitea with this target after harness changes;
starting an old stopped container does not apply new settings.

**`make e2e-bootstrap`** prepares fixtures and writes `.e2e/gitea.env` and `.e2e/sentry.env`
atomically with mode 0600. Existing users, tokens and repositories are reused.

- Gitea: the admin `e2e-admin` with an all-scopes token, and a separate plugin token with exactly
  `read:user`, `read:repository` and `write:issue` (verified through `GET /api/v1/token` before
  reuse). The private repository `e2e/demo` with an initial commit is the plugin fixture.
- Gitea, for the integration: the non-admin `e2e-user`, who owns `integration-demo` and
  `integration-z-default`, owns the organization `e2e-org`, and is a read-only team member of
  `e2e-member-org`. The confidential OAuth application `sentry-gitea-e2e` has the redirect URI
  `http://127.0.0.1:9000/extensions/gitea/setup/`. If its saved secret is lost, bootstrap
  regenerates it; rerun `make e2e-install` afterwards.
- Sentry: `sentry createuser` creates the bootstrap superuser, then `sentry exec` runs
  `docker/e2e/seed-sentry.py` in the web container. It ensures the default `sentry` organization
  (self-hosted runs in single-organization mode, and its HTML views serve only that organization
  to non-superusers), a team, the `demo` project and its key, a non-superuser organization owner
  `e2e-owner` for browser installs, and an organization-scoped API token with `org:read`,
  `org:integrations`, `project:admin`, `event:write` and `event:admin`. A stored token that lacks
  a scope is replaced. Credentials come back through a temporary 0600 file, never stdout.

**`make e2e-install`** builds the wheel, copies it into the self-hosted `sentry/` directory and
writes `# BEGIN sentry-gitea` / `# END sentry-gitea` blocks into `enhance-image.sh` and
`sentry.conf.py`, preserving everything else. The configuration block contains
[`docker/sentry.conf.py.snippet`](../docker/sentry.conf.py.snippet) plus, unless the snippet
already sets them, `GITEA_WEBHOOK_URL_PREFIX = "http://web:9000"`, the `TASKWORKER_IMPORTS` entry
and `GITEA_PLUGIN_ALLOWED_BASE_URLS = ("http://gitea:3000",)`, followed by the OAuth settings as
Python literals (`GITEA_BASE_URL`, `GITEA_PUBLIC_URL`, client ID and secret). Self-hosted's
Dockerfile copies `sentry/` into the image, so the OAuth values are left out while the `web` and
`sentry-cleanup` images build and written back afterwards, also on failure. The installer then
recreates every service that uses those images (web, taskworker, taskscheduler, consumers) and
restarts nginx. It prints `Extension installed` on success.

**`make e2e-test`** runs the smoke suite.

### Suites

| File | Cases | Covers |
| --- | --- | --- |
| `tests/e2e/test_smoke.py` | 6 | Sentry organization API; both plugins listed (skipped on Sentry 26.10, which removed the legacy plugin API); the Gitea provider listed with `canAdd: false` and its `externalInstall` link; the unauthenticated ping returns the package version; the Gitea API and fixture repository; the Sentry web container reaches `http://gitea:3000`. |
| `tests/e2e/test_plugins.py` | 10 | Skipped on Sentry 26.10, which removed the legacy plugin API. Configuration validation and token redaction; base URLs outside the allowlist (`http://gitea-unlisted:3000` and `http://169.254.169.254`) are refused, and changing the base URL without re-entering the token fails; an issue opened by another Gitea user with a valid metadata marker is not recovered (a new issue is filed and the planted one is untouched); sidebar create; link with backlink and unlink; alert filing and repeat comments through both the specific `gitea_alerts` action and the all-legacy action; closed issues are skipped; a Gitea outage is logged as `sentry-gitea: alert delivery failed` without credentials. Stored links must use the public Gitea URL. |
| `tests/e2e/test_integration.py` | 9 | OAuth denial with a redacted web log; personal install and owner choices; organization install and reinstall (same integration ID and `externalId`); repository and issue search for both owner kinds, excluding pull requests; create with labels, assignee and the remembered default repository; link, repeated link and unlink; access-token expiry and refresh through real OAuth endpoints; an issue carrying a valid marker but written by another Gitea user is not recovered (a new issue is filed, and a retried create reports the existing link). |
| `tests/e2e/test_sync.py` | 11 | Repository add, re-add and delete with exactly one hook at `.../webhook/<integration id>/<repository id>/`; webhook authentication (signatures, unknown integration, malformed payload, unknown repository); a delivery signed with one repository's secret is refused for any other repository (400 when the payload names another repository, 401 at another repository's URL); a 7-commit push truncated to 5 and backfilled, with Gitea's redelivery changing nothing; release commit ranges; status, comment and assignee sync; stack-trace links; email-only CODEOWNERS and rejection of `@handles`; a hook left at the old URL format is replaced when the repository is re-added, and Gitea's own Test Delivery then succeeds. |

The files also hold host-only regression tests (marker `not e2e`) for the helpers they use. Each
E2E file skips only when the credential files are missing; incomplete files, disabled features,
failed requests and behavior differences are failures. Created issues, labels, events,
integrations and Gitea repositories remain for inspection; plugin settings, alert rules, sync
options, code mappings and Sentry repositories are restored or deleted.

### How the suites drive the stack

- **Alert rules.** Plugin cases create rules with **"The event occurs"
  (`EveryEventCondition`) as the only trigger**, because Sentry 26's workflow engine drops it
  when combined with another trigger, plus per-case and per-delivery tag filters. Sentry enforces
  a 5-minute action interval, so every repeat delivery gets a fresh rule while the event keeps its
  fingerprint and group; this tests the plugin's deduplication, not Sentry's cooldown.
- **Pipeline nudge.** On an idle self-hosted instance, post-processing for the newest event
  waits for the next event. While a plugin case runs, a background sender posts a filler event
  every 4 seconds. Fillers share one fingerprint and carry no case tags, so no rule matches them.
- **Polls.** Every wait is bounded to 180 seconds. Transient connection errors from Sentry's API
  are retried within that budget.
- **Logs.** Plugin cases read recent `taskworker` output and integration cases read `web` output
  (both streams). The captured text is checked for every harness credential before it is
  filtered to `sentry-gitea:` lines, and raw logs are never printed.
- **Browser.** OAuth flows use `requests` and Python's HTML parser, not Selenium or JavaScript.
  Every redirect is followed manually with an origin check, form posts send an origin-only
  `Referer`, and Sentry and Gitea use separate cookie jars. Sentry 26.10 renders its login form in
  React, so the harness logs in like that page does: it loads `/auth/login/` and
  `/api/0/auth/config/` (which opens the session holding Django's test cookie), then posts JSON to
  `/api/0/auth/login/` with the `sc` CSRF cookie as `X-CSRFToken`. Gitea 28 has no hidden `_csrf`
  form fields; it relies on Go's cross-origin protection, so form posts send a same-origin
  `Origin` and `Sec-Fetch-Site: same-origin`. This verifies HTTP behavior, not the compiled
  frontend's React views or icons.
- **Token refresh.** With a 90-second access-token lifetime, the refresh case obtains a separate
  token for the same app and scopes, waits until Gitea rejects it, and then requires two searches
  to succeed, which proves that Sentry's own token expired and was refreshed. Refresh-token
  invalidation must stay off, or that probe would revoke Sentry's refresh token.
- **Webhooks.** Sync cases push with host `git`, passing the admin token as an origin-scoped
  `http.extraHeader` through `GIT_CONFIG_*` variables (never in argv, URLs or files). Synthetic
  deliveries are signed by a short `sentry exec` script in the web container that derives the
  per-repository secret and prints only signatures. Repository deletion is scheduled by Sentry;
  a `sentry exec` script runs exactly that repository's due deletion. Gitea 28 sends assignment
  changes as `X-Gitea-Event: issues` with `X-Gitea-Event-Type: issue_assign`.

### Idempotence and cleanup

Repeating `make e2e-bootstrap`, `make e2e-install` and the suites reuses users, repositories,
organizations, the OAuth app and tokens, and leaves one marked block per file.

```sh
make e2e-down       # stop and remove the Gitea containers; keeps its volume and credentials
make e2e-uninstall  # remove the marked blocks, rebuild and recreate Sentry services
```

`e2e-down` never touches self-hosted; bring Gitea back with `make e2e-gitea-up`. `e2e-uninstall`
prints `Extension removed`, keeps copied wheels, test data and credentials, and never runs
`down -v` against self-hosted volumes. After it, `/extensions/gitea/ping/` no longer answers.

With variables exported, the equivalent scripts are:

```sh
bash docker/e2e/install-into-self-hosted.sh
bash docker/e2e/bootstrap-gitea.sh
bash docker/e2e/bootstrap-sentry.sh
bash docker/e2e/install-into-self-hosted.sh uninstall
```

For an interactive shell in the stack, run
`docker compose --env-file .env --env-file .env.custom exec web sentry shell` from the
self-hosted checkout. The automated scripts use `exec -T web sentry ...` and never start a
second Sentry stack.
