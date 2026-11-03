"""Host regressions for interrupted/denied OAuth and credential diagnostics."""

import json
import secrets
from urllib.parse import urlencode

import pytest
import responses
from browser import CALLBACK, SCOPES, Browser
from helpers import E2EEnvironment, IntegrationEnvironment


@pytest.fixture
def settings() -> IntegrationEnvironment:
    stack = E2EEnvironment(
        sentry_url="http://sentry.test",
        sentry_token=secrets.token_urlsafe(32),
        sentry_org="e2e",
        sentry_project="demo",
        sentry_dsn="http://public@sentry.test/1",
        gitea_url="http://gitea.test",
        gitea_internal_url="http://gitea:3000",
        gitea_token=secrets.token_urlsafe(32),
        gitea_owner="e2e",
        gitea_repo="demo",
        gitea_plugin_token=secrets.token_urlsafe(32),
    )
    return IntegrationEnvironment(
        stack=stack,
        sentry_admin_email="e2e-admin@example.invalid",
        sentry_admin_password=secrets.token_urlsafe(32),
        sentry_owner_username="e2e-owner",
        sentry_owner_password=secrets.token_urlsafe(32),
        gitea_e2e_username="e2e-user",
        gitea_e2e_password=secrets.token_urlsafe(32),
        gitea_e2e_repo="integration-demo",
        gitea_e2e_default_repo="integration-z-default",
        gitea_e2e_org="e2e-org",
        gitea_e2e_member_org="e2e-member-org",
        gitea_oauth_app_id="7",
        gitea_client_id="public-client-id",
        gitea_client_secret=secrets.token_urlsafe(32),
    )


OWNER_FORM = (
    '<form method="post" action="/extensions/gitea/setup/">'
    '<input name="csrfmiddlewaretoken" value="test-csrf">'
    '<select name="owner"><option value="user:2">e2e-user (user)</option></select></form>'
)
DENIAL = "<title>Setup Error</title><p>Gitea authorization failed</p>"


def oauth_redirects(
    settings, *, method="S256", denied=False, with_login=False, callback_state=None
):
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": settings.gitea_client_id,
        "redirect_uri": settings.stack.sentry_url + CALLBACK,
        "scope": SCOPES,
        "response_type": "code",
        "state": callback_state or state,
        "code_challenge": "A" * 43,
        "code_challenge_method": method,
    }
    authorize = settings.stack.gitea_url + "/login/oauth/authorize?" + urlencode(params)
    callback = (
        settings.stack.sentry_url
        + CALLBACK
        + "?"
        + urlencode(
            {
                "state": state,
                **({"error": "access_denied"} if denied else {"code": secrets.token_urlsafe(32)}),
            }
        )
    )
    responses.get(
        settings.stack.sentry_url + "/extensions/gitea/install/?organization=e2e",
        status=302,
        headers={"Location": authorize},
    )
    if with_login:
        responses.get(authorize, status=303, headers={"Location": "/user/login"})
        responses.get(
            settings.stack.gitea_url + "/user/login",
            body=(
                '<form method="post" action="/user/login"><input name="user_name">'
                '<input name="password" type="password"></form>'
            ),
        )
        # Gitea can complete an existing confidential grant directly after login.
        responses.post(
            settings.stack.gitea_url + "/user/login", status=303, headers={"Location": callback}
        )
    elif denied:
        responses.get(
            authorize,
            body=(
                '<form method="post" action="/login/oauth/grant">'
                f'<input name="client_id" value="{settings.gitea_client_id}">'
                f'<input name="state" value="{state}"><input name="scope" value="{SCOPES}">'
                f'<input name="redirect_uri" value="{settings.stack.sentry_url + CALLBACK}">'
                '<input name="nonce" value=""><button name="granted" value="false">Cancel</button>'
                '<button name="granted" value="true">Authorize</button></form>'
            ),
        )
        responses.post(
            settings.stack.gitea_url + "/login/oauth/grant",
            status=303,
            headers={"Location": callback},
        )
    else:
        responses.get(authorize, status=303, headers={"Location": callback})
    responses.get(callback, body=DENIAL if denied else OWNER_FORM)
    return state


@responses.activate
def test_reset_keeps_sentry_session_and_discards_revocation_login_cookie(settings, monkeypatch):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    before = (browser.sentry, browser.gitea)
    browser.sentry.cookies.set("sessionid", "authenticated-owner")
    browser.sentry_logged_in = True
    browser.gitea.cookies.set("redirect_to", "old-authorization")
    called = []

    def revoke(env):
        assert not browser.gitea.cookies
        browser.gitea.cookies.set("session", "temporary-revocation-login")
        called.append("revoke")

    monkeypatch.setattr(browser, "revoke_grant", revoke)
    browser.reset(settings)
    assert called == ["revoke"]
    assert browser.sentry is before[0] and browser.gitea is not before[1]
    assert browser.sentry.cookies.get("sessionid") == "authenticated-owner"
    assert browser.sentry_logged_in
    assert not browser.gitea.cookies
    assert not responses.calls
    browser.close()


@responses.activate
@pytest.mark.parametrize("stale", [OWNER_FORM, DENIAL, "You can safely close this window now."])
def test_recognizable_stale_pipeline_restarts_once_and_still_validates_oauth(
    settings, monkeypatch, stale
):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    setup = settings.stack.sentry_url + "/extensions/gitea/install/?organization=e2e"
    responses.get(setup, body=stale)
    resets = []
    monkeypatch.setattr(browser, "reset", lambda env: resets.append(True))
    state = oauth_redirects(settings, with_login=True)
    page = browser.owner_page(settings)
    assert page.form("owner").choices["owner"] == {"user:2": "e2e-user (user)"}
    assert browser.saw_authorize
    assert not resets, "Stale recovery must only restart setup, preserving authentication"
    assert any(
        secret.kind == "oauth state" and secret.value == state
        for secret in browser.private_credentials
    )
    browser.close()


@responses.activate
def test_stale_pipeline_recurrence_fails_after_one_reset(settings, monkeypatch):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    responses.get(
        settings.stack.sentry_url + "/extensions/gitea/install/?organization=e2e", body=OWNER_FORM
    )
    resets = []
    monkeypatch.setattr(browser, "reset", lambda env: resets.append(True))
    with pytest.raises(pytest.fail.Exception, match="after a clean restart"):
        browser.owner_page(settings)
    assert not resets
    assert len(responses.calls) == 2
    browser.close()


@responses.activate(assert_all_requests_are_fired=False)
def test_pkce_failure_is_never_retried_or_treated_as_stale_pipeline(settings, monkeypatch):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    resets = []
    monkeypatch.setattr(browser, "reset", lambda env: resets.append(True))
    oauth_redirects(settings, method="plain")
    with pytest.raises(pytest.fail.Exception, match="S256 PKCE"):
        browser.owner_page(settings)
    assert not resets
    browser.close()


@responses.activate
def test_callback_state_mismatch_is_not_retried(settings, monkeypatch):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    resets = []
    monkeypatch.setattr(browser, "reset", lambda env: resets.append(True))
    wrong_state = secrets.token_urlsafe(32)
    state = oauth_redirects(settings, callback_state=wrong_state)
    with pytest.raises(pytest.fail.Exception, match="callback state") as failure:
        browser.owner_page(settings)
    assert not resets
    assert state not in str(failure.value) and wrong_state not in str(failure.value)
    browser.close()


@responses.activate
def test_browser_body_leak_reports_kind_path_and_status(settings):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    responses.get(
        settings.stack.sentry_url + "/auth/login/", status=500, body=settings.gitea_client_secret
    )
    with pytest.raises(pytest.fail.Exception) as failure:
        browser.login_sentry(settings)
    message = str(failure.value)
    assert "oauth client secret" in message
    assert "response GET /auth/login/ HTTP 500" in message
    assert settings.gitea_client_secret not in message
    browser.close()


@responses.activate
def test_non_oauth_query_state_is_not_collected_as_a_secret(settings):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    url = settings.stack.gitea_url + "/explore/repos?state=open"
    responses.get(url, body="Repositories")
    browser.request("GET", url)
    assert not browser.private_credentials
    browser.close()


@responses.activate
def test_denial_then_restore_uses_a_new_authorization_state(settings, monkeypatch):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    browser.response_credentials = settings.credentials
    denied_state = oauth_redirects(settings, denied=True)
    browser.owner_page(settings, deny=True)
    # Simulate the interrupted nested pipeline when the same session is reused.
    responses.get(
        settings.stack.sentry_url + "/extensions/gitea/install/?organization=e2e", body=DENIAL
    )
    resets = []
    monkeypatch.setattr(browser, "reset", lambda env: resets.append(True))
    restored_state = oauth_redirects(settings)
    assert browser.owner_page(settings).form("owner").choices
    assert denied_state != restored_state
    assert not resets
    assert browser.saw_authorize
    browser.close()


def test_credential_registry_contains_only_explicit_secret_fields(settings):
    credentials = settings.credentials
    assert {item.kind for item in credentials} == {
        "gitea admin token",
        "gitea plugin token",
        "sentry token",
        "sentry admin password",
        "sentry owner password",
        "e2e user password",
        "oauth client secret",
    }
    nonsecrets = {
        settings.sentry_admin_email,
        settings.sentry_owner_username,
        settings.gitea_e2e_username,
        settings.gitea_e2e_org,
        settings.gitea_e2e_member_org,
        settings.gitea_client_id,
        settings.stack.sentry_url,
        settings.stack.gitea_url,
    }
    assert not nonsecrets.intersection(item.value for item in credentials)


# Sentry 26.10 renders its login form in React; the server page carries no form.
LOGIN_PAGE = '<div id="blk_router"></div>'


def sentry_login(settings, *, user=None, member=None):
    root = settings.stack.sentry_url
    responses.get(
        root + "/auth/login/",
        body=LOGIN_PAGE,
        headers={"Set-Cookie": "sc=test-csrf; Path=/"},
    )
    responses.get(root + "/api/0/auth/config/", json={})
    responses.post(
        root + "/api/0/auth/login/",
        json={"nextUri": "/organizations/e2e/issues/", "user": {"id": "2"}},
        headers={"Set-Cookie": "sessionid=authenticated-owner; Path=/"},
    )
    identity = {
        "id": "2",
        "username": settings.sentry_owner_username,
        "isActive": True,
        "isSuperuser": False,
        "isStaff": False,
    }
    responses.get(root + "/api/0/users/me/", json=identity | (user or {}))
    responses.get(
        root + "/api/0/organizations/e2e/members/me/",
        json={"orgRole": "owner", "teams": ["e2e"], "user": identity} | (member or {}),
    )


@responses.activate
def test_module_browser_logs_in_once_as_owner_across_eight_installs(settings, monkeypatch):
    from conftest import integration_browser

    sentry_login(settings)
    logout = settings.stack.sentry_url + "/auth/logout/"
    responses.get(
        logout, body='<form method="post"><input name="csrfmiddlewaretoken" value="csrf"></form>'
    )
    responses.post(logout, status=302, headers={"Location": "/auth/login/"})
    lifetime = integration_browser.__wrapped__(settings)
    browser = next(lifetime)
    session = browser.sentry
    revocations = []

    def revoke(env):
        assert not browser.gitea.cookies
        browser.gitea.cookies.set("session", "revocation-only")
        revocations.append(True)

    monkeypatch.setattr(browser, "revoke_grant", revoke)
    try:
        for _ in range(8):
            browser.reset(settings)
            assert not browser.gitea.cookies
            assert browser.sentry is session
            assert session.cookies.get("sessionid") == "authenticated-owner"
            oauth_redirects(settings)
            browser.owner_page(settings)
    finally:
        lifetime.close()
    posts = [call.request for call in responses.calls if call.request.method == "POST"]
    posts = [request for request in posts if "/auth/login/" in request.url]
    assert [request.url for request in posts] == [settings.stack.sentry_url + "/api/0/auth/login/"]
    assert json.loads(posts[0].body) == {
        "username": settings.sentry_owner_username,
        "password": settings.sentry_owner_password,
    }
    assert posts[0].headers["X-CSRFToken"] == "test-csrf"
    assert len(revocations) == 8
    assert not browser.sentry_logged_in


@responses.activate(assert_all_requests_are_fired=False)
@pytest.mark.parametrize(
    ("user", "member", "message"),
    [
        ({"isSuperuser": True}, {}, "non-superuser"),
        ({"isStaff": True}, {}, "non-superuser"),
        ({"isActive": False}, {}, "non-superuser"),
        ({"username": "different-user"}, {}, "non-superuser"),
        ({}, {"orgRole": "member"}, "organization owner"),
        ({}, {"teams": []}, "organization owner"),
        ({}, {"user": {"id": "99"}}, "organization owner"),
    ],
)
def test_browser_verifies_non_superuser_owner_before_oauth(settings, user, member, message):
    sentry_login(settings, user=user, member=member)
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    try:
        with pytest.raises(pytest.fail.Exception, match=message):
            browser.login_sentry(settings)
    finally:
        browser.close()


@responses.activate
@pytest.mark.parametrize(
    ("status", "message"),
    [
        (200, "You have made too many login attempts. Please try again later."),
        (200, "You have made too many failed authentication attempts. Please try again later."),
        (429, "Too many requests"),
    ],
)
def test_sentry_login_rate_limit_is_explicit_and_never_retried(settings, status, message):
    from conftest import integration_browser

    root = settings.stack.sentry_url
    responses.get(
        root + "/auth/login/",
        body=LOGIN_PAGE,
        headers={"Set-Cookie": "sc=login-form; Path=/"},
    )
    responses.get(root + "/api/0/auth/config/", json={})
    responses.post(root + "/api/0/auth/login/", status=status, json={"detail": message})
    lifetime = integration_browser.__wrapped__(settings)
    with pytest.raises(pytest.fail.Exception, match="Sentry login rate-limited"):
        next(lifetime)
    # An unauthenticated CSRF cookie must not trigger logout or mask the diagnosis.
    assert len(responses.calls) == 3


@responses.activate
def test_gitea_login_429_is_explicit(settings):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    responses.get(settings.stack.gitea_url + "/user/login", status=429)
    try:
        with pytest.raises(pytest.fail.Exception, match="Gitea login rate-limited"):
            browser.login_gitea(settings)
    finally:
        browser.close()


@responses.activate
@pytest.mark.parametrize("rate_limited", [True, False])
def test_setup_login_redirect_has_specific_diagnostic(settings, rate_limited):
    browser = Browser(settings.stack.sentry_url, settings.stack.gitea_url)
    root = settings.stack.sentry_url
    responses.get(
        root + "/extensions/gitea/install/?organization=e2e",
        status=302,
        headers={"Location": "/auth/login/"},
    )
    responses.get(
        root + "/auth/login/",
        body=LOGIN_PAGE
        + (
            "You have made too many login attempts. Please try again later." if rate_limited else ""
        ),
    )
    message = "Sentry login rate-limited" if rate_limited else "Sentry session is not authenticated"
    try:
        with pytest.raises(pytest.fail.Exception, match=message):
            browser.owner_page(settings)
        assert len(responses.calls) == 2
    finally:
        browser.close()
