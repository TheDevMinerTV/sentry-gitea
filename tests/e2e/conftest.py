import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# The same host-only helpers drive the scripts and their regression tests.
sys.path.insert(0, str(ROOT / "docker/e2e"))

from browser import Browser  # noqa: E402
from flows import PluginFlow  # noqa: E402
from harness import API, read_env  # noqa: E402
from helpers import E2EEnvironment, IntegrationEnvironment  # noqa: E402


@pytest.fixture(scope="session")
def e2e() -> E2EEnvironment:
    paths = [ROOT / ".e2e/sentry.env", ROOT / ".e2e/gitea.env"]
    missing = [str(path.relative_to(ROOT)) for path in paths if not path.exists()]
    if missing:
        pytest.skip(f"E2E credentials missing: {', '.join(missing)}; run make e2e-bootstrap")
    values = read_env(paths[0]) | read_env(paths[1]) | dict(os.environ)
    required = [
        "SENTRY_URL",
        "SENTRY_TOKEN",
        "SENTRY_ORG",
        "SENTRY_PROJECT",
        "SENTRY_DSN",
        "GITEA_URL",
        "GITEA_INTERNAL_URL",
        "GITEA_TOKEN",
        "GITEA_PLUGIN_TOKEN",
        "GITEA_OWNER",
        "GITEA_REPO",
    ]
    absent = [name for name in required if not values.get(name)]
    if absent:
        pytest.fail(f"Incomplete E2E env files: {', '.join(absent)}; rerun make e2e-bootstrap")
    return E2EEnvironment(**{name.lower(): values[name].rstrip("/") for name in required})


@pytest.fixture(scope="session")
def legacy_plugin_api(e2e: E2EEnvironment) -> None:
    path = f"/api/0/projects/{e2e.sentry_org}/{e2e.sentry_project}/plugins/"
    if API(e2e.sentry_url, e2e.sentry_token).request("GET", path, expected=(200, 404)) is None:
        pytest.skip("This Sentry has no legacy plugin API (removed in 26.10); plugins need 26.3.1")


@pytest.fixture
def plugin_flow(e2e: E2EEnvironment, legacy_plugin_api: None) -> Iterator[PluginFlow]:
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("Plugin E2E changes shared project configuration; run without xdist")
    flow = PluginFlow(e2e)
    try:
        flow.configure()
        yield flow
    finally:
        flow.close()


@pytest.fixture(scope="session")
def integration_env(e2e: E2EEnvironment) -> IntegrationEnvironment:
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("Integration E2E shares OAuth grants and project defaults; run without xdist")
    paths = [ROOT / ".e2e/sentry.env", ROOT / ".e2e/gitea.env"]
    values = read_env(paths[0]) | read_env(paths[1])
    required = [
        "SENTRY_ADMIN_EMAIL",
        "SENTRY_ADMIN_PASSWORD",
        "SENTRY_OWNER_USERNAME",
        "SENTRY_OWNER_PASSWORD",
        "GITEA_E2E_USERNAME",
        "GITEA_E2E_PASSWORD",
        "GITEA_E2E_REPO",
        "GITEA_E2E_DEFAULT_REPO",
        "GITEA_E2E_ORG",
        "GITEA_E2E_MEMBER_ORG",
        "GITEA_OAUTH_APP_ID",
        "GITEA_CLIENT_ID",
        "GITEA_CLIENT_SECRET",
    ]
    absent = [name for name in required if not values.get(name)]
    if absent:
        pytest.fail(
            f"Incomplete integration E2E env: {', '.join(absent)}; rerun make e2e-bootstrap"
        )
    for path in paths:
        if path.stat().st_mode & 0o077:
            pytest.fail(f"Credential file {path.name} must have mode 0600; rerun bootstrap")
    if e2e.sentry_url != "http://127.0.0.1:9000" or e2e.gitea_internal_url != "http://gitea:3000":
        pytest.fail("Integration E2E requires the seeded Sentry and internal Gitea URLs")
    return IntegrationEnvironment(e2e, **{name.lower(): values[name] for name in required})


@pytest.fixture(scope="module")
def integration_browser(integration_env: IntegrationEnvironment) -> Iterator[Browser]:
    # Sentry counts successful logins toward its five-attempts-per-minute limit.
    # Test isolation resets Gitea and restarts setup, retaining this owner session.
    browser = Browser(integration_env.stack.sentry_url, integration_env.stack.gitea_url)
    try:
        browser.login_sentry(integration_env)
        yield browser
    finally:
        try:
            browser.logout_sentry()
        finally:
            browser.close()
