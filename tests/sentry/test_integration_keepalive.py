"""OAuth keep-alive: the `refresh_identities` task and the schedule shipped in the snippet."""

import json
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs

import responses
from sentry.constants import ObjectStatus
from sentry.taskworker.runtime import app
from sentry.testutils.silo import no_silo_test
from sentry.users.models.identity import Identity
from taskbroker_client.scheduler import ScheduleRunner
from test_integration_provider import BASE, METADATA, TOKENS, IntegrationFixture

from sentry_gitea.integration.tasks import KEEPALIVE_AGE, refresh_identities

LOGGER = "sentry_gitea.integration"
TOKEN_URL = BASE + "/login/oauth/access_token"
SNIPPET = Path(__file__).resolve().parents[2] / "docker" / "sentry.conf.py.snippet"
DAY = 24 * 3600


def rotate(*, failing=()):
    """Gitea's token endpoint: rotate each refresh token, or reject the expired ones."""

    def callback(request):
        refresh = parse_qs(request.body)["refresh_token"][0]
        if refresh in failing:
            body = {"error": "invalid_grant", "error_description": f"expired {refresh}"}
            return 400, {}, json.dumps(body)
        body = {
            "access_token": refresh + "-a2",
            "refresh_token": refresh + "-r2",
            "expires_in": 3600,
        }
        return 200, {}, json.dumps(body)

    return callback


def refreshes():
    return [parse_qs(c.request.body)["refresh_token"][0] for c in responses.calls]


@no_silo_test
class TestIdentityKeepAlive(IntegrationFixture):
    def install(self, name, *, issued_ago=None, organization=None):
        """Another owner installation, with its own identity and refresh token `name`."""
        data = {**TOKENS, "access_token": name + "-a", "refresh_token": name}
        if issued_ago is not None:
            data["issued_at"] = int(time.time() - issued_ago)
        integration, org_integration, identity, _ = self.create_identity_integration(
            self.user,
            organization or self.organization,
            {
                "provider": "gitea",
                "external_id": "owner-" + name,
                "name": name,
                "metadata": {**METADATA, "owner_id": 100 + len(name)},
            },
            {"external_id": f"{BASE}|2", "data": data},
        )
        return integration, org_integration, identity

    def data(self, identity):
        return Identity.objects.get(id=identity.id).data

    @responses.activate
    def test_refreshes_only_due_identities_of_active_installations(self):
        responses.add_callback(responses.POST, TOKEN_URL, callback=rotate())
        self.identity.update(data={**TOKENS, "issued_at": int(time.time() - 8 * DAY)})
        *_, fresh = self.install("fresh", issued_ago=DAY)
        *_, legacy = self.install("legacy")  # installed before tokens carried issued_at
        *_, boundary = self.install("boundary", issued_ago=KEEPALIVE_AGE + 60)
        disabled, _, idle = self.install("disabled", issued_ago=30 * DAY)
        disabled.update(status=ObjectStatus.DISABLED)
        _, unlinked, orphan = self.install("orphan", issued_ago=30 * DAY)
        unlinked.update(default_auth_id=None)
        with self.assertLogs(LOGGER, level="INFO") as logs, self.tasks():
            refresh_identities.delay()
        assert sorted(refreshes()) == sorted([TOKENS["refresh_token"], "legacy", "boundary"])
        for identity, name in ((self.identity, TOKENS["refresh_token"]), (legacy, "legacy")):
            data = self.data(identity)
            assert data["refresh_token"] == name + "-r2"
            assert abs(data["issued_at"] - time.time()) < 60
        for identity, name in ((fresh, "fresh"), (idle, "disabled"), (orphan, "orphan")):
            assert self.data(identity)["refresh_token"] == name
        [summary] = [
            r for r in logs.records if r.getMessage() == "sentry-gitea: identity keep-alive"
        ]
        assert (summary.due_count, summary.refreshed_count, summary.failed_count) == (3, 3, 0)
        # A second run finds nothing due.
        responses.calls.reset()
        refresh_identities()
        assert not responses.calls

    @responses.activate
    def test_expired_refresh_token_is_logged_and_the_others_still_refresh(self):
        responses.add_callback(responses.POST, TOKEN_URL, callback=rotate(failing={"lapsed7f3a"}))
        self.identity.update(data={**TOKENS, "issued_at": int(time.time() - 8 * DAY)})
        *_, expired = self.install("lapsed7f3a", issued_ago=40 * DAY)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            refresh_identities()
        assert sorted(refreshes()) == sorted([TOKENS["refresh_token"], "lapsed7f3a"])
        assert self.data(self.identity)["refresh_token"] == TOKENS["refresh_token"] + "-r2"
        assert self.data(expired)["refresh_token"] == "lapsed7f3a"
        [failure] = [
            r for r in logs.records if r.getMessage() == "sentry-gitea: token refresh failed"
        ]
        assert failure.identity_id == expired.id and failure.status == 400
        [summary] = [
            r for r in logs.records if r.getMessage() == "sentry-gitea: identity keep-alive"
        ]
        assert (summary.refreshed_count, summary.failed_count) == (1, 1)
        assert "lapsed7f3a" not in "\n".join(logs.output)

    @responses.activate
    def test_without_active_installations_nothing_happens(self):
        self.identity.update(data={**TOKENS, "issued_at": 0})
        self.integration.update(status=ObjectStatus.DISABLED)
        with self.assertNoLogs(LOGGER, level="INFO"):
            refresh_identities()
        assert not responses.calls

    def test_registered_task_and_the_shipped_schedule_lines_work_with_the_scheduler(self):
        assert refresh_identities.name == "sentry_gitea.integration.refresh_identities"
        assert refresh_identities.namespace.name == "integrations"
        from sentry.conf import server

        lines = SNIPPET.read_text().splitlines()
        assert lines[0] == "# BEGIN sentry-gitea" and lines[-1] == "# END sentry-gitea"
        namespace = {key: value for key, value in vars(server).items() if key.isupper()}
        # Do not mutate the live settings objects.
        namespace["TASKWORKER_SCHEDULES"] = dict(server.TASKWORKER_SCHEDULES)
        exec(compile("\n".join(lines[1:-1]), "sentry.conf.py", "exec"), namespace)
        assert "sentry_gitea.integration.tasks" in namespace["TASKWORKER_IMPORTS"]
        schedule = namespace["TASKWORKER_SCHEDULES"]["gitea-identity-refresh"]
        assert schedule == {
            "task": "integrations:sentry_gitea.integration.refresh_identities",
            "schedule": timedelta(hours=12),
        }
        assert set(server.TASKWORKER_SCHEDULES) < set(namespace["TASKWORKER_SCHEDULES"])
        # `sentry run taskworker-scheduler` resolves "namespace:name" exactly like this.
        runner = ScheduleRunner(app, MagicMock())
        runner.add("gitea-identity-refresh", schedule)
        found = app.taskregistry.get_task(*schedule["task"].split(":"))
        assert found.name == refresh_identities.name
