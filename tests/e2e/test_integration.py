"""Real integration flows plus host-only regressions for their browser/bootstrap helpers."""

import ast
import base64
import hashlib
import json
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
import responses
from browser import Browser
from flows import META, PluginFlow, check, poll
from harness import API, bootstrap_gitea_integration, ensure_gitea_oauth_app, read_env, write_env
from helpers import IntegrationEnvironment, recent_oauth_logs
from redaction import Credential


def test_oauth_settings_are_python_literals_and_require_complete_credentials() -> None:
    from harness import integration_settings

    values = {
        "GITEA_INTERNAL_URL": "http://gitea:3000",
        "GITEA_URL": "http://127.0.0.1:13000",
        "GITEA_CLIENT_ID": "test-client",
        "GITEA_CLIENT_SECRET": "quotes'\\\" and backslashes\\",
    }
    assignments = ast.parse(integration_settings(values)).body
    parsed = {node.targets[0].id: ast.literal_eval(node.value) for node in assignments}
    assert parsed == {
        "GITEA_BASE_URL": values["GITEA_INTERNAL_URL"],
        "GITEA_PUBLIC_URL": values["GITEA_URL"],
        "GITEA_CLIENT_ID": values["GITEA_CLIENT_ID"],
        "GITEA_CLIENT_SECRET": values["GITEA_CLIENT_SECRET"],
    }
    assert integration_settings({}) == ""
    with pytest.raises(RuntimeError, match="bootstrap"):
        integration_settings({"GITEA_CLIENT_ID": "incomplete"})


@pytest.mark.parametrize("build_fails", [False, True])
def test_installer_keeps_oauth_secret_out_of_image_context(tmp_path, monkeypatch, build_fails):
    import harness

    directory = tmp_path / "sentry"
    directory.mkdir()
    settings = directory / "sentry.conf.py"
    settings.write_text("# operator settings\n")
    mode = stat.S_IMODE(settings.stat().st_mode)
    monkeypatch.setattr(harness, "STATE", tmp_path / ".e2e")
    monkeypatch.setenv("SELF_HOSTED_DIR", str(tmp_path))
    monkeypatch.setattr(harness, "active_profiles", lambda: set())
    monkeypatch.setattr(
        harness,
        "compose_config",
        lambda: {
            "services": {
                "web": {"image": "sentry-self-hosted-local"},
                "sentry-cleanup": {"image": "sentry-cleanup-self-hosted-local"},
            }
        },
    )
    values = {
        "GITEA_URL": "http://127.0.0.1:13000",
        "GITEA_INTERNAL_URL": "http://gitea:3000",
        "GITEA_CLIENT_ID": "test-client",
        "GITEA_CLIENT_SECRET": "private-build-test-value",
    }
    write_env(harness.STATE / "gitea.env", values)

    def fake_build(path):
        wheel = path / "sentry_gitea-test.whl"
        wheel.touch()
        return wheel

    def fake_run(command, **kwargs):
        if "ps" in command:
            return "web\nsentry-cleanup\n"
        if "build" in command:
            assert values["GITEA_CLIENT_SECRET"] not in settings.read_text()
            if build_fails:
                raise RuntimeError("test build failure")
        if "--force-recreate" in command:
            assert values["GITEA_CLIENT_SECRET"] in settings.read_text()
        return ""

    monkeypatch.setattr(harness, "build_wheel", fake_build)
    monkeypatch.setattr(harness, "run", fake_run)
    for _ in range(2):
        if build_fails:
            with pytest.raises(RuntimeError, match="test build failure"):
                harness.install()
        else:
            harness.install()
        assert values["GITEA_CLIENT_SECRET"] in settings.read_text()
        assert settings.read_text().count(harness.BEGIN) == 1
        assert stat.S_IMODE(settings.stat().st_mode) == mode


@responses.activate
def test_browser_submits_rendered_fields_and_rejects_foreign_form_actions() -> None:
    from browser import Browser, Page

    browser = Browser("http://sentry.test", "http://gitea.test")
    page = Page(
        "http://sentry.test/auth/login/",
        '<form method="post"><input name="csrfmiddlewaretoken" value="test-csrf">'
        '<input name="op" value="login"><input name="username">'
        '<input name="password" type="password"></form>',
    )
    responses.post("http://sentry.test/auth/login/", body="signed in")
    browser.submit(page, page.form("username"), {"username": "e2e", "password": "test-value"})
    request = responses.calls[0].request
    assert parse_qs(request.body)["csrfmiddlewaretoken"] == ["test-csrf"]
    assert request.headers["Origin"] == "http://sentry.test"
    assert request.headers["Referer"] == "http://sentry.test/"
    assert browser.sentry is not browser.gitea
    bad = Page(
        "http://gitea.test/user/login",
        '<form method="post" action="https://untrusted.test/"><input name="user_name"></form>',
    )
    with pytest.raises(RuntimeError, match="origin"):
        browser.submit(bad, bad.form("user_name"), {"password": "test-value"})
    browser.close()


@responses.activate
def test_gitea_grant_preserves_hidden_fields_and_cancel_button_without_legacy_csrf() -> None:
    from browser import Browser, Page

    browser = Browser("http://sentry.test", "http://gitea.test")
    page = Page(
        "http://gitea.test/login/oauth/authorize",
        '<form method="post" action="/login/oauth/grant">'
        '<input name="client_id" value="test-client"><input name="state" value="test-state">'
        '<input name="scope" value="read:user"><input name="nonce" value="">'
        '<input name="redirect_uri" value="http://sentry.test/extensions/gitea/setup/">'
        '<button name="granted" value="true">Authorize</button>'
        '<button name="granted" value="false">Cancel</button></form>',
    )
    responses.post("http://gitea.test/login/oauth/grant", body="cancelled")
    browser.submit(page, page.form("client_id"), {"granted": "false"})
    request = responses.calls[0].request
    fields = parse_qs(request.body, keep_blank_values=True)
    assert fields["granted"] == ["false"]
    assert fields["state"] == ["test-state"]
    assert fields["nonce"] == [""]
    assert "_csrf" not in fields
    assert request.headers["Sec-Fetch-Site"] == "same-origin"
    browser.close()


@responses.activate
def test_browser_errors_never_include_callback_queries_or_response_bodies() -> None:
    from browser import Browser

    browser = Browser("http://sentry.test", "http://gitea.test")
    url = "http://sentry.test/extensions/gitea/setup/?code=private-code&state=private-state"
    responses.get(url, body="private-body", status=500)
    with pytest.raises(RuntimeError) as error:
        browser.request("GET", url)
    assert str(error.value) == "Browser GET /extensions/gitea/setup/: HTTP 500; content withheld"
    browser.close()


@responses.activate
def test_integration_bootstrap_reuses_private_credentials_and_non_admin_fixtures(
    tmp_path, monkeypatch
):
    import harness

    monkeypatch.setattr(harness, "STATE", tmp_path)
    url = "http://gitea.test"
    write_env(
        tmp_path / "gitea.env",
        {
            "GITEA_URL": url,
            "GITEA_TOKEN": "test-admin-token",
            "GITEA_PLUGIN_TOKEN": "test-plugin-token",
        },
    )
    user = {"login": "e2e-user", "is_admin": False}
    responses.get(url + "/api/v1/users/e2e-user", status=404)
    responses.get(url + "/api/v1/users/e2e-user", json=user)
    responses.post(url + "/api/v1/admin/users", json=user, status=201)
    responses.patch(url + "/api/v1/admin/users/e2e-user", json=user)

    def identity(request):
        identity = {"login": "e2e-admin", "is_admin": True}
        if request.headers["Authorization"].startswith("Basic "):
            identity = user
        return 200, {"Content-Type": "application/json"}, json.dumps(identity)

    responses.add_callback("GET", url + "/api/v1/user", callback=identity)
    for org, path in (("e2e-org", "/admin/users/e2e-user/orgs"), ("e2e-member-org", "/orgs")):
        responses.get(url + f"/api/v1/orgs/{org}", status=404)
        responses.get(url + f"/api/v1/orgs/{org}", json={"username": org})
        responses.post(url + "/api/v1" + path, json={"username": org}, status=201)
        responses.get(url + f"/api/v1/orgs/{org}/members/e2e-user", status=204)
        responses.get(
            url + f"/api/v1/users/e2e-user/orgs/{org}/permissions",
            json={"can_read": org == "e2e-org", "is_owner": org == "e2e-org"},
        )
    responses.get(url + "/api/v1/orgs/e2e-org/teams", json=[{"id": 1, "name": "Owners"}])
    responses.put(url + "/api/v1/teams/1/members/e2e-user", status=204)
    responses.get(url + "/api/v1/orgs/e2e-member-org/teams", json=[])
    responses.get(
        url + "/api/v1/orgs/e2e-member-org/teams", json=[{"id": 2, "name": "e2e-members"}]
    )
    responses.post(
        url + "/api/v1/orgs/e2e-member-org/teams",
        json={"id": 2, "name": "e2e-members"},
        status=201,
        match=[
            responses.matchers.json_params_matcher(
                {
                    "name": "e2e-members",
                    "permission": "read",
                    "units_map": {"repo.code": "read", "repo.issues": "read"},
                    "includes_all_repositories": True,
                }
            )
        ],
    )
    responses.patch(url + "/api/v1/teams/2", json={"id": 2, "name": "e2e-members"})
    responses.put(url + "/api/v1/teams/2/members/e2e-user", status=204)
    for repo in (
        "e2e-user/integration-demo",
        "e2e-user/integration-z-default",
        "e2e-org/integration-demo",
        "e2e-member-org/integration-demo",
    ):
        responses.get(url + f"/api/v1/repos/{repo}", status=404)
        responses.get(url + f"/api/v1/repos/{repo}", json={"empty": False})
        owner = repo.split("/")[0]
        path = "/api/v1/user/repos" if owner == "e2e-user" else f"/api/v1/orgs/{owner}/repos"
        responses.post(url + path, json={"empty": False}, status=201)
    app = {
        "id": 7,
        "client_id": "test-client",
        "client_secret": "test-client-secret",
        "name": "sentry-gitea-e2e",
        "confidential_client": True,
        "redirect_uris": ["http://127.0.0.1:9000/extensions/gitea/setup/"],
        "skip_secondary_authorization": False,
    }
    responses.get(url + "/api/v1/user/applications/oauth2", json=[])
    responses.get(url + "/api/v1/user/applications/oauth2", json=[app | {"client_secret": ""}])
    responses.post(url + "/api/v1/user/applications/oauth2", json=app, status=201)
    bootstrap_gitea_integration()
    first = read_env(tmp_path / "gitea.env")
    bootstrap_gitea_integration()
    assert read_env(tmp_path / "gitea.env") == first
    assert stat.S_IMODE((tmp_path / "gitea.env").stat().st_mode) == 0o600
    assert first["GITEA_PLUGIN_TOKEN"] == "test-plugin-token"
    assert sum(call.request.method == "POST" for call in responses.calls) == 9


@responses.activate
def test_lost_oauth_secret_rotates_existing_app_instead_of_creating_duplicate(
    tmp_path, monkeypatch
):
    import harness

    monkeypatch.setattr(harness, "STATE", tmp_path)
    url = "http://gitea.test"
    app = {"id": 7, "name": "sentry-gitea-e2e", "client_id": "same-client"}
    responses.get(url + "/api/v1/user/applications/oauth2", json=[app])
    responses.patch(
        url + "/api/v1/user/applications/oauth2/7", json=app | {"client_secret": "new-test-secret"}
    )
    values = {"GITEA_CLIENT_ID": "same-client"}
    ensure_gitea_oauth_app(API(url), values)
    assert values["GITEA_CLIENT_SECRET"] == "new-test-secret"
    assert values["GITEA_OAUTH_APP_ID"] == "7"
    assert [call.request.method for call in responses.calls] == ["GET", "PATCH"]


@pytest.mark.parametrize("logger", ["sentry_gitea.integration.identity", "sentry.superuser"])
def test_web_log_leaks_are_rejected_before_filtering(monkeypatch, logger) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout=(
                "sentry-gitea: oauth failed\n"
                f"web | 2026-10-29T00:00:00Z [WARNING] {logger}: private-test-value\n"
            ),
        ),
    )
    with pytest.raises(pytest.fail.Exception) as failure:
        recent_oauth_logs(
            "2026-10-29T00:00:00Z", (Credential("oauth state", "private-test-value"),)
        )
    assert str(failure.value) == (
        f"E2E credential exposure: oauth state; log service=web logger={logger}; content withheld"
    )


class IntegrationFlow(PluginFlow):
    """Reuse ingestion/polling only; never configure plugins or start the alert nudger."""

    def __init__(self, env: IntegrationEnvironment, browser: Browser) -> None:
        super().__init__(env.stack)
        self.settings = env
        self.release = "sentry-gitea-e2e@0.2"
        self.credentials = env.credentials
        for api in (self.sentry, self.gitea, self.plugin_gitea):
            api.credentials = self.credentials
        self.browser = browser
        self.browser.private_credentials.update(self.credentials)
        user = self.gitea.request("GET", f"/api/v1/users/{env.gitea_e2e_username}")
        check(user["is_admin"] is False, "OAuth E2E must use a non-admin Gitea user")
        org = self.gitea.request("GET", f"/api/v1/orgs/{env.gitea_e2e_org}")
        self.owners = {"user": f"user:{user['id']}", "org": f"org:{org['id']}"}
        self.names = {"user": env.gitea_e2e_username, "org": env.gitea_e2e_org}

    def installations(self) -> list[dict]:
        return self.sentry.request(
            "GET", self.settings.integrations_path, params={"provider_key": "gitea"}
        )

    def external_id(self, kind: str) -> str:
        return hashlib.sha256(
            f"v1|{self.env.gitea_internal_url}|{self.owners[kind].replace(':', '|')}".encode()
        ).hexdigest()

    def install_owner(self, kind: str) -> str:
        return self.browser.install(self.settings, self.owners[kind], self.sentry)

    def repository(self, kind: str = "user", *, default: bool = False) -> str:
        name = self.settings.gitea_e2e_default_repo if default else self.settings.gitea_e2e_repo
        return f"{self.names[kind]}/{name}"

    def search(self, integration: str, field: str, query: str, *, repo: str = "", expected=(200,)):
        params = {"field": field, "query": query}
        if repo:
            params["repo"] = repo
        return self.sentry.request(
            "GET",
            f"/extensions/gitea/search/{self.env.sentry_org}/{integration}/",
            params=params,
            expected=expected,
        )

    def integration_path(self, integration: str, group: dict) -> str:
        return f"{self.group_path(group)}integrations/{integration}/"

    def fields(self, integration: str, group: dict, action: str, **params) -> dict:
        result = self.sentry.request(
            "GET", self.integration_path(integration, group), params={"action": action, **params}
        )
        key = f"{action}IssueConfig"
        check(isinstance(result.get(key), list), f"Sentry did not expose {key}")
        return {field["name"]: field for field in result[key]}

    def links(self, integration: str, group: dict) -> list[dict]:
        rows = self.sentry.request("GET", f"{self.group_path(group)}integrations/")
        matches = [row for row in rows if str(row["id"]) == integration]
        check(len(matches) == 1, "Installed Gitea owner is absent from the group integrations API")
        return matches[0]["externalIssues"]

    def assert_external_issue(self, integration: str, group: dict, repo: str, issue: dict) -> None:
        repository = self.gitea.request("GET", f"/api/v1/repos/{repo}")
        key = f"{repository['id']}#{issue['number']}"
        matches = [row for row in self.links(integration, group) if row["key"] == key]
        check(len(matches) == 1, "Sentry must show exactly one link with the canonical issue key")
        assert matches[0]["displayName"] == f"{repo}#{issue['number']}"
        assert matches[0]["url"] == issue["html_url"]
        assert issue["html_url"].startswith(self.env.gitea_url + "/")

    def new_group(self) -> dict:
        self.case_id = uuid4().hex
        self.message = f"sentry-gitea-integration-e2e-{self.case_id}"
        return self.send_and_wait()

    @contextmanager
    def pull_request(self, repo: str) -> Iterator[dict]:
        branch = f"e2e-{uuid4().hex}"
        root = f"/api/v1/repos/{repo}"
        self.gitea.request(
            "POST",
            f"{root}/contents/{branch}.txt",
            expected=(201,),
            json={
                "branch": "main",
                "new_branch": branch,
                "message": "E2E PR fixture",
                "content": base64.b64encode(b"E2E PR exclusion\n").decode(),
            },
        )
        pull = None
        try:
            pull = self.gitea.request(
                "POST",
                f"{root}/pulls",
                expected=(201,),
                json={"head": branch, "base": "main", "title": f"{self.message} pull request"},
            )
            yield pull
        finally:
            if pull is not None:
                self.gitea.request(
                    "PATCH",
                    f"{root}/pulls/{pull['number']}",
                    expected=(201,),
                    json={"state": "closed"},
                )
            self.gitea.request("DELETE", f"{root}/branches/{branch}", expected=(204,))


@pytest.fixture
def integration_flow(
    integration_env: IntegrationEnvironment, integration_browser: Browser
) -> Iterator[IntegrationFlow]:
    flow = IntegrationFlow(integration_env, integration_browser)
    try:
        flow.browser.reset(integration_env)
        yield flow
    finally:
        flow.close()


@pytest.mark.e2e
class TestIntegration:
    def test_oauth_denial_and_redacted_web_log(
        self,
        integration_flow: IntegrationFlow,
        request: pytest.FixtureRequest,
    ) -> None:
        flow = integration_flow
        before = {row["externalId"]: str(row["id"]) for row in flow.installations()}
        since = datetime.now(UTC).isoformat()

        def restore() -> None:
            # Keep the authenticated owner session; each install starts setup again.
            # Restoration in teardown cannot replace the redaction diagnosis.
            flow.browser.reset(flow.settings, revoke=False)
            # Revocation affects prior runs too. Restore every managed owner that
            # was already installed, without creating new integrations after denial.
            for kind in ("user", "org"):
                if flow.external_id(kind) in before:
                    restored = flow.install_owner(kind)
                    assert restored == before[flow.external_id(kind)]

        request.addfinalizer(restore)
        # The fixture revoked the managed grant and discarded its login cookie.
        flow.browser.owner_page(flow.settings, deny=True)
        after = {row["externalId"]: str(row["id"]) for row in flow.installations()}
        assert after == before, "Denied OAuth changed the installed integrations"
        credentials = tuple(flow.browser.private_credentials)
        logs = poll("Sentry's OAuth denial warning", lambda: recent_oauth_logs(since, credentials))
        check(
            not re.search(
                r"\b(?:code|state|access_token|refresh_token|client_secret)\s*[=:]", logs
            ),
            "OAuth warning includes a sensitive field; log service=web; content withheld",
        )

    def test_install_user_and_owner_choices(self, integration_flow: IntegrationFlow) -> None:
        flow = integration_flow
        env = flow.settings
        for org, owner in ((env.gitea_e2e_org, True), (env.gitea_e2e_member_org, False)):
            permission = flow.gitea.request(
                "GET", f"/api/v1/users/{env.gitea_e2e_username}/orgs/{org}/permissions"
            )
            flow.gitea.request(
                "GET", f"/api/v1/orgs/{org}/members/{env.gitea_e2e_username}", expected=(204,)
            )
            assert permission["is_owner"] is owner
        integration = flow.install_owner("user")
        assert flow.browser.owner_choices == {
            flow.owners["user"]: f"{env.gitea_e2e_username} (user)",
            flow.owners["org"]: f"{env.gitea_e2e_org} (org)",
        }
        row = next(row for row in flow.installations() if str(row["id"]) == integration)
        assert row["name"] == env.gitea_e2e_username
        assert row["provider"]["key"] == "gitea"
        assert row["externalId"] == flow.external_id("user")

    def test_install_org_and_reinstall(self, integration_flow: IntegrationFlow) -> None:
        flow = integration_flow
        first = flow.install_owner("org")
        assert flow.install_owner("org") == first
        rows = [row for row in flow.installations() if row["externalId"] == flow.external_id("org")]
        assert len(rows) == 1
        assert rows[0]["name"] == flow.settings.gitea_e2e_org

    @pytest.mark.parametrize("kind", ["user", "org"])
    def test_search_owner_repos_and_exclude_prs(
        self, integration_flow: IntegrationFlow, kind: str
    ) -> None:
        flow = integration_flow
        integration = flow.install_owner(kind)
        repo = flow.repository(kind)
        expected_repos = {repo}
        if kind == "user":
            expected_repos.add(flow.repository(default=True))
        choices = flow.search(integration, "repo", "integration")
        assert {row["value"] for row in choices} == expected_repos
        assert all(row["label"] == row["value"] for row in choices)
        repository = flow.gitea.request("GET", f"/api/v1/repos/{repo}")
        issue = flow.gitea.request(
            "POST",
            f"/api/v1/repos/{repo}/issues",
            expected=(201,),
            json={"title": f"{flow.message} issue"},
        )
        with flow.pull_request(repo) as pull:
            # Prove this fixture is also returned by Gitea's issue endpoint.
            raw = flow.gitea.request("GET", f"/api/v1/repos/{repo}/issues/{pull['number']}")
            check(raw.get("pull_request") is not None, "PR fixture was not a Gitea pull request")
            found = flow.search(integration, "externalIssue", str(issue["number"]), repo=repo)
            assert found == [
                {
                    "label": f"#{issue['number']} {issue['title']}",
                    "value": f"{repository['id']}#{issue['number']}",
                }
            ]
            assert flow.search(integration, "externalIssue", str(pull["number"]), repo=repo) == []
            assert flow.search(integration, "externalIssue", flow.message, repo=repo) == found
        # The authorizing user can access these namespaces, but this install cannot.
        wrong_repo = flow.repository("org" if kind == "user" else "user")
        error = flow.search(integration, "externalIssue", "1", repo=wrong_repo, expected=(400,))
        assert "repo" in error

    def test_create_labels_display_and_project_default(
        self, integration_flow: IntegrationFlow
    ) -> None:
        flow = integration_flow
        integration = flow.install_owner("user")
        first_group = flow.new_group()
        first_repo = flow.repository()
        # Establish a different persisted value on every run, including reruns.
        flow.sentry.request(
            "POST",
            flow.integration_path(integration, first_group),
            expected=(201,),
            json={"repo": first_repo, "title": f"Baseline {flow.message}", "description": ""},
        )
        group = flow.new_group()
        assert flow.fields(integration, group, "create")["repo"]["default"] == first_repo
        repo = flow.repository(default=True)
        fields = flow.fields(integration, group, "create", repo=repo)
        check(
            {"repo", "title", "description", "labels", "assignee"} <= fields.keys(),
            "Create form omitted required integration fields",
        )
        assert fields["repo"]["default"] == repo
        assert group["shortId"] in fields["title"]["default"]
        assert group["permalink"] in fields["description"]["default"]
        title = fields["title"]["default"] + " edited by E2E"
        body = fields["description"]["default"] + "\n\nEdited E2E description."
        existing_label = f"e2e-existing:{flow.case_id}"
        new_label = f"e2e-new:{flow.case_id}"
        label = flow.gitea.request(
            "POST",
            f"/api/v1/repos/{repo}/labels",
            expected=(201,),
            json={"name": existing_label, "color": "4477aa"},
        )
        linked = flow.sentry.request(
            "POST",
            flow.integration_path(integration, group),
            expected=(201,),
            json={
                "repo": repo,
                "title": title,
                "description": body,
                "labels": f"{existing_label}, {new_label}",
                "assignee": flow.settings.gitea_e2e_username,
            },
        )
        number = linked["key"].split("#")[1]
        issue = flow.gitea.request("GET", f"/api/v1/repos/{repo}/issues/{number}")
        assert issue["title"] == title
        assert issue["body"] == body
        assert {item["name"] for item in issue["labels"]} == {existing_label, new_label}
        assert label["id"] in {item["id"] for item in issue["labels"]}
        assert {item["login"] for item in issue["assignees"]} == {flow.settings.gitea_e2e_username}
        assert linked["displayName"] == f"{repo}#{number}"
        flow.assert_external_issue(integration, group, repo, issue)
        # A different group in the same project must now inherit the non-first repo.
        assert flow.fields(integration, first_group, "create")["repo"]["default"] == repo

    def test_create_recovers_only_the_installations_own_marker(
        self, integration_flow: IntegrationFlow
    ) -> None:
        """Idempotent create trusts a metadata marker only on the installation user's issues."""
        flow = integration_flow
        integration = flow.install_owner("user")
        group = flow.new_group()
        repo = flow.repository()
        root = f"/api/v1/repos/{repo}"
        fields = flow.fields(integration, group, "create", repo=repo)
        description = fields["description"]["default"]
        marker = META.search(description)
        check(marker is not None, "Create form description lacks the sentry-gitea-meta marker")
        dedup_id = json.loads(marker[1])["dedup_id"]

        def marked(body: str | None) -> bool:
            found = META.search(body or "")
            try:
                return bool(found) and json.loads(found[1]).get("dedup_id") == dedup_id
            except ValueError:
                return False

        def searchable(number: int) -> bool:
            # The marker search the integration itself runs before creating.
            rows = flow.pages(root + "/issues", state="all", type="issues", q=dedup_id)
            return any(row["number"] == number for row in rows)

        # A different Gitea user who can write here (the site admin) copies the marker.
        planted = flow.gitea.request(
            "POST",
            root + "/issues",
            expected=(201,),
            json={"title": f"Planted {flow.message}", "body": description},
        )
        try:
            check(
                planted["user"]["login"] != flow.settings.gitea_e2e_username,
                "The planted issue must not be authored by the installation user",
            )
            poll(
                "Gitea's issue search to return the planted marker issue",
                lambda: searchable(planted["number"]),
                seconds=90,
            )
            data = {"repo": repo, "title": fields["title"]["default"], "description": description}
            path = flow.integration_path(integration, group)
            linked = flow.sentry.request("POST", path, expected=(201,), json=data)
            number = int(linked["key"].split("#")[1])
            assert number != planted["number"], "Integration adopted another user's issue"
            issue = flow.gitea.request("GET", f"{root}/issues/{number}")
            assert issue["user"]["login"] == flow.settings.gitea_e2e_username
            flow.assert_external_issue(integration, group, repo, issue)
            untouched = flow.gitea.request("GET", f"{root}/issues/{planted['number']}")
            assert untouched["body"] == planted["body"] and untouched["state"] == "open"
            # Control: once searchable, a retried create recovers the installation's own
            # issue (same key, so Sentry reports it as already linked) instead of a duplicate.
            poll(
                "Gitea's issue search to return the created issue",
                lambda: searchable(number),
                seconds=90,
            )
            retried = flow.sentry.request("POST", path, expected=(400,), json=data)
            assert retried == {"detail": "That issue is already linked"}
            own = [
                row["number"]
                for row in flow.pages(root + "/issues", state="all", type="issues", q=dedup_id)
                if row["user"]["login"] == flow.settings.gitea_e2e_username
                and marked(row.get("body"))
            ]
            assert own == [number], "A retried create filed a duplicate issue"
        finally:
            flow.gitea.request(
                "PATCH",
                f"{root}/issues/{planted['number']}",
                expected=(201,),
                json={"state": "closed"},
            )

    def test_link_repeat_and_unlink(self, integration_flow: IntegrationFlow) -> None:
        flow = integration_flow
        integration = flow.install_owner("user")
        group = flow.new_group()
        repo = flow.repository()
        root = f"/api/v1/repos/{repo}"
        repository = flow.gitea.request("GET", root)
        issue = flow.gitea.request(
            "POST", root + "/issues", expected=(201,), json={"title": f"Existing {flow.message}"}
        )
        fields = flow.fields(integration, group, "link", repo=repo)
        marker = f"<!-- sentry-gitea-link {group['id']} -->"
        assert marker in fields["comment"]["default"]
        assert group["permalink"] in fields["comment"]["default"]
        check(bool(fields.get("linkContext", {}).get("default")), "Link form has no signed context")
        data = {
            "repo": repo,
            "externalIssue": f"{repository['id']}#{issue['number']}",
            "comment": f"E2E backlink: {group['permalink']}",
            "linkContext": fields["linkContext"]["default"],
        }
        path = flow.integration_path(integration, group)
        linked = flow.sentry.request("PUT", path, expected=(201,), json=data)
        comments_path = f"{root}/issues/{issue['number']}/comments"
        # Gitea 28.1.0 returns the entire comment list; it ignores page/limit here.
        comments = flow.gitea.request("GET", comments_path)
        assert len(comments) == 1
        assert comments[0]["body"].count(marker) == 1
        assert data["comment"] in comments[0]["body"]
        flow.assert_external_issue(integration, group, repo, issue)
        # Sentry 26.10 answers a repeated link of the same issue with 200 instead of 400.
        duplicate = flow.sentry.request("PUT", path, expected=(200,), json=data)
        assert (duplicate["id"], duplicate["key"]) == (linked["id"], linked["key"])
        assert flow.gitea.request("GET", comments_path) == comments
        flow.sentry.request("DELETE", path, expected=(204,), params={"externalIssue": linked["id"]})
        assert not any(row["key"] == linked["key"] for row in flow.links(integration, group))
        remaining = flow.gitea.request("GET", f"{root}/issues/{issue['number']}")
        assert remaining["id"] == issue["id"]
        assert remaining["state"] == "open"
        assert flow.gitea.request("GET", comments_path) == comments

    def test_expired_access_token_refreshes(self, integration_flow: IntegrationFlow) -> None:
        flow = integration_flow
        integration = flow.install_owner("user")
        before = flow.search(integration, "repo", "integration")
        check(bool(before), "Refresh baseline repository search returned no choices")
        # No Sentry requests until this later-issued JWT has been rejected as expired.
        flow.browser.wait_for_later_token_expiry(flow.settings)
        assert flow.search(integration, "repo", "integration") == before
        # A subsequent independent request must work too. DB persistence itself is
        # covered by the Sentry-level tests; public APIs do not expose Identity.data.
        assert flow.search(integration, "repo", "integration") == before
