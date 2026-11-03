"""DB/Redis adapter tests; events are constructed in memory, never stored in Snuba."""

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests
import responses
from django.conf import settings
from django.test import override_settings
from sentry import eventstore
from sentry.api.serializers.models.plugin import serialize_field
from sentry.exceptions import PluginError
from sentry.models.group import Group
from sentry.models.groupmeta import GroupMeta
from sentry.models.options.project_option import ProjectOption
from sentry.net.http import SafeSession
from sentry.plugins.base import plugins
from sentry.testutils.cases import APITestCase
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL
from sentry_gitea.plugins.issue import (
    BLOCKED_ADDRESS,
    TOKEN_REENTRY,
    GiteaIssuePlugin,
    plugin_allowed_origins,
    plugin_session,
)
from sentry_gitea.render import ensure_metadata, parse_metadata, render_body

BASE = "https://forge.example/git"
PUBLIC = "https://public-forge.example/browser"
API = BASE + "/api/v1/repos/owner/repo"
TOKEN = "synthetic-plugin-credential"
REPOSITORY = {"id": 41, "full_name": "owner/repo", "default_branch": "main"}
BOT = {"id": 5, "login": "sentry-bot"}
UPSTREAM_TEXT = "upstream-body-must-not-be-echoed"


@contextmanager
def fake_gitea():
    """A real HTTP server on loopback answering the two configuration checks."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = {
                "/api/v1/user": BOT,
                "/api/v1/repos/owner/repo": REPOSITORY,
            }.get(self.path)
            body = json.dumps(payload or {"message": "not found"}).encode()
            self.send_response(200 if payload else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class TestGiteaIssuePlugin(APITestCase):
    def setUp(self):
        super().setUp()
        self.plugin = plugins.get("gitea")
        self.plugin.enable(self.project)
        for name, value in {"base_url": BASE, "repo": "owner/repo", "token": TOKEN}.items():
            self.plugin.set_option(name, value, self.project)
        self.login_as(self.user)
        self.event = eventstore.backend.create_event(
            project_id=self.project.id,
            group_id=self.group.id,
            event_id="a" * 32,
            data={
                "title": "A failure",
                "type": "error",
                "level": "error",
                "timestamp": 1700000000,
                "release": "app@abcdef0",
                "culprit": "src/server.py:42",
                "tags": [["environment", "production"]],
                "exception": {
                    "values": [
                        {
                            "type": "Error",
                            "value": "failure",
                            "mechanism": {"type": "generic", "handled": False},
                        }
                    ]
                },
            },
        )
        self.event.group = self.group
        self.event.project = self.project
        self.latest = patch.object(Group, "get_latest_event", return_value=self.event)
        self.latest.start()
        self.addCleanup(self.latest.stop)
        self.addCleanup(GroupMeta.objects.clear_local_cache)

    def endpoint(self, action):
        return f"/api/0/issues/{self.group.id}/plugins/gitea/{action}/"

    def config_endpoint(self):
        return (
            f"/api/0/projects/{self.project.organization.slug}/{self.project.slug}/plugins/gitea/"
        )

    def remote_issue(self, **overrides):
        return {
            "number": 7,
            "id": 9001,
            "title": "Remote title",
            "state": "open",
            "html_url": "http://gitea:3000/owner/repo/issues/7",
            "pull_request": None,
            "user": BOT,
            **overrides,
        }

    def mock_create(self):
        responses.get(BASE + "/api/v1/user", json=BOT)
        responses.get(API, json=REPOSITORY)
        responses.get(API + "/assignees", json=[{"login": "alice"}])
        responses.get(API + "/issues", json=[])
        responses.get(
            API + "/labels", json=[{"id": 12, "name": "bug"}], headers={"X-Total-Count": "1"}
        )
        responses.post(
            API + "/issues",
            json=self.remote_issue(labels=[{"id": 12}], assignees=[{"login": "alice"}]),
            status=201,
        )

    def test_fields_metadata_and_saved_secret_serialization(self):
        assert isinstance(self.plugin, GiteaIssuePlugin)
        assert self.plugin.author == AUTHOR
        assert self.plugin.author_url == PROJECT_URL
        assert ("Bug Tracker", ISSUES_URL) in self.plugin.resource_links
        assert self.plugin.auth_provider is None
        assert self.plugin.issue_fields == {"id", "title", "url", "repo", "repo_id", "base_url"}
        fields = self.plugin.get_config(
            self.project, user=self.user, initial={}, add_additional_fields=True
        )
        assert {f["name"] for f in fields} == {
            "base_url",
            "public_url",
            "token",
            "repo",
            "default_labels",
        }
        public = next(f for f in fields if f["name"] == "public_url")
        assert public["label"] == "Gitea URL for links"
        assert public["type"] == "text"
        assert public["required"] is False
        token = next(f for f in fields if f["name"] == "token")
        serialized = serialize_field(self.project, self.plugin, token)
        assert serialized["type"] == "secret"
        assert serialized["required"] is False
        assert serialized["hasSavedValue"] is True
        assert serialized["value"] is None
        assert serialized["prefix"] == ""
        assert TOKEN not in json.dumps(serialized)
        # Blank stays blank here; validate_config decides whether the saved token is reused.
        for empty in [None, "", "   "]:
            assert self.plugin.validate_config_field(self.project, "token", empty) == ""

    @responses.activate
    def test_config_api_normalizes_validates_and_preserves_blank_secret(self):
        responses.get(BASE + "/api/v1/user", json={"id": 1})
        responses.get(API, json=REPOSITORY)
        response = self.client.put(
            self.config_endpoint(),
            data={
                "base_url": BASE + "/",
                "public_url": " https://PUBLIC-FORGE.example/browser/ ",
                "repo": "owner/repo",
                "token": "",
                "default_labels": " bug ",
            },
        )
        assert response.status_code == 200, response.data
        assert TOKEN not in json.dumps(response.data)
        assert self.plugin.get_option("token", self.project) == TOKEN
        assert self.plugin.get_option("base_url", self.project) == BASE
        assert self.plugin.get_option("public_url", self.project) == PUBLIC
        assert self.plugin.get_option("default_labels", self.project) == "bug"

    @responses.activate
    def test_public_url_validation_and_clearing(self):
        responses.get(BASE + "/api/v1/user", json={"id": 1})
        responses.get(API, json=REPOSITORY)
        self.plugin.set_option("public_url", PUBLIC, self.project)
        assert self.plugin.validate_config(self.project, {})["public_url"] == PUBLIC
        # Sentry's field hook trims surrounding whitespace before URL validation.
        for empty in [None, "", "   "]:
            assert self.plugin.validate_config_field(self.project, "public_url", empty) == ""
        for empty in [None, ""]:
            assert (
                self.plugin.validate_config(self.project, {"public_url": empty})["public_url"] == ""
            )
        for invalid in [
            "ftp://forge.example",
            "https://u:p@forge.example",
            "https://forge.example?x=1",
            "https://forge.example#fragment",
            "https://forge.example/%2e%2e/api",
            "https://forge.\nexample",
            False,
        ]:
            with self.subTest(public_url=invalid):
                before = len(responses.calls)
                with self.assertRaises(PluginError):
                    self.plugin.validate_config_field(self.project, "public_url", invalid)
                with self.assertRaises(PluginError):
                    self.plugin.validate_config(self.project, {"public_url": invalid})
                assert len(responses.calls) == before
                self.plugin.set_option("public_url", invalid, self.project)
                assert self.plugin.is_configured(self.project) is False
        response = self.client.put(
            self.config_endpoint(), data={"base_url": BASE, "repo": "owner/repo", "public_url": ""}
        )
        assert response.status_code == 200, response.data
        assert self.plugin.get_option("public_url", self.project) == ""
        assert self.plugin.is_configured(self.project)

    @responses.activate
    def test_validation_errors_are_friendly_and_logged_without_token(self):
        responses.get(BASE + "/api/v1/user", status=401, body=TOKEN)
        with self.assertLogs("sentry_gitea.plugins.issue", level="ERROR") as logs:
            with self.assertRaises(PluginError) as caught:
                self.plugin.validate_config(self.project, {"base_url": BASE, "repo": "owner/repo"})
        assert "token" in str(caught.exception).lower()
        assert TOKEN not in str(caught.exception)
        assert TOKEN not in str(logs.output)
        assert logs.records[0].status == 401
        for field, value in [("repo", "o/r/extra"), ("base_url", "https://u:p@forge.example")]:
            with self.assertRaises(PluginError):
                self.plugin.validate_config_field(self.project, field, value)

    def test_is_configured_is_local_and_requires_all_credentials(self):
        assert self.plugin.is_configured(self.project)
        assert self.plugin.is_configured(None) is False
        self.plugin.set_option("token", "", self.project)
        assert self.plugin.is_configured(self.project) is False

    @responses.activate
    def test_create_fields_and_complete_link_persistence(self):
        self.mock_create()
        response = self.client.get(self.endpoint("create"))
        assert response.status_code == 200, response.data
        fields = {field["name"]: field for field in response.data}
        assert fields["title"]["default"].startswith(f"[{self.group.qualified_short_id}]")
        assert ["alice", "alice"] in [list(c) for c in fields["assignee"]["choices"]]
        assert fields["repo"]["readonly"] is True
        response = self.client.post(
            self.endpoint("create"),
            data={
                "title": "User title",
                "description": "Human edited description",
                "labels": "bug",
                "assignee": "alice",
            },
        )
        assert response.status_code == 200, response.data
        assert response.data["label"] == "owner/repo#7"
        link = self.plugin.read_link(self.group)
        assert link == {
            "id": "7",
            "title": "Remote title",
            "repo": "owner/repo",
            "repo_id": "41",
            "base_url": BASE,
            "url": BASE + "/owner/repo/issues/7",
        }
        stored = dict(GroupMeta.objects.filter(group=self.group).values_list("key", "value"))
        assert {
            "gitea:issue_" + key: value for key, value in link.items()
        }.items() <= stored.items()
        payload = json.loads(
            next(c.request.body for c in responses.calls if c.request.method == "POST")
        )
        assert payload["labels"] == [12]
        assert payload["assignees"] == ["alice"]
        assert payload["body"].startswith("Human edited description")
        assert parse_metadata(payload["body"])["dedup_id"].endswith(
            f"/{self.project.id}/{self.group.id}"
        )

    @responses.activate
    def test_create_stores_and_displays_public_url_without_changing_api_binding(self):
        self.plugin.set_option("public_url", PUBLIC + "/", self.project)
        self.mock_create()
        response = self.client.post(
            self.endpoint("create"),
            data={"title": "Title", "description": "Body", "labels": "bug", "assignee": "alice"},
        )
        assert response.status_code == 200, response.data
        expected = PUBLIC + "/owner/repo/issues/7"
        assert response.data["issue_url"] == response.data["link"] == expected
        link = self.plugin.read_link(self.group)
        assert link["url"] == expected
        assert link["base_url"] == BASE
        assert self.plugin.get_issue_url(self.group, "7") == expected
        self.plugin.set_option("public_url", "https://new-public.example", self.project)
        self.plugin.assert_binding(self.project, link, REPOSITORY)
        assert self.plugin.get_issue_url(self.group, "7") == expected

    @responses.activate
    def test_link_ignores_remote_html_url_with_or_without_public_url(self):
        responses.get(API, json=REPOSITORY)
        responses.get(API + "/issues/7", json=self.remote_issue())
        for public in ["", PUBLIC]:
            for html_url in [
                "http://gitea:3000/owner/repo/issues/7",
                "https://unrelated.example/owner/repo/issues/7",
                None,
            ]:
                with self.subTest(public_url=public, html_url=html_url):
                    self.plugin.set_option("public_url", public, self.project)
                    responses.replace(
                        responses.GET, API + "/issues/7", json=self.remote_issue(html_url=html_url)
                    )
                    response = self.client.post(self.endpoint("link"), data={"issue_id": "7"})
                    assert response.status_code == 200, response.data
                    expected = (public or BASE) + "/owner/repo/issues/7"
                    assert response.data["link"] == expected
                    assert self.plugin.read_link(self.group)["url"] == expected
                    assert self.plugin.get_issue_url(self.group, "7") == expected
                    assert self.client.post(self.endpoint("unlink")).status_code == 200

    @responses.activate
    def test_semantic_title_and_assignee_validation(self):
        self.mock_create()
        for fields in [{"title": "   "}, {"title": "Title", "assignee": "unknown"}]:
            response = self.client.post(
                self.endpoint("create"), data={"description": "Body", **fields}
            )
            assert response.status_code == 400, response.data
        assert not any(call.request.method == "POST" for call in responses.calls)

    @responses.activate
    def test_link_and_unlink_keep_nonempty_title_and_clear_all_keys(self):
        responses.get(API, json=REPOSITORY)
        responses.get(API + "/issues/7", json=self.remote_issue(title=""))
        responses.post(API + "/issues/7/comments", json={"id": 8}, status=201)
        fields = self.client.get(self.endpoint("link")).data
        assert self.group.get_absolute_url() in next(
            f["default"] for f in fields if f["name"] == "comment"
        )
        response = self.client.post(
            self.endpoint("link"), data={"issue_id": "7", "comment": "backlink"}
        )
        assert response.status_code == 200, response.data
        assert self.plugin.read_link(self.group)["title"]
        assert self.plugin.get_issue_label(self.group, "7") == "owner/repo#7"
        assert self.client.post(self.endpoint("unlink")).status_code == 200
        assert self.plugin.read_link(self.group) is None
        assert not GroupMeta.objects.filter(
            group=self.group, key__startswith="gitea:issue_"
        ).exists()
        assert all(c.request.method != "DELETE" for c in responses.calls)

    @responses.activate
    def test_link_rejects_pull_requests(self):
        responses.get(API, json=REPOSITORY)
        responses.get(API + "/issues/7", json=self.remote_issue(pull_request={}))
        response = self.client.post(self.endpoint("link"), data={"issue_id": "7"})
        assert response.status_code == 400
        assert self.plugin.read_link(self.group) is None

    def test_no_event_returns_inherited_validation_error(self):
        with patch.object(Group, "get_latest_event", return_value=None):
            for action in ["create", "link"]:
                assert self.client.post(self.endpoint(action), data={}).status_code == 400

    def test_fresh_read_drops_stale_cache_after_external_unlink(self):
        self.plugin.write_link(self.group, {"id": "7", "title": "Old"})
        assert self.plugin.build_issue(self.group)["id"] == "7"
        GroupMeta.objects.filter(group=self.group, key__startswith="gitea:issue_").delete()
        assert self.plugin.read_link(self.group) is None

    @responses.activate
    def test_lock_covers_remote_create_and_core_persistence(self):
        self.mock_create()
        held = False
        writes = GroupMeta.objects.set_value

        @contextmanager
        def acquire():
            nonlocal held
            held = True
            yield
            held = False

        def checked_write(*args, **kwargs):
            assert held
            return writes(*args, **kwargs)

        with patch("sentry_gitea.plugins.issue.locks.get") as get_lock:
            get_lock.return_value.acquire.side_effect = acquire
            with patch.object(GroupMeta.objects, "set_value", side_effect=checked_write):
                response = self.client.post(
                    self.endpoint("create"),
                    data={
                        "title": "Title",
                        "description": "Body",
                        "assignee": "alice",
                        "labels": "bug",
                    },
                )
        assert response.status_code == 200, response.data
        get_lock.assert_called_once_with(
            f"sentry-gitea:group:{self.group.id}", duration=60, name="sentry_gitea_link"
        )
        assert held is False

    def test_contention_is_deferred_and_logged(self):
        with patch("sentry_gitea.plugins.issue.locks.get") as lock:
            lock.return_value.acquire.side_effect = UnableToAcquireLock("busy")
            with self.assertLogs("sentry_gitea.plugins.issue", level="WARNING"):
                response = self.client.post(self.endpoint("unlink"))
        assert response.status_code == 409

    def test_sidebar_get_unlink_is_mutating_and_uses_shared_lock(self):
        self.plugin.write_link(self.group, {"id": "7", "title": "Old"})
        with patch("sentry_gitea.plugins.issue.locks.get") as get_lock:
            response = self.client.get(self.endpoint("unlink"))
        assert response.status_code == 200, response.data
        get_lock.assert_called_once_with(
            f"sentry-gitea:group:{self.group.id}", duration=60, name="sentry_gitea_link"
        )
        assert self.plugin.read_link(self.group) is None

    @responses.activate
    def test_manual_create_rereads_winning_link_inside_lock(self):
        self.mock_create()
        winner = {
            "id": "19",
            "title": "Winner",
            "repo": "owner/repo",
            "repo_id": "41",
            "base_url": BASE,
            "url": BASE + "/owner/repo/issues/19",
        }

        @contextmanager
        def acquire():
            self.plugin.write_link(self.group, winner)
            yield

        with patch("sentry_gitea.plugins.issue.locks.get") as get_lock:
            get_lock.return_value.acquire.side_effect = acquire
            response = self.client.post(
                self.endpoint("create"), data={"title": "Title", "description": "Body"}
            )
        assert response.status_code == 200, response.data
        assert response.data["id"] == "19"
        assert all(call.request.method == "GET" for call in responses.calls)

    def test_transaction_rollback_clears_cache_and_all_partial_fields(self):
        original = GroupMeta.objects.set_value
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated local failure")
            return original(*args, **kwargs)

        with patch.object(GroupMeta.objects, "set_value", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.plugin.write_link(self.group, {"id": "7", "title": "Title"})
        assert self.plugin.read_link(self.group) is None


class TestPluginOutboundGuard(APITestCase):
    """R2-01: a project admin chooses base_url, so it must not become an internal-network probe."""

    def setUp(self):
        super().setUp()
        self.plugin = plugins.get("gitea")
        self.plugin.enable(self.project)
        for name, value in {"base_url": BASE, "repo": "owner/repo", "token": TOKEN}.items():
            self.plugin.set_option(name, value, self.project)
        self.login_as(self.user)

    def put(self, **data):
        url = f"/api/0/projects/{self.project.organization.slug}/{self.project.slug}/plugins/gitea/"
        return self.client.put(url, data={"repo": "owner/repo", **data}, format="json")

    def assert_unchanged(self):
        assert self.plugin.get_option("base_url", self.project) == BASE
        assert self.plugin.get_option("token", self.project) == TOKEN

    def test_private_and_reserved_targets_are_refused_without_allowlist(self):
        real_getaddrinfo = socket.getaddrinfo

        def resolve(host, *args, **kwargs):
            # A private DNS name, resolved the way an internal resolver would answer.
            if host.rstrip(".") == "gitea.internal":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 3000))]
            if host.strip("[]") == "::1":
                # Even where the container lacks IPv6, the guard must judge the address.
                return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 3000, 0, 0))]
            return real_getaddrinfo(host, *args, **kwargs)

        targets = [
            "http://169.254.169.254",
            "http://127.0.0.1:3000",
            "http://localhost:3000",
            "http://[::1]:3000",
            "http://gitea.internal:3000",
        ]
        with override_settings(GITEA_PLUGIN_ALLOWED_BASE_URLS=()):
            with patch("socket.getaddrinfo", side_effect=resolve):
                for target in targets:
                    with self.subTest(target=target):
                        assert isinstance(plugin_session(target), SafeSession)
                        with self.assertLogs("sentry_gitea.plugins.issue", level="ERROR") as logs:
                            response = self.put(base_url=target, token="new-token")
                        assert response.status_code == 400, response.data
                        assert response.data["errors"]["__all__"] == BLOCKED_ADDRESS
                        assert logs.records[-1].error_type == "RestrictedIPAddress"
                        assert "new-token" not in str(logs.output)
                        self.assert_unchanged()

    def test_allowlisted_origin_may_use_a_private_address(self):
        with fake_gitea() as url:
            with override_settings(GITEA_PLUGIN_ALLOWED_BASE_URLS=()):
                response = self.put(base_url=url, token="new-token")
                assert response.status_code == 400
                assert response.data["errors"]["__all__"] == BLOCKED_ADDRESS
            with override_settings(GITEA_PLUGIN_ALLOWED_BASE_URLS=[url.upper() + "/"]):
                assert type(plugin_session(url + "/sub")) is requests.Session
                response = self.put(base_url=url + "/", token="new-token")
        assert response.status_code == 200, response.data
        assert self.plugin.get_option("base_url", self.project) == url
        assert self.plugin.get_option("token", self.project) == "new-token"

    def test_default_allowlist_is_the_integration_instance(self):
        with override_settings(
            GITEA_PLUGIN_ALLOWED_BASE_URLS=None,
            GITEA_BASE_URL="http://gitea:3000/",
            GITEA_PUBLIC_URL="https://GIT.example:443/x",
        ):
            del settings.GITEA_PLUGIN_ALLOWED_BASE_URLS  # unset, not merely None
            assert plugin_allowed_origins() == {"http://gitea:3000", "https://git.example"}
            assert type(plugin_session("http://gitea:3000/api")) is requests.Session
            # An explicit setting replaces the default.
            with override_settings(GITEA_PLUGIN_ALLOWED_BASE_URLS="http://other:3000"):
                assert plugin_allowed_origins() == {"http://other:3000"}
            with override_settings(GITEA_PLUGIN_ALLOWED_BASE_URLS=("not a url", "http://ok")):
                with self.assertLogs("sentry_gitea.plugins.issue", level="WARNING"):
                    assert plugin_allowed_origins() == {"http://ok"}
        with override_settings(
            GITEA_PLUGIN_ALLOWED_BASE_URLS=None, GITEA_BASE_URL=None, GITEA_PUBLIC_URL=None
        ):
            assert plugin_allowed_origins() == frozenset()
            assert isinstance(plugin_session("http://gitea:3000"), SafeSession)

    @responses.activate
    def test_changing_base_url_requires_reentering_the_token(self):
        response = self.put(base_url="https://other.example/git", token="")
        assert response.status_code == 400
        assert response.data["errors"]["__all__"] == TOKEN_REENTRY
        assert not responses.calls
        self.assert_unchanged()
        with self.assertRaises(PluginError):
            self.plugin.validate_config(self.project, {"base_url": "https://other.example/git"})
        assert not responses.calls
        # Same server after normalization: the saved token is kept.
        responses.get(BASE + "/api/v1/user", json=BOT)
        responses.get(API, json=REPOSITORY)
        response = self.put(base_url="HTTPS://FORGE.example/git/", token="  ")
        assert response.status_code == 200, response.data
        self.assert_unchanged()
        # A token typed for the new server is used, and only there.
        other = "https://other.example/git/api/v1"
        responses.get(other + "/user", json=BOT)
        responses.get(other + "/repos/owner/repo", json=REPOSITORY)
        response = self.put(base_url="https://other.example/git", token="other-token")
        assert response.status_code == 200, response.data
        assert self.plugin.get_option("token", self.project) == "other-token"
        assert {
            c.request.headers["Authorization"]
            for c in responses.calls
            if c.request.url.startswith(other)
        } == {"token other-token"}

    @responses.activate
    def test_configuration_errors_report_status_without_upstream_bodies(self):
        responses.get(BASE + "/api/v1/user", status=500)
        for status in [401, 403, 404, 500, 502]:
            with self.subTest(status=status):
                responses.replace(
                    responses.GET, BASE + "/api/v1/user", status=status, body=UPSTREAM_TEXT
                )
                with self.assertLogs("sentry_gitea.plugins.issue", level="ERROR"):
                    response = self.put(base_url=BASE, token="new-token")
                assert response.status_code == 400
                assert UPSTREAM_TEXT not in json.dumps(response.data)
                assert f"Gitea status {status}" in response.data["errors"]["__all__"]
        # Our own diagnostics are still shown.
        responses.replace(
            responses.GET, BASE + "/api/v1/user", status=302, headers={"Location": "/elsewhere"}
        )
        with self.assertLogs("sentry_gitea.plugins.issue", level="ERROR"):
            response = self.put(base_url=BASE, token="new-token")
        assert "redirect refused" in response.data["errors"]["__all__"]


class TestIssuePluginReadsFreshOptions(APITestCase):
    def test_is_enabled_ignores_a_stale_thread_local_disabled_flag(self):
        """Regression: a worker cached "disabled" and never saw the operator enable it."""
        plugin = plugins.get("gitea")
        plugin.enable(self.project)
        other = self.create_project(organization=self.organization)
        manager = ProjectOption.objects
        key = manager._make_key(self.project.id)
        stale = dict(manager.get_all_values(self.project))
        stale["gitea:enabled"] = False
        manager._option_cache[key] = stale
        manager._option_cache[manager._make_key(other.id)] = {"marker": "kept"}
        assert plugin.get_option("enabled", self.project) is False
        assert plugin.is_enabled(self.project) is True
        # Only this project's entry is dropped.
        assert manager._option_cache[manager._make_key(other.id)] == {"marker": "kept"}
        plugin.disable(self.project)
        assert plugin.is_enabled(self.project) is False


class TestManualCreateTrustsOnlyOwnMarkers(APITestCase):
    def setUp(self):
        super().setUp()
        self.plugin = plugins.get("gitea")
        self.plugin.enable(self.project)
        for name, value in {"base_url": BASE, "repo": "owner/repo", "token": TOKEN}.items():
            self.plugin.set_option(name, value, self.project)
        self.login_as(self.user)
        event = eventstore.backend.create_event(
            project_id=self.project.id,
            group_id=self.group.id,
            event_id="c" * 32,
            data={"title": "A failure", "level": "error", "timestamp": 1700000000},
        )
        event.group, event.project = self.group, self.project
        latest = patch.object(Group, "get_latest_event", return_value=event)
        latest.start()
        self.addCleanup(latest.stop)
        self.addCleanup(GroupMeta.objects.clear_local_cache)
        data = self.plugin.event_data(self.group, event)
        self.marker_body = ensure_metadata(render_body(data), data)

    @responses.activate
    def test_marker_on_an_issue_by_another_user_is_not_recovered(self):
        responses.get(BASE + "/api/v1/user", json=BOT)
        responses.get(API, json=REPOSITORY)
        responses.get(API + "/assignees", json=[])
        planted = {
            "number": 3,
            "title": "Planted",
            "state": "open",
            "pull_request": None,
            "body": self.marker_body,
            "user": {"id": 66, "login": "mallory"},
        }
        responses.get(API + "/issues", json=[planted], headers={"X-Total-Count": "1"})
        responses.post(
            API + "/issues",
            json={**planted, "number": 8, "title": "Ours", "user": BOT},
            status=201,
        )
        response = self.client.post(
            f"/api/0/issues/{self.group.id}/plugins/gitea/create/",
            data={"title": "Title", "description": "Body"},
        )
        assert response.status_code == 200, response.data
        assert self.plugin.read_link(self.group)["id"] == "8"
        assert sum(c.request.method == "POST" for c in responses.calls) == 1
        # The same marker on our own user's issue is recovered without creating another.
        self.plugin.write_link(self.group, {})
        responses.replace(
            responses.GET,
            API + "/issues",
            json=[planted, {**planted, "number": 9, "user": BOT}],
            headers={"X-Total-Count": "2"},
        )
        response = self.client.post(
            f"/api/0/issues/{self.group.id}/plugins/gitea/create/",
            data={"title": "Title", "description": "Body"},
        )
        assert response.status_code == 200, response.data
        assert self.plugin.read_link(self.group)["id"] == "9"
        assert sum(c.request.method == "POST" for c in responses.calls) == 1
