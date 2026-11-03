import pytest
from harness import API, run, self_hosted_compose
from helpers import E2EEnvironment

from sentry_gitea import __version__

pytestmark = pytest.mark.e2e


def test_sentry_api(e2e: E2EEnvironment) -> None:
    api = API(e2e.sentry_url, e2e.sentry_token)
    result = api.request("GET", f"/api/0/organizations/{e2e.sentry_org}/")
    assert result["slug"] == e2e.sentry_org


def test_project_plugins(e2e: E2EEnvironment, legacy_plugin_api: None) -> None:
    api = API(e2e.sentry_url, e2e.sentry_token)
    result = api.request("GET", f"/api/0/projects/{e2e.sentry_org}/{e2e.sentry_project}/plugins/")
    assert {"gitea", "gitea_alerts"} <= {plugin["id"] for plugin in result}


def test_integration_provider(e2e: E2EEnvironment) -> None:
    api = API(e2e.sentry_url, e2e.sentry_token)
    result = api.request("GET", f"/api/0/organizations/{e2e.sentry_org}/config/integrations/")
    provider = next(item for item in result["providers"] if item["key"] == "gitea")
    assert provider["name"] == "Gitea"
    assert provider["canAdd"] is False
    assert provider["metadata"]["aspects"]["externalInstall"]["url"] == "/extensions/gitea/install/"


def test_public_extension_ping(e2e: E2EEnvironment) -> None:
    assert API(e2e.sentry_url).request("GET", "/extensions/gitea/ping/") == {
        "ok": True,
        "version": __version__,
    }


def test_gitea_api_and_initial_commit(e2e: E2EEnvironment) -> None:
    api = API(e2e.gitea_url, e2e.gitea_token, "token")
    assert api.request("GET", "/api/v1/user")["is_admin"] is True
    repo = api.request("GET", f"/api/v1/repos/{e2e.gitea_owner}/{e2e.gitea_repo}")
    assert repo["empty"] is False


def test_sentry_container_reaches_gitea(e2e: E2EEnvironment) -> None:
    # Use the running web container: no extra image, stack, or published port.
    program = (
        "import json, urllib.request; "
        "response = urllib.request.urlopen('http://gitea:3000/api/healthz', timeout=15); "
        "assert response.status == 200; "
        "assert json.load(response)['status'] == 'pass'"
    )
    run(
        self_hosted_compose() + ["exec", "-T", "web", "python", "-c", program],
        label="Sentry container to Gitea health check",
        timeout=30,
    )
