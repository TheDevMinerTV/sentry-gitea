# sentry-gitea — instructions for coding agents

Open-source (Apache-2.0) Gitea integration for **self-hosted Sentry 26.10.0**, targeting
**Gitea 28.1.0**. It is installed into the Sentry image with pip (self-hosted
`sentry/enhance-image.sh`). Sentry is NOT a pip dependency: it is provided by the host image
`ghcr.io/getsentry/sentry:26.10.0`. The package has two modes, the **plugins** and the
**integration**; use those names in code, docs and messages.

Human contributors follow the same rules: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Hard rules

1. **Licensing (most important).** Sentry 26.x is FSL-1.1 (source-available, not open source).
   - You may read Sentry 26.x's source to learn interfaces, base classes, method signatures and
     behavior. You must NOT copy or closely paraphrase code from it.
   - The only Sentry code you may adapt is from **Sentry 24.10.0, or a release you verified is
     already Apache-2.0** (FSL releases convert two years after publication). When you adapt
     anything, add the upstream path to the "Adapted files" list in `NOTICE` and keep a short
     `# Adapted from sentry <version>: <path>` comment.
   - Never copy from the archived `getsentry/sentry-plugins` repository (BSL-1.1).
   - Gitea (MIT) is a reference for API and webhook shapes.
2. **No secrets** in code, tests, fixtures, docs or logs. Tokens come from configuration; never log
   them, and never put credentials, private host names or machine-specific paths in the repository.
3. **Do not commit, push or open pull requests unless asked.** Leave changes in the working tree
   for the person you are working with.

## Layout

```
src/sentry_gitea/
  meta.py            project URLs and author constants
  api/client.py      pure Gitea REST client (requests; timeouts; pagination and byte budgets)
  api/webhooks.py    X-Gitea-Signature verification (hmac.compare_digest), payload parsing
  render.py          issue title/body/labels/severity: pure functions, no Sentry imports
  plugins/issue.py   GiteaIssuePlugin(IssuePlugin2)        (entry point sentry.plugins: gitea)
  plugins/alerts.py  GiteaAlertsPlugin(NotificationPlugin) (entry point sentry.plugins: gitea_alerts)
  integration/       IntegrationProvider and installation, identity, issues, sync, repositories,
                     webhooks, tasks, source links
  urls.py            ROOT_URLCONF shim: our /extensions/gitea/ routes + include("sentry.conf.urls")
tests/unit/          pure tests, run on the host; must not import sentry
tests/sentry/        run inside the Sentry image with Postgres + Redis (sentry.testutils, responses)
tests/e2e/           real self-hosted Sentry (errors-only profile) + gitea/gitea:28.1.0
fixtures/            our own stub package (sentry.testutils adds "fixtures" to INSTALLED_APPS)
docker/              enhance-image.sh, sentry.conf.py snippet, compose files, E2E harness
```

Modules under `api/` and `render.py` must stay importable without Sentry so unit tests run anywhere.

## Commands

- Unit tests: `make unit` (or `.venv/bin/pytest tests/unit -q`).
- Lint and format: `make lint` (`ruff check .` and `ruff format --check .`; ruff also formats
  Python code blocks in Markdown).
- Harness tests: `.venv/bin/pytest tests/e2e -m "not e2e" -q`.
- Sentry-level tests: `make sentry-tests` (Docker). End-to-end: see `docs/TESTING.md`; it needs a
  dedicated self-hosted Sentry and must not run alongside another heavy workload.

## Conventions

- Python 3.12+ syntax, type hints, ruff-clean (line length 100).
- Every HTTP call has a timeout and a response size bound. Gitea errors surface as exceptions with a
  status and a short category, never with the token or upstream response bodies in user-facing
  messages. Sentry swallows legacy-plugin exceptions, so plugins log failures themselves (with IDs
  and status, without exception text that could contain credentials) before re-raising.
- Plugin base URLs are operator-untrusted: only origins in `GITEA_PLUGIN_ALLOWED_BASE_URLS` may
  reach private addresses; everything else goes through Sentry's outbound HTTP guard.
- Text derived from events is rendered so it cannot create Gitea @mentions or links. Hidden
  markers (`sentry-gitea-meta`, `sentry-gitea-link`, `sentry-gitea-note`) are trusted only when
  written by our own Gitea user.
- Gitea quirks: `POST /repos/{o}/{r}/issues` takes label **IDs**; assignees are **usernames**;
  push webhooks truncate `commits[]` to 5 (use `total_commits` and the commits API); there is no
  blame API; hook secrets cannot be changed through the API.
- Tests first for every behavior, in the lowest layer that can prove it. Mock HTTP with
  `responses`.
- When behavior or configuration changes, update `README.md` and the matching file in `docs/`.

## Reporting

End every task with: files changed, tests added and their results, commands you could not run (and
why), and open questions or risks.
