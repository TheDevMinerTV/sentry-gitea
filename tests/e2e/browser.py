"""Small requests-only browser for the real Sentry/Gitea HTML forms.

Original test code, informed by upstream interfaces only. No JavaScript is executed.
Never include response bodies, redirect queries, cookies, or form values in failures.
"""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from flows import TransientError, check, poll, poll_budget, without_credentials
from helpers import IntegrationEnvironment
from redaction import Credential

SCOPES = "read:user read:organization write:issue write:repository"
CALLBACK = "/extensions/gitea/setup/"
LOGIN_API = "/api/0/auth/login/"
# Sentry names its CSRF cookie "sc", not Django's default "csrftoken".
SENTRY_CSRF_COOKIE = "sc"
REDIRECTS = (301, 302, 303, 307, 308)
SENTRY_LOGIN_LIMIT_ERRORS = (
    "You have made too many login attempts. Please try again later.",
    "You have made too many failed authentication attempts. Please try again later.",
)


class StalePipeline(Exception):
    """A recognizable pre-OAuth Sentry page can receive one fresh setup request."""


@dataclass(repr=False)
class Form:
    action: str
    method: str
    fields: dict[str, str] = field(default_factory=dict)
    choices: dict[str, dict[str, str]] = field(default_factory=dict)
    buttons: list[tuple[str, str]] = field(default_factory=list)


class Page(HTMLParser):
    def __init__(self, url: str, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.forms: list[Form] = []
        self.actions: list[str] = []
        self.text: list[str] = []
        self.current: Form | None = None
        self.select = self.option = ""
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if action := attributes.get("data-url"):
            self.actions.append(action)
        if tag == "form":
            self.current = Form(attributes.get("action") or "", attributes.get("method", "get"))
            self.forms.append(self.current)
        if self.current is None:
            return
        name = attributes.get("name")
        if tag == "input" and name and "disabled" not in attributes:
            if attributes.get("type") not in {"submit", "button", "checkbox", "radio"}:
                self.current.fields[name] = attributes.get("value") or ""
            elif "checked" in attributes:
                self.current.fields[name] = attributes.get("value") or "on"
            if attributes.get("type") == "checkbox":
                self.current.choices.setdefault(name, {})[attributes.get("value") or "on"] = ""
        elif tag == "button" and name:
            self.current.buttons.append((name, attributes.get("value") or ""))
        elif tag == "select" and name:
            self.select = name
            self.current.choices[name] = {}
        elif tag == "option" and self.select:
            self.option = attributes.get("value") or ""
            self.current.choices[self.select][self.option] = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None
        elif tag == "select":
            self.select = self.option = ""
        elif tag == "option":
            self.option = ""

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self.current and self.select and self.option:
            self.current.choices[self.select][self.option] += data

    def form(self, field_name: str) -> Form:
        matches = [
            form for form in self.forms if field_name in form.fields or field_name in form.choices
        ]
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one browser form containing {field_name}")
        return matches[0]

    def contains(self, text: str) -> bool:
        return text in " ".join(self.text)


def origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise RuntimeError("Browser URL must not contain credentials")
    return f"{parts.scheme}://{parts.netloc}"


class Browser:
    def __init__(self, sentry_url: str, gitea_url: str) -> None:
        self.sentry_url = sentry_url.rstrip("/")
        self.gitea_url = gitea_url.rstrip("/")
        # Cookies ignore ports. Sharing a jar would let two 127.0.0.1 apps collide.
        self.sentry = requests.Session()
        self.gitea = requests.Session()
        self.private_credentials: set[Credential] = set()
        self.response_credentials: tuple[Credential, ...] = ()
        self.sentry_logged_in = False
        self.saw_authorize = False
        self.saw_consent = False
        self.owner_choices: dict[str, str] = {}

    def close(self) -> None:
        self.sentry.close()
        self.gitea.close()
        self.sentry.cookies.clear()
        self.gitea.cookies.clear()
        self.sentry_logged_in = False

    def remember(self, kind: str, value: str) -> None:
        if value:
            self.private_credentials.add(Credential(kind, value))

    def logout_sentry(self) -> None:
        """Flush both pipeline pointers through Sentry's CSRF-protected logout form."""
        if not self.sentry_logged_in:
            return
        response = self.follow(self.request("GET", self.sentry_url + "/auth/logout/"))
        page = Page(response.url, response.text)
        form = page.form("csrfmiddlewaretoken")
        check(bool(form.fields["csrfmiddlewaretoken"]), "Sentry logout has no CSRF token")
        response = self.submit(page, form, {})
        check(response.status_code in (302, 303), "Sentry logout did not flush the session")
        self.sentry.cookies.clear()
        self.sentry_logged_in = False

    def reset(self, env: IntegrationEnvironment, *, revoke: bool = True) -> None:
        """Isolate Gitea grants/cookies while keeping the module's Sentry login.

        owner_page starts a new organization setup pipeline on every attempt.
        Successful and denied OAuth clear their nested identity state themselves.
        """
        check(self.sentry_logged_in, "Sentry module browser must be authenticated before reset")
        self.response_credentials = env.credentials
        self.private_credentials.update(env.credentials)
        self.gitea.close()
        self.gitea.cookies.clear()
        self.gitea = requests.Session()
        if revoke:
            try:
                self.revoke_grant(env)
            finally:
                # Revocation authenticates Gitea. Install must still exercise its
                # login form with a fresh jar and no old redirect_to cookie.
                self.gitea.close()
                self.gitea.cookies.clear()
                self.gitea = requests.Session()

    def request(self, method: str, url: str, *, expected=(200, *REDIRECTS), **kwargs):
        host = origin(url)
        if host not in (origin(self.sentry_url), origin(self.gitea_url)):
            raise RuntimeError("Browser redirect/form left the configured origin; URL withheld")
        path = urlsplit(url).path
        if (host == origin(self.gitea_url) and path == "/login/oauth/authorize") or (
            host == origin(self.sentry_url) and path == CALLBACK
        ):
            params = parse_qs(urlsplit(url).query)
            for key, kind in (("code", "oauth authorization code"), ("state", "oauth state")):
                for value in params.get(key, []):
                    self.remember(kind, value)
        session = self.sentry if host == origin(self.sentry_url) else self.gitea
        try:
            response = session.request(
                method, url, timeout=poll_budget(15), allow_redirects=False, **kwargs
            )
        except (requests.ConnectionError, requests.Timeout):
            # A poll (e.g. Gitea hook history) may retry this; other callers fail as before.
            raise TransientError(f"Browser {method} {path} failed; content withheld") from None
        except requests.RequestException:
            raise RuntimeError(f"Browser {method} {path} failed; content withheld") from None
        # OAuth forms intentionally contain state; only static credentials are
        # forbidden in browser HTML. All observed secrets are still checked in logs.
        without_credentials(
            response.text,
            self.response_credentials,
            where=f"response {method} {path} HTTP {response.status_code}",
        )
        if host == origin(self.sentry_url) and (
            path.startswith("/auth/login/") or path == LOGIN_API
        ):
            page = Page(response.url, response.text)
            check(
                response.status_code != 429
                and not any(page.contains(message) for message in SENTRY_LOGIN_LIMIT_ERRORS),
                "Sentry login rate-limited",
            )
        if host == origin(self.gitea_url) and path == "/user/login":
            check(response.status_code != 429, "Gitea login rate-limited")
        if response.status_code not in expected:
            raise RuntimeError(
                f"Browser {method} {path}: HTTP {response.status_code}; content withheld"
            )
        return response

    def follow(self, response: requests.Response) -> requests.Response:
        for _ in range(20):
            if response.status_code not in REDIRECTS:
                return response
            if not response.headers.get("Location"):
                raise RuntimeError("Browser redirect has no Location")
            response = self.request("GET", urljoin(response.url, response.headers["Location"]))
        raise RuntimeError("Browser exceeded 20 redirects")

    def submit(self, page: Page, form: Form, values: dict[str, Any]) -> requests.Response:
        url = urljoin(page.url, form.action)
        if origin(url) != origin(page.url) or form.method.lower() != "post":
            raise RuntimeError("Browser expected a POST form on the same origin")
        # Both apps receive the origin-only Referer, never a callback code/state query.
        headers = {
            "Origin": origin(page.url),
            "Referer": origin(page.url) + "/",
            "Sec-Fetch-Site": "same-origin",
        }
        return self.request("POST", url, data=form.fields | values, headers=headers)

    def login_sentry(self, env: IntegrationEnvironment) -> None:
        """Log in like Sentry 26.10's React login page: JSON to the login API, CSRF from cookie."""
        self.response_credentials = env.credentials
        self.private_credentials.update(env.credentials)
        self.follow(self.request("GET", self.sentry_url + "/auth/login/"))
        # The page's auth-config call opens the session that carries Django's test cookie.
        self.request("GET", self.sentry_url + "/api/0/auth/config/", expected=(200,))
        csrf = self.sentry.cookies.get(SENTRY_CSRF_COOKIE)
        check(bool(csrf), "Sentry login set no CSRF cookie")
        response = self.request(
            "POST",
            self.sentry_url + LOGIN_API,
            expected=(200,),
            json={"username": env.sentry_owner_username, "password": env.sentry_owner_password},
            headers={
                "X-CSRFToken": csrf,
                "Origin": origin(self.sentry_url),
                "Referer": origin(self.sentry_url) + "/",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        try:
            signed_in = bool(response.json().get("user"))
        except ValueError:
            signed_in = False
        check(signed_in, "Sentry browser login failed")
        self.sentry_logged_in = True
        self.verify_sentry_owner(env)

    def _authenticated_me(self) -> requests.Response | None:
        response = self.request("GET", self.sentry_url + "/api/0/users/me/", expected=(200, 401))
        return response if response.status_code == 200 else None

    def verify_sentry_owner(self, env: IntegrationEnvironment) -> None:
        """Check the cookie-authenticated identity before sending any OAuth callbacks."""
        # Right after a logout elsewhere, Sentry 26.10 can answer a fresh session's first
        # requests with 401 for a few seconds.
        response = poll("Sentry accepted the new session", self._authenticated_me, seconds=30)
        try:
            user = response.json()
        except ValueError:
            raise RuntimeError(
                "Sentry browser identity response was not JSON; content withheld"
            ) from None
        check(
            isinstance(user, dict)
            and bool(user.get("id"))
            and user.get("username") == env.sentry_owner_username
            and user.get("isSuperuser") is False
            and user.get("isStaff") is False
            and user.get("isActive") is True,
            "Sentry browser must be the seeded active non-superuser owner; rerun bootstrap",
        )
        response = self.request(
            "GET",
            self.sentry_url + f"/api/0/organizations/{env.stack.sentry_org}/members/me/",
            expected=(200,),
        )
        try:
            member = response.json()
        except ValueError:
            raise RuntimeError(
                "Sentry browser membership response was not JSON; content withheld"
            ) from None
        check(
            isinstance(member, dict)
            and member.get("orgRole") == "owner"
            and isinstance(member.get("user"), dict)
            and member["user"].get("id") == user["id"]
            and isinstance(member.get("teams"), list)
            and "e2e" in member["teams"],
            "Sentry browser must be an organization owner on the e2e team; rerun bootstrap",
        )

    def login_gitea(self, env: IntegrationEnvironment) -> None:
        response = self.follow(self.request("GET", self.gitea_url + "/user/login"))
        page = Page(response.url, response.text)
        if any("user_name" in form.fields for form in page.forms):
            response = self.follow(self.gitea_login(page, env))
        check(urlsplit(response.url).path != "/user/login", "Gitea browser login failed")

    def gitea_login(self, page: Page, env: IntegrationEnvironment) -> requests.Response:
        check(origin(page.url) == origin(self.gitea_url), "Gitea login appeared on wrong origin")
        # 28.1.0 removed _csrf; Go CrossOriginProtection validates the headers instead.
        return self.submit(
            page,
            page.form("user_name"),
            {
                "user_name": env.gitea_e2e_username,
                "password": env.gitea_e2e_password,
            },
        )

    def owner_page(self, env: IntegrationEnvironment, *, deny: bool = False) -> Page:
        for attempt in range(2):
            try:
                return self._owner_page(env, deny=deny)
            except StalePipeline:
                check(attempt == 0, "Setup resumed a stale Sentry pipeline after a clean restart")
                # A stale identity at its callback fails and clears both pipelines;
                # one at completion clears its nested state before showing owners.
                # Restart only setup: logging in again consumes Sentry's login quota.
        raise AssertionError("Unreachable browser restart state")

    def _owner_page(self, env: IntegrationEnvironment, *, deny: bool) -> Page:
        self.saw_authorize = self.saw_consent = False
        self.owner_choices = {}
        expected_state = ""
        saw_callback = False
        response = self.request(
            "GET",
            self.sentry_url + f"/extensions/gitea/install/?organization={env.stack.sentry_org}",
        )
        for _ in range(30):
            if urlsplit(response.url).path == "/login/oauth/authorize":
                check(
                    origin(response.url) == origin(self.gitea_url),
                    "OAuth did not use the public Gitea origin",
                )
                params = parse_qs(urlsplit(response.url).query)
                check(params.get("client_id") == [env.gitea_client_id], "Wrong OAuth client")
                check(
                    params.get("redirect_uri") == [self.sentry_url + CALLBACK],
                    "Wrong OAuth callback URI",
                )
                check(
                    params.get("code_challenge_method") == ["S256"]
                    and bool(params.get("code_challenge"))
                    and len(params.get("state", [])) == 1
                    and bool(params["state"][0]),
                    "OAuth authorize omitted state or S256 PKCE",
                )
                check(params.get("scope") == [SCOPES], "Unexpected OAuth scopes")
                check(params.get("response_type") == ["code"], "Unexpected OAuth response type")
                check(
                    not expected_state or params["state"] == [expected_state],
                    "OAuth state changed while following Gitea login",
                )
                expected_state = params["state"][0]
                self.saw_authorize = True
            if (
                origin(response.url) == origin(self.sentry_url)
                and urlsplit(response.url).path == CALLBACK
            ):
                callback = parse_qs(urlsplit(response.url).query)
                if any(key in callback for key in ("code", "state", "error")):
                    check(
                        self.saw_authorize and callback.get("state") == [expected_state],
                        "OAuth callback state does not match this install attempt",
                    )
                    if deny:
                        check(
                            callback.get("error") == ["access_denied"] and "code" not in callback,
                            "OAuth cancellation did not return access_denied",
                        )
                    else:
                        check(
                            len(callback.get("code", [])) == 1 and "error" not in callback,
                            "OAuth callback omitted a single authorization code",
                        )
                    saw_callback = True
            if response.status_code in REDIRECTS:
                response = self.request("GET", urljoin(response.url, response.headers["Location"]))
                continue
            page = Page(response.url, response.text)
            if origin(page.url) == origin(self.gitea_url):
                if any("user_name" in form.fields for form in page.forms):
                    response = self.gitea_login(page, env)
                    continue
                form = page.form("client_id")
                check(
                    ("granted", "false" if deny else "true") in form.buttons,
                    "Gitea consent form has no expected submit button",
                )
                check(
                    form.fields.get("client_id") == env.gitea_client_id,
                    "Gitea consent form has the wrong OAuth client",
                )
                check(
                    form.fields.get("state") == expected_state
                    and form.fields.get("redirect_uri") == self.sentry_url + CALLBACK,
                    "Gitea consent form changed state or callback URI",
                )
                self.saw_consent = True
                response = self.submit(page, form, {"granted": "false" if deny else "true"})
                continue
            check(
                not urlsplit(page.url).path.startswith("/auth/login/")
                and not any("username" in form.fields for form in page.forms),
                "Sentry session is not authenticated; setup redirected to login",
            )
            if not self.saw_authorize and (
                any("owner" in form.choices for form in page.forms)
                or page.contains("Setup Error")
                or page.contains("You can safely close this window now.")
            ):
                raise StalePipeline
            check(self.saw_authorize, "Setup never reached Gitea's public authorize endpoint")
            check(saw_callback, "Setup never received the OAuth callback for this attempt")
            if deny:
                check(self.saw_consent, "Gitea reused a grant; denial did not reach consent")
                check(
                    page.contains("Setup Error") and page.contains("Gitea authorization failed"),
                    "Sentry did not display the OAuth denial error",
                )
                check(
                    not any("owner" in form.choices for form in page.forms),
                    "Denied authorization reached owner selection",
                )
            else:
                form = page.form("owner")
                check(bool(form.fields.get("csrfmiddlewaretoken")), "Owner form has no CSRF token")
                check(
                    urlsplit(page.url).path == CALLBACK
                    and urljoin(page.url, form.action) == self.sentry_url + CALLBACK,
                    "Owner form must submit to the generic Gitea setup URL",
                )
                self.owner_choices = form.choices["owner"]
            return page
        raise RuntimeError("Gitea install exceeded its bounded browser steps")

    def install(
        self,
        env: IntegrationEnvironment,
        owner: str,
        api,
        repositories: list[str] | None = None,
    ) -> str:
        """Install `owner`, selecting `repositories` (Gitea IDs; all offered ones by default)."""
        page = self.owner_page(env)
        check(owner in self.owner_choices, "Requested owner was not offered by Sentry")
        response = self.follow(self.submit(page, page.form("owner"), {"owner": owner}))
        picker = Page(response.url, response.text)
        forms = [form for form in picker.forms if "csrfmiddlewaretoken" in form.fields]
        check(len(forms) == 1, "Sentry did not show the repository picker")
        offered = list(forms[0].choices.get("repository", {}))
        chosen = offered if repositories is None else repositories
        check(set(chosen) <= set(offered), "Requested repositories were not offered by Sentry")
        response = self.follow(self.submit(picker, forms[0], {"repository": chosen}))
        check(
            Page(response.url, response.text).contains("You can safely close this window now."),
            "Sentry did not finish the integration setup dialog",
        )
        # The dialog shows the same closing text on failure; surface Sentry's own error.
        failure = re.search(r'"success":\s*false.*?"error":\s*"([^"]{1,300})"', response.text, re.S)
        check(failure is None, f"Sentry setup failed: {failure.group(1) if failure else ''}")
        expected = hashlib.sha256(
            f"v1|{env.stack.gitea_internal_url}|{owner.replace(':', '|')}".encode()
        ).hexdigest()
        rows = api.request("GET", env.integrations_path, params={"provider_key": "gitea"})
        matches = [row for row in rows if row["externalId"] == expected]
        check(len(matches) == 1, "Installed owner must have exactly one stable external ID")
        return str(matches[0]["id"])

    def revoke_grant(self, env: IntegrationEnvironment) -> None:
        """Make consent visible on reruns; Gitea has no prompt=consent support."""
        self.login_gitea(env)
        response = self.follow(self.request("GET", self.gitea_url + "/user/settings/applications"))
        page = Page(response.url, response.text)
        prefix = f"/user/settings/applications/oauth2/{env.gitea_oauth_app_id}/revoke/"
        actions = [action for action in page.actions if action.startswith(prefix)]
        check(len(actions) <= 1, "More than one managed Gitea OAuth grant was found")
        for action in actions:
            response = self.request(
                "POST",
                urljoin(page.url, action),
                expected=(200,),
                headers={
                    "Origin": origin(page.url),
                    "Referer": origin(page.url) + "/",
                    "Sec-Fetch-Site": "same-origin",
                    "X-Gitea-Fetch-Action": "1",
                },
            )
            try:
                result = response.json()
            except ValueError:
                raise RuntimeError("Gitea grant revocation did not return JSON") from None
            check(bool(result.get("redirect")), "Gitea grant revocation did not succeed")

    def wait_for_later_token_expiry(self, env: IntegrationEnvironment) -> None:
        """Issue a later JWT without visiting Sentry; wait until Gitea rejects it.

        No Sentry operations may occur between its preceding read and this wait.
        With refresh invalidation disabled, this does not invalidate Sentry's grant.
        """
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        self.remember("oauth PKCE verifier", verifier)
        self.remember("oauth state", state)
        response = self.request(
            "GET",
            self.gitea_url
            + "/login/oauth/authorize?"
            + urlencode(
                {
                    "client_id": env.gitea_client_id,
                    "redirect_uri": self.sentry_url + CALLBACK,
                    "response_type": "code",
                    "scope": SCOPES,
                    "state": state,
                    "code_challenge": challenge.rstrip(b"=").decode(),
                    "code_challenge_method": "S256",
                }
            ),
        )
        check(response.status_code in REDIRECTS, "Existing confidential grant was not reused")
        callback = urljoin(response.url, response.headers["Location"])
        check(
            origin(callback) == origin(self.sentry_url) and urlsplit(callback).path == CALLBACK,
            "Token expiry probe did not receive the registered callback",
        )
        params = parse_qs(urlsplit(callback).query)
        check(
            params.get("state") == [state] and len(params.get("code", [])) == 1,
            "Token expiry probe received invalid state/code",
        )
        code = params["code"][0]
        self.remember("oauth authorization code", code)
        response = self.request(
            "POST",
            self.gitea_url + "/login/oauth/access_token",
            expected=(200,),
            data={
                "grant_type": "authorization_code",
                "client_id": env.gitea_client_id,
                "client_secret": env.gitea_client_secret,
                "code": code,
                "redirect_uri": self.sentry_url + CALLBACK,
                "code_verifier": verifier,
            },
        )
        try:
            tokens = response.json()
        except ValueError:
            raise RuntimeError("Expiry probe token exchange did not return JSON") from None
        check(
            isinstance(tokens.get("access_token"), str) and bool(tokens["access_token"]),
            "Expiry probe token exchange returned no access token",
        )
        for key, kind in (
            ("access_token", "oauth access token"),
            ("refresh_token", "oauth refresh token"),
        ):
            self.remember(kind, tokens.get(key, ""))
        lifetime = tokens.get("expires_in")
        check(
            type(lifetime) is int and 0 < lifetime <= 120,
            "Gitea access token lifetime must be at most 120s; recreate E2E Gitea",
        )
        # Use a fresh cookie-free session: a logged-in browser can mask expired Bearer auth.
        with requests.Session() as probe:
            probe.headers["Authorization"] = f"Bearer {tokens['access_token']}"

            def status() -> int:
                try:
                    reply = probe.get(
                        self.gitea_url + "/api/v1/user",
                        timeout=poll_budget(10),
                        allow_redirects=False,
                    )
                except requests.RequestException:
                    raise TransientError(
                        "Expiry probe could not reach Gitea; content withheld"
                    ) from None
                check(reply.status_code in (200, 401), "Expiry probe returned an unexpected status")
                return reply.status_code

            check(status() == 200, "New expiry probe token was not accepted by Gitea")
            poll(
                "Gitea to reject the later access token",
                lambda: status() == 401,
                seconds=lifetime + 20,
            )
