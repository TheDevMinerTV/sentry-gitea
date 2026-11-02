"""Notification adapters need Postgres and Redis, with no event storage backend."""

import json
import time
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import responses
from django.test import override_settings
from django.utils import timezone
from sentry import eventstore
from sentry.exceptions import PluginError
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.locks import locks
from sentry.models.activity import Activity
from sentry.models.grouplink import GroupLink
from sentry.models.groupmeta import GroupMeta
from sentry.plugins.base import plugins
from sentry.rules.actions.notify_event import NotifyEventAction
from sentry.rules.actions.notify_event_service import NotifyEventServiceAction
from sentry.testutils.cases import TestCase
from sentry.types.activity import ActivityType
from sentry.types.rules import RuleFuture
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea.api.client import REQUEST_ALLOWANCE, TIMEOUT, GiteaClient
from sentry_gitea.api.errors import GiteaApiError
from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL
from sentry_gitea.plugins.alerts import LOCK_WAIT
from sentry_gitea.plugins.issue import BLOCKED_ADDRESS, GiteaIssuePlugin, group_lock
from sentry_gitea.render import ensure_metadata, parse_metadata, render_body

BASE = "https://forge.example/git"
PUBLIC = "https://public-forge.example/browser"
API = BASE + "/api/v1/repos/owner/repo"
TOKEN = "synthetic-alert-credential"
BOT = {"id": 5, "login": "sentry-bot"}


class FakeClock:
    """Replaces only our plugin module's `time`; Sentry's own clock is untouched."""

    def __init__(self):
        # Start at the real clock: the Gitea client, which keeps real time, shares deadlines.
        self.now = time.monotonic()
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.slept.append(seconds)
        self.now += seconds


class TestGiteaAlertsPlugin(TestCase):
    def setUp(self):
        super().setUp()
        self.issue_plugin = plugins.get("gitea")
        self.plugin = plugins.get("gitea_alerts")
        for plugin in [self.issue_plugin, self.plugin]:
            plugin.enable(self.project)
        for name, value in {"base_url": BASE, "repo": "owner/repo", "token": TOKEN}.items():
            self.issue_plugin.set_option(name, value, self.project)
        self.event = eventstore.backend.create_event(
            project_id=self.project.id,
            group_id=self.group.id,
            event_id="b" * 32,
            data={
                "title": "Event failure",
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
                            # EventDict normalizes mechanisms without a type as legacy data.
                            "mechanism": {"type": "generic", "handled": False},
                        }
                    ]
                },
            },
        )
        self.event.group = self.group
        self.event.project = self.project
        self.addCleanup(GroupMeta.objects.clear_local_cache)

    def mock_repo(self):
        responses.get(BASE + "/api/v1/user", json=BOT)
        responses.get(API, json={"id": 41, "full_name": "owner/repo", "default_branch": "main"})

    def remote_issue(self, **kwargs):
        return {
            "number": 7,
            "title": "Remote issue",
            "state": "open",
            "labels": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
            "pull_request": None,
            "html_url": "http://gitea:3000/owner/repo/issues/7",
            "user": BOT,
            **kwargs,
        }

    def mock_create(self):
        self.mock_repo()
        responses.get(API + "/issues", json=[])
        responses.get(
            API + "/labels",
            json=[
                {"id": 1, "name": "source:sentry"},
                {"id": 2, "name": "severity:high"},
                {"id": 3, "name": "class:crash"},
                {"id": 4, "name": "default"},
            ],
            headers={"X-Total-Count": "4"},
        )
        responses.post(API + "/issues", json=self.remote_issue(), status=201)

    def notify(self):
        self.plugin.notify_users(self.group, self.event, ["Failure rule"])

    def created_issue_payload(self):
        # Label creation is also a POST, so method alone cannot identify the issue payload.
        calls = [
            call
            for call in responses.calls
            if call.request.method == "POST" and call.request.url == API + "/issues"
        ]
        assert len(calls) == 1, "Expected exactly one issue creation request"
        assert getattr(calls[0].response, "status_code", None) == 201
        return json.loads(calls[0].request.body)

    def test_event_normalization_preserves_unhandled_classification(self):
        mechanism = self.event.data["exception"]["values"][0]["mechanism"]
        assert mechanism["type"] == "generic"
        assert mechanism.get("handled") is False
        data = self.issue_plugin.event_data(self.group, self.event)
        assert data.handled is False
        metadata = parse_metadata(ensure_metadata(render_body(data), data))
        assert metadata["severity"] == "high"
        assert metadata["class"] == "crash"

    def test_config_defaults_dependency_and_no_duplicate_credentials(self):
        assert (self.plugin.author, self.plugin.author_url) == (AUTHOR, PROJECT_URL)
        assert ("Bug Tracker", ISSUES_URL) in self.plugin.resource_links
        fields = {f["name"]: f for f in self.plugin.get_config(self.project)}
        assert set(fields) == {
            "labels",
            "severity_labels",
            "comment_on_repeat",
            "fix_hint",
            "refile_on_regression",
            "refile_after_close",
        }
        assert fields["labels"]["default"] == "source:sentry"
        assert fields["severity_labels"]["default"] is True
        assert fields["comment_on_repeat"]["default"] is True
        assert fields["fix_hint"]["default"] is False
        assert fields["refile_on_regression"]["default"] is True
        assert fields["refile_after_close"]["default"] is False
        for key in ["refile_on_regression", "refile_after_close"]:
            with self.assertRaises(PluginError):
                self.plugin.validate_config(self.project, {key: "yes"})
        assert self.plugin.is_configured(self.project)
        self.issue_plugin.disable(self.project)
        assert self.plugin.is_configured(self.project) is False
        with self.assertRaises(PluginError):
            self.plugin.validate_config(self.project, {})
        self.issue_plugin.enable(self.project)
        self.issue_plugin.set_option("token", "", self.project)
        assert self.plugin.is_configured(self.project) is False

    @responses.activate
    def test_create_then_comment_open_then_skip_closed(self):
        self.mock_create()
        self.issue_plugin.set_option("default_labels", "default", self.project)
        with self.assertLogs("sentry_gitea.plugins.alerts", level="INFO") as logs:
            self.notify()
        assert any(r.message.startswith("sentry-gitea: filed issue") for r in logs.records)
        link = self.issue_plugin.read_link(self.group)
        assert link["id"] == "7"
        assert link["url"] == BASE + "/owner/repo/issues/7"
        assert self.issue_plugin.get_issue_label(self.group, "7") == "owner/repo#7"
        created = self.created_issue_payload()
        assert set(created["labels"]) == {1, 2, 3, 4}
        assert parse_metadata(created["body"])["class"] == "crash"
        assert all(c.request.headers["Authorization"] == f"token {TOKEN}" for c in responses.calls)
        responses.get(API + "/issues/7", json=self.remote_issue())
        responses.post(API + "/issues/7/comments", json={"id": 88}, status=201)
        with self.assertLogs("sentry_gitea.plugins.alerts", level="INFO") as logs:
            self.notify()
        assert any(r.message.startswith("sentry-gitea: commented on issue") for r in logs.records)
        comment = json.loads(responses.calls[-1].request.body)["body"]
        assert self.event.event_id in comment
        assert "<!-- sentry-gitea-event" in comment
        responses.replace(responses.GET, API + "/issues/7", json=self.remote_issue(state="closed"))
        before = len(responses.calls)
        self.notify()
        assert all(c.request.method == "GET" for c in responses.calls[before:])

    @responses.activate
    def test_repeat_option_and_severity_options_are_respected(self):
        self.mock_create()
        self.plugin.set_option("severity_labels", False, self.project)
        self.plugin.set_option("comment_on_repeat", False, self.project)
        self.notify()
        created = self.created_issue_payload()
        assert created["labels"] == [1]
        responses.get(API + "/issues/7", json=self.remote_issue())
        before = len(responses.calls)
        self.notify()
        assert all(c.request.method == "GET" for c in responses.calls[before:])

    @responses.activate
    def test_configuration_test_is_read_only(self):
        self.mock_repo()
        assert self.plugin.test_configuration(self.project) is None
        assert {c.request.method for c in responses.calls} == {"GET"}
        assert len(responses.calls) == 2
        assert self.issue_plugin.read_link(self.group) is None

    @responses.activate
    def test_failure_logs_contract_prefix_fields_without_token(self):
        responses.get(API, body=TOKEN, status=403)
        with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR") as logs:
            with self.assertRaises(PluginError):
                self.notify()
        record = logs.records[-1]
        assert record.getMessage().startswith("sentry-gitea: alert delivery failed")
        assert record.project_id == self.project.id
        assert record.group_id == self.group.id
        assert record.status == 403
        assert TOKEN not in str(logs.output)
        assert record.exc_info is None

    @responses.activate
    def test_remote_success_local_failure_recovers_marker_without_duplicate_create(self):
        self.mock_create()
        with patch.object(
            GiteaIssuePlugin, "write_link", side_effect=RuntimeError("DB unavailable")
        ) as write_link:
            with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR") as logs:
                with self.assertRaises(PluginError):
                    self.notify()
        write_link.assert_called_once()
        assert logs.records[-1].error_type == "RuntimeError"
        assert self.issue_plugin.read_link(self.group) is None
        created = self.created_issue_payload()
        responses.replace(
            responses.GET,
            API + "/issues",
            json=[self.remote_issue(body=created["body"])],
            headers={"X-Total-Count": "1"},
        )
        self.notify()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"
        assert (
            sum(
                c.request.method == "POST" and c.request.url == API + "/issues"
                for c in responses.calls
            )
            == 1
        )

    @responses.activate
    def test_public_url_controls_alert_link_and_does_not_change_repeat_binding(self):
        self.issue_plugin.set_option("public_url", PUBLIC, self.project)
        self.mock_create()
        self.notify()
        link = self.issue_plugin.read_link(self.group)
        assert link["url"] == PUBLIC + "/owner/repo/issues/7"
        assert link["base_url"] == BASE
        self.issue_plugin.set_option("public_url", "https://new-public.example", self.project)
        responses.get(API + "/issues/7", json=self.remote_issue())
        responses.post(API + "/issues/7/comments", json={"id": 88}, status=201)
        self.notify()
        assert self.issue_plugin.get_issue_url(self.group, "7") == link["url"]
        assert responses.calls[-1].request.url == API + "/issues/7/comments"
        assert self.group.get_absolute_url() in json.loads(responses.calls[-1].request.body)["body"]

    @responses.activate
    def test_recovers_closed_issue_without_reopening_or_commenting(self):
        self.issue_plugin.set_option("public_url", PUBLIC, self.project)
        self.mock_repo()
        data = self.issue_plugin.event_data(self.group, self.event)
        body = ensure_metadata(render_body(data), data)
        responses.get(
            API + "/issues",
            json=[self.remote_issue(body=body, state="closed")],
            headers={"X-Total-Count": "1"},
        )
        self.notify()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"
        assert self.issue_plugin.read_link(self.group)["url"] == PUBLIC + "/owner/repo/issues/7"
        assert all(c.request.method == "GET" for c in responses.calls)

    @responses.activate
    def test_configuration_change_displays_old_link_and_refuses_automatic_writes(self):
        self.mock_create()
        self.notify()
        self.issue_plugin.set_option("base_url", "https://new.example", self.project)
        self.issue_plugin.set_option("repo", "other/repo", self.project)
        assert self.issue_plugin.get_issue_url(self.group, "7") == BASE + "/owner/repo/issues/7"
        assert self.issue_plugin.get_issue_label(self.group, "7") == "owner/repo#7"
        before = len(responses.calls)
        with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR"):
            with self.assertRaises(PluginError):
                self.notify()
        assert len(responses.calls) == before

    @responses.activate
    def test_fix_hint_failures_cannot_block_filing(self):
        self.mock_create()
        self.plugin.set_option("fix_hint", True, self.project)
        responses.get(API + "/compare/abcdef0..main", status=500, body=TOKEN)
        with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR") as logs:
            self.notify()
        compare_calls = [
            call for call in responses.calls if call.request.url == API + "/compare/abcdef0..main"
        ]
        assert len(compare_calls) == 1
        assert compare_calls[0].response.status_code == 500
        self.created_issue_payload()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"
        assert TOKEN not in str(logs.output)
        assert logs.records[0].getMessage().startswith("sentry-gitea: alert delivery failed")
        assert logs.records[0].status == 500

    def test_lock_contention_retries_briefly_then_skips(self):
        """R3-7: a busy group lock is retried for at most LOCK_WAIT seconds, then skipped."""
        clock = FakeClock()
        with (
            patch("sentry_gitea.plugins.issue.time", clock),
            patch("sentry_gitea.plugins.issue.locks.get") as lock,
        ):
            lock.return_value.acquire.side_effect = UnableToAcquireLock("busy")
            with self.assertLogs("sentry_gitea.plugins.alerts", level="WARNING") as logs:
                assert self.notify() is None
        assert lock.return_value.acquire.call_count > 2
        assert len(clock.slept) == lock.return_value.acquire.call_count - 1
        assert 0 < sum(clock.slept) <= LOCK_WAIT <= 10
        assert sum(clock.slept) == pytest.approx(LOCK_WAIT)
        [record] = logs.records
        assert record.levelname == "WARNING"
        assert record.getMessage() == "sentry-gitea: alert skipped; group lock busy"
        assert record.group_id == self.group.id

    @responses.activate
    def test_lock_released_during_retry_window_delivers_the_alert(self):
        self.mock_create()
        clock = FakeClock()
        real_get = locks.get
        attempts = []

        def get(*args, **kwargs):
            lock = real_get(*args, **kwargs)
            acquire = lock.acquire

            def flaky_acquire():
                attempts.append(clock.now)
                if len(attempts) < 3:
                    raise UnableToAcquireLock("busy")
                return acquire()

            lock.acquire = flaky_acquire
            return lock

        with (
            patch("sentry_gitea.plugins.issue.time", clock),
            patch("sentry_gitea.plugins.issue.locks.get", side_effect=get),
        ):
            self.notify()
        assert len(attempts) == 3 and sum(clock.slept) < LOCK_WAIT
        self.created_issue_payload()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"

    def test_manual_operations_do_not_wait_for_the_lock(self):
        clock = FakeClock()
        with (
            patch("sentry_gitea.plugins.issue.time", clock),
            patch("sentry_gitea.plugins.issue.locks.get") as lock,
        ):
            lock.return_value.acquire.side_effect = UnableToAcquireLock("busy")
            with self.assertRaises(UnableToAcquireLock):
                with group_lock(self.group):
                    pass
        assert lock.return_value.acquire.call_count == 1 and clock.slept == []

    @responses.activate
    def test_planted_marker_by_another_user_is_not_recovered(self):
        """R2-03: a marker in an issue someone else opened must not capture the link."""
        self.mock_create()
        data = self.issue_plugin.event_data(self.group, self.event)
        planted = self.remote_issue(
            number=3,
            body=ensure_metadata(render_body(data), data),
            user={"id": 66, "login": "mallory"},
        )
        responses.replace(
            responses.GET, API + "/issues", json=[planted], headers={"X-Total-Count": "1"}
        )
        self.notify()
        self.created_issue_payload()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"
        user_calls = [c for c in responses.calls if c.request.url == BASE + "/api/v1/user"]
        assert len(user_calls) == 1

    @override_settings(GITEA_PLUGIN_ALLOWED_BASE_URLS=())
    def test_blocked_gitea_address_is_logged_and_reported(self):
        self.issue_plugin.set_option("base_url", "http://169.254.169.254", self.project)
        with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR") as logs:
            with self.assertRaises(PluginError) as caught:
                self.notify()
        assert str(caught.exception) == BLOCKED_ADDRESS
        assert logs.records[-1].error_type == "RestrictedIPAddress"
        assert logs.records[-1].status == 0
        assert TOKEN not in str(logs.output)

    @responses.activate
    def test_alert_rereads_link_after_acquiring_shared_lock(self):
        self.mock_repo()
        responses.get(API + "/issues/19", json=self.remote_issue(number=19, state="closed"))

        @contextmanager
        def acquire():
            self.issue_plugin.write_link(
                self.group,
                {
                    "id": "19",
                    "title": "Winner",
                    "repo": "owner/repo",
                    "repo_id": "41",
                    "base_url": BASE,
                    "url": BASE + "/owner/repo/issues/19",
                },
            )
            yield

        with patch("sentry_gitea.plugins.issue.locks.get") as get_lock:
            get_lock.return_value.acquire.side_effect = acquire
            self.notify()
        get_lock.assert_called_once_with(
            f"sentry-gitea:group:{self.group.id}", duration=60, name="sentry_gitea_link"
        )
        assert all(call.request.method == "GET" for call in responses.calls)

    @responses.activate
    def test_hint_has_separate_cutoff_and_leaves_time_for_create(self):
        self.mock_create()
        self.plugin.set_option("fix_hint", True, self.project)
        now = [0.0]
        clock = SimpleNamespace(monotonic=lambda: now[0])
        real_monotonic = time.monotonic
        budgets = []

        def slow_compare(client, base, head, *, read_deadline):
            budgets.append((client.deadline, read_deadline))
            # A final socket read can take 15 seconds before its timeout fires.
            now[0] = read_deadline + TIMEOUT[1]
            raise GiteaApiError(0, "Advisory read budget exhausted")

        # Patching time.monotonic itself also changes Sentry/Redis's process-wide clock.
        # Replace only our modules' time bindings, keeping their deadline units identical.
        with (
            patch("sentry_gitea.plugins.issue.time", clock),
            patch("sentry_gitea.api.client.time", clock),
            patch.object(
                GiteaClient, "compare_commits", autospec=True, side_effect=slow_compare
            ) as compare,
        ):
            assert time.monotonic is real_monotonic
            with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR") as logs:
                self.notify()
        compare.assert_called_once()
        # Hint exceptions are deliberately swallowed, so assertions belong outside the callback.
        deadline, read_deadline = budgets[0]
        assert read_deadline < deadline - REQUEST_ALLOWANCE
        assert now[0] + REQUEST_ALLOWANCE < deadline
        assert logs.records[0].status == 0
        self.created_issue_payload()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"

    @responses.activate
    def test_recovered_label_conflict_still_logs_failed_gitea_call(self):
        self.mock_repo()
        self.plugin.set_option("severity_labels", False, self.project)
        responses.get(API + "/issues", json=[])
        responses.get(API + "/labels", json=[])
        responses.post(API + "/labels", status=409, body=TOKEN)
        responses.get(
            API + "/labels",
            json=[{"id": 1, "name": "source:sentry"}],
            headers={"X-Total-Count": "1"},
        )
        responses.post(API + "/issues", json=self.remote_issue(), status=201)
        with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR") as logs:
            self.notify()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"
        assert logs.records[0].status == 409
        assert logs.records[0].getMessage().startswith("sentry-gitea: alert delivery failed")
        assert TOKEN not in str(logs.output)

    @responses.activate
    def test_both_action_routes_and_swallowed_failures_still_log(self):
        responses.get(API, status=500, body=TOKEN)
        rule = self.create_project_rule(project=self.project)
        for action_type in [NotifyEventAction, NotifyEventServiceAction]:
            action = action_type(self.project, data={"service": "gitea_alerts"})
            callbacks = list(action.after(self.event))
            callback = next(
                f.callback
                for f in callbacks
                if getattr(f.callback, "__self__", None) is self.plugin
            )
            with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR") as logs:
                callback(self.event, [RuleFuture(rule=rule, kwargs={})])
            assert TOKEN not in str(logs.output)

    def test_inherited_limiter_is_preserved(self):
        with patch(
            "sentry.plugins.bases.notify.ratelimits.backend.is_limited", return_value=True
        ) as limit:
            assert self.plugin.should_notify(self.group, self.event) is False
        limit.assert_called_once_with(project=self.project, key="gitea_alerts", limit=10)

    # Re-filing after the linked issue was closed (0.2.0).

    def issue_creates(self):
        return [
            json.loads(c.request.body)
            for c in responses.calls
            if c.request.method == "POST" and c.request.url == API + "/issues"
        ]

    def file_then_close(self, *, closed_ago, closed_at=True):
        """File #7, then make Gitea report it closed `closed_ago` ago; the next create is #8."""
        self.mock_create()
        self.notify()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"
        extra = {}
        if closed_at:
            extra["closed_at"] = (timezone.now() - closed_ago).strftime("%Y-%m-%dT%H:%M:%SZ")
        responses.get(API + "/issues/7", json=self.remote_issue(state="closed", **extra))
        responses.replace(
            responses.POST,
            API + "/issues",
            json=self.remote_issue(number=8, html_url="http://gitea:3000/owner/repo/issues/8"),
            status=201,
        )

    def regress(self, ago):
        Activity.objects.create(
            project=self.project,
            group=self.group,
            type=ActivityType.SET_REGRESSION.value,
            datetime=timezone.now() - ago,
            data={},
        )

    def assert_skipped(self, refile_skipped):
        before = len(responses.calls)
        with self.assertLogs("sentry_gitea.plugins.alerts", level="INFO") as logs:
            self.notify()
        record = next(r for r in logs.records if r.message.startswith("sentry-gitea: skipped"))
        assert (record.reason, record.refile_skipped) == ("closed", refile_skipped)
        assert all(c.request.method == "GET" for c in responses.calls[before:])
        assert self.issue_plugin.read_link(self.group)["id"] == "7"

    @responses.activate
    def test_regression_after_close_files_a_new_issue_referencing_the_old_one(self):
        self.file_then_close(closed_ago=timedelta(days=2))
        self.regress(timedelta(hours=1))
        with self.assertLogs("sentry_gitea.plugins.alerts", level="INFO") as logs:
            self.notify()
        record = next(r for r in logs.records if r.message.startswith("sentry-gitea: filed"))
        assert (record.issue_id, record.recurrence_of, record.refile_reason) == (
            "8",
            "7",
            "regressed",
        )
        link = self.issue_plugin.read_link(self.group)
        assert (link["id"], link["url"]) == ("8", BASE + "/owner/repo/issues/8")
        first, second = self.issue_creates()
        assert first["title"] == second["title"]
        assert first["labels"] == second["labels"]
        expected_date = (timezone.now() - timedelta(days=2)).date().isoformat()
        assert f"**Recurrence of:** #7 (closed {expected_date})" in second["body"]
        assert "Recurrence of" not in first["body"]
        assert parse_metadata(second["body"])["recurrence_of"] == "7"
        assert (
            parse_metadata(second["body"])["dedup_id"] == parse_metadata(first["body"])["dedup_id"]
        )
        # Recovery during the re-file only looks at open issues.
        searches = [
            c.request.url
            for c in responses.calls
            if c.request.method == "GET" and c.request.url.startswith(API + "/issues?")
        ]
        assert "state=open" in searches[-1]
        # The new link is open: the next alert comments on #8 and files nothing.
        responses.get(API + "/issues/8", json=self.remote_issue(number=8))
        responses.post(API + "/issues/8/comments", json={"id": 89}, status=201)
        self.notify()
        assert responses.calls[-1].request.url == API + "/issues/8/comments"
        assert len(self.issue_creates()) == 2

    @responses.activate
    def test_regression_before_the_close_does_not_refile(self):
        self.file_then_close(closed_ago=timedelta(days=1))
        self.regress(timedelta(days=2))
        self.assert_skipped("no recurrence signal")

    @responses.activate
    def test_missing_closed_at_never_refiles(self):
        self.plugin.set_option("refile_after_close", True, self.project)
        self.file_then_close(closed_ago=timedelta(days=2), closed_at=False)
        self.regress(timedelta(hours=1))
        self.assert_skipped("no closed_at")

    @responses.activate
    def test_both_refile_options_off_keep_closed_issues_silent(self):
        self.plugin.set_option("refile_on_regression", False, self.project)
        self.file_then_close(closed_ago=timedelta(days=2))
        self.regress(timedelta(hours=1))
        self.assert_skipped("disabled")

    @responses.activate
    def test_refile_after_close_waits_out_the_grace_period(self):
        self.plugin.set_option("refile_after_close", True, self.project)
        self.file_then_close(closed_ago=timedelta(minutes=13))
        self.assert_skipped("no recurrence signal")
        responses.replace(
            responses.GET,
            API + "/issues/7",
            json=self.remote_issue(
                state="closed",
                closed_at=(timezone.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
        with self.assertLogs("sentry_gitea.plugins.alerts", level="INFO") as logs:
            self.notify()
        record = next(r for r in logs.records if r.message.startswith("sentry-gitea: filed"))
        assert record.refile_reason == "after_close"
        assert self.issue_plugin.read_link(self.group)["id"] == "8"

    @responses.activate
    def test_refile_after_close_is_off_by_default(self):
        self.file_then_close(closed_ago=timedelta(days=3))
        self.assert_skipped("no recurrence signal")

    @responses.activate
    def test_comment_on_repeat_off_does_not_block_a_refile(self):
        self.plugin.set_option("comment_on_repeat", False, self.project)
        self.file_then_close(closed_ago=timedelta(days=2))
        self.regress(timedelta(hours=1))
        self.notify()
        assert self.issue_plugin.read_link(self.group)["id"] == "8"

    @responses.activate
    def test_lost_refile_link_recovers_the_new_open_issue_never_the_closed_one(self):
        self.file_then_close(closed_ago=timedelta(days=2))
        self.regress(timedelta(hours=1))
        with patch.object(
            GiteaIssuePlugin, "write_link", side_effect=RuntimeError("DB unavailable")
        ):
            with self.assertLogs("sentry_gitea.plugins.alerts", level="ERROR"):
                with self.assertRaises(PluginError):
                    self.notify()
        assert self.issue_plugin.read_link(self.group)["id"] == "7"
        _, refiled = self.issue_creates()
        old_body = self.issue_creates()[0]["body"]
        # Both carry the group's marker; the server-side state filter is not trusted alone.
        responses.replace(
            responses.GET,
            API + "/issues",
            json=[
                self.remote_issue(number=7, state="closed", body=old_body),
                self.remote_issue(number=8, body=refiled["body"]),
            ],
            headers={"X-Total-Count": "2"},
        )
        self.notify()
        assert self.issue_plugin.read_link(self.group)["id"] == "8"
        assert len(self.issue_creates()) == 2

    @responses.activate
    def test_integration_linked_issue_is_left_to_the_integrations_status_sync(self):
        self.file_then_close(closed_ago=timedelta(days=2))
        self.regress(timedelta(hours=1))
        integration = self.create_integration(
            organization=self.organization, provider="gitea", external_id="gitea-test"
        )
        external = ExternalIssue.objects.create(
            organization_id=self.organization.id,
            integration_id=integration.id,
            key="41#7",
            title="Remote issue",
        )
        GroupLink.objects.create(
            group=self.group,
            project_id=self.project.id,
            linked_id=external.id,
            linked_type=GroupLink.LinkedType.issue,
            relationship=GroupLink.Relationship.references,
        )
        self.assert_skipped("integration linked")


class TestAlertsReadFreshOptions(TestCase):
    """Regression: task workers never fire request_finished, so Sentry's thread-local
    project-option cache went stale and alerts used old labels/credentials."""

    def test_should_notify_drops_stale_thread_local_options(self):
        from sentry.models.options.project_option import ProjectOption

        issue_plugin = plugins.get("gitea")
        alerts = plugins.get("gitea_alerts")
        for plugin in (issue_plugin, alerts):
            plugin.enable(self.project)
        for name, value in {"base_url": BASE, "repo": "owner/repo", "token": TOKEN}.items():
            issue_plugin.set_option(name, value, self.project)
        issue_plugin.set_option("default_labels", "fresh", self.project)

        # Simulate a worker thread that cached the settings before an operator edit.
        key = ProjectOption.objects._make_key(self.project.id)
        stale = dict(ProjectOption.objects.get_all_values(self.project))
        stale["gitea:default_labels"] = "stale"
        ProjectOption.objects._option_cache[key] = stale
        assert issue_plugin.get_option("default_labels", self.project) == "stale"

        # should_notify only reads event.project; avoid the Snuba-backed self.event fixture.
        from types import SimpleNamespace

        alerts.should_notify(self.group, SimpleNamespace(project=self.project))

        assert issue_plugin.get_option("default_labels", self.project) == "fresh"


class TestAlertsSeeEnablingWithoutRestart(TestCase):
    """Regression (E2E blocker): the rule action checks is_enabled before should_notify, and a
    task worker's thread-local option cache kept the "disabled" value until restart."""

    def stale_worker_cache(self, **overrides):
        from sentry.models.options.project_option import ProjectOption

        manager = ProjectOption.objects
        stale = dict(manager.get_all_values(self.project))
        stale.update(overrides)
        manager._option_cache[manager._make_key(self.project.id)] = stale

    def test_rule_action_uses_current_enabled_state(self):
        issue_plugin = plugins.get("gitea")
        alerts = plugins.get("gitea_alerts")
        for name, value in {"base_url": BASE, "repo": "owner/repo", "token": TOKEN}.items():
            issue_plugin.set_option(name, value, self.project)
        issue_plugin.enable(self.project)
        alerts.disable(self.project)
        # The worker read the options while Gitea Alerts was still disabled...
        assert alerts.is_enabled(self.project) is False
        # ...then the operator enabled it (shared cache updated, worker's local copy not).
        alerts.enable(self.project)
        self.stale_worker_cache(**{"gitea_alerts:enabled": False, "gitea:enabled": False})
        assert alerts.get_option("enabled", self.project) is False
        assert alerts.is_enabled(self.project) is True
        self.stale_worker_cache(**{"gitea_alerts:enabled": False, "gitea:enabled": False})
        event = SimpleNamespace(project=self.project, group=self.group, event_id="d" * 32)
        action = NotifyEventServiceAction(self.project, data={"service": "gitea_alerts"})
        with patch.object(action.logger, "info") as info:
            futures = list(action.after(event))
        assert not any(c.args and c.args[0] == "rules.fail.is_enabled" for c in info.call_args_list)
        assert [f.callback.__self__ for f in futures] == [alerts]
