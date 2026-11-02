"""Opt-in issue synchronization through Sentry's own sync tasks and endpoints."""

import json
from unittest.mock import patch

import responses
from sentry.constants import ObjectStatus
from sentry.integrations.mixins.issues import ResolveSyncAction
from sentry.integrations.services.assignment_source import AssignmentSource
from sentry.models.activity import Activity
from sentry.models.group import Group, GroupStatus
from sentry.models.groupassignee import GroupAssignee
from sentry.models.groupmeta import GroupMeta
from sentry.shared_integrations.exceptions import IntegrationFormError
from sentry.testutils.silo import no_silo_test
from sentry.types.activity import ActivityType
from sentry.users.services.user.service import user_service
from sync_fixtures import WEBHOOK_REPO, SyncFixture, gitea_comment, gitea_user
from test_integration_provider import API

from sentry_gitea.integration.issue_sync import comment_locator, escape_markdown

ISSUE = API + "/repos/team/repo/issues/7"
COMMENTS = ISSUE + "/comments"
OPTIONS = [
    "sync_status_forward",
    "sync_status_reverse",
    "sync_comments",
    "sync_assignee_forward",
    "sync_assignee_reverse",
]


def methods(method):
    return [call for call in responses.calls if call.request.method == method]


class EventlessSyncFixture(SyncFixture):
    """Activity notifications run eagerly under self.tasks(); keep them away from Snuba."""

    def setUp(self):
        super().setUp()
        latest = patch.object(Group, "get_latest_event", return_value=None)
        latest.start()
        self.addCleanup(latest.stop)


@no_silo_test
class TestSyncConfiguration(SyncFixture):
    def details_url(self):
        return f"/api/0/organizations/{self.organization.slug}/integrations/{self.integration.id}/"

    def test_every_option_defaults_to_false_and_is_exposed(self):
        for attribute in ["comment", "outbound_status", "inbound_status"]:
            assert not self.installation.should_sync(attribute)
        assert not self.installation.should_sync("outbound_assignee")
        assert not self.installation.should_sync("inbound_assignee")
        result = self.client.get(self.details_url())
        assert result.status_code == 200, result.data
        fields = {f["name"]: f for f in result.data["configOrganization"]}
        assert set(fields) == {"repository_ids", *OPTIONS, "user_mappings"}
        assert all(fields[name]["type"] == "boolean" for name in OPTIONS)
        assert not any(fields[name]["default"] for name in OPTIONS)
        assert "Add Repository" in fields["sync_status_reverse"]["help"]

    def test_options_and_mappings_are_set_through_the_integration_endpoint(self):
        result = self.client.post(
            self.details_url(),
            {
                "sync_status_forward": True,
                "sync_comments": True,
                "user_mappings": {"Owner@Example.com": "alice", "b@example.com": "bob.b"},
            },
            format="json",
        )
        assert result.status_code == 200, result.data
        self.org_integration.refresh_from_db()
        config = self.org_integration.config
        assert config["sync_status_forward"] is True and config["sync_comments"] is True
        assert config["user_mappings"] == {"owner@example.com": "alice", "b@example.com": "bob.b"}
        installation = self.integration.get_installation(self.organization.id)
        assert installation.should_sync("outbound_status") and installation.should_sync("comment")
        assert not installation.should_sync("inbound_status")
        # The textarea round-trips JSON text; stored data stays an object.
        text = self.client.get(self.details_url()).data["configData"]["user_mappings"]
        assert json.loads(text) == config["user_mappings"]
        assert (
            self.client.post(self.details_url(), {"user_mappings": text}, format="json").status_code
            == 200
        )
        self.org_integration.refresh_from_db()
        assert self.org_integration.config["user_mappings"] == config["user_mappings"]

    def test_invalid_values_are_rejected_without_changes(self):
        too_many = {f"u{i}@example.com": "alice" for i in range(501)}
        for invalid in [
            {"sync_comments": "true"},
            {"sync_status_reverse": 1},
            {"user_mappings": {"not-an-email": "alice"}},
            {"user_mappings": {"a@example.com": "../alice"}},
            {"user_mappings": {"a@example.com": "-alice"}},
            {"user_mappings": {"a@example.com": 5}},
            {"user_mappings": {"A@example.com": "a", "a@example.com": "b"}},
            {"user_mappings": too_many},
            {"user_mappings": "[1, 2]"},
            {"user_mappings": "{broken"},
        ]:
            with self.assertRaises(IntegrationFormError):
                self.installation.update_organization_config(invalid)
            result = self.client.post(self.details_url(), invalid, format="json")
            assert result.status_code == 400, invalid
        self.org_integration.refresh_from_db()
        assert not set(self.org_integration.config) & {*OPTIONS, "user_mappings"}

    def test_echo_guard_and_inactive_installations(self):
        self.enable(sync_assignee_forward=True)
        own = AssignmentSource.from_integration(self.integration)
        other = AssignmentSource(source_name="jira", integration_id=self.integration.id + 1000)
        assert self.installation.should_sync("outbound_assignee")
        assert not self.installation.should_sync("outbound_assignee", own)
        assert self.installation.should_sync("outbound_assignee", other)
        self.org_integration.update(status=ObjectStatus.DISABLED)
        installation = self.integration.get_installation(self.organization.id)
        assert not installation.should_sync("outbound_assignee")


@no_silo_test
class TestStatusSync(EventlessSyncFixture):
    @responses.activate
    def test_resolving_and_unresolving_in_sentry_updates_gitea_once(self):
        external = self.link_issue()
        self.enable(sync_status_forward=True)
        self.mock_issue(state="open")
        responses.patch(ISSUE, json=self.remote_issue(state="closed"), status=201)
        url = f"/api/0/organizations/{self.organization.slug}/issues/?id={self.group.id}"
        with self.tasks():
            result = self.client.put(url, {"status": "resolved"}, format="json")
        assert result.status_code == 200, result.data
        [patch] = methods("PATCH")
        assert json.loads(patch.request.body) == {"state": "closed"}
        responses.replace(responses.GET, ISSUE, json=self.remote_issue(state="closed"))
        installation = self.integration.get_installation(self.organization.id)
        installation.sync_status_outbound(external, True, self.project.id)
        assert len(methods("PATCH")) == 1  # already closed: no write
        installation.sync_status_outbound(external, False, self.project.id)
        assert json.loads(methods("PATCH")[-1].request.body) == {"state": "open"}

    @responses.activate
    def test_forward_disabled_makes_no_requests(self):
        external = self.link_issue()
        self.installation.sync_status_outbound(external, True, self.project.id)
        assert not responses.calls

    @responses.activate
    def test_gitea_close_and_reopen_apply_the_current_state(self):
        self.add_repo()
        self.link_issue()
        # Both directions: the inbound change must not be written back to Gitea.
        self.enable(sync_status_reverse=True, sync_status_forward=True)
        self.mock_issue(state="closed")
        with self.tasks():
            assert self.signed("issues", self.issue_payload("closed")).status_code == 204
        self.group.refresh_from_db()
        assert self.group.status == GroupStatus.RESOLVED
        # A replayed "closed" delivery after Gitea reopened the issue cannot resolve again.
        responses.replace(responses.GET, ISSUE, json=self.remote_issue(state="open"))
        with self.tasks():
            assert self.signed("issues", self.issue_payload("closed")).status_code == 204
        self.group.refresh_from_db()
        assert self.group.status == GroupStatus.UNRESOLVED
        assert not methods("PATCH")

    @responses.activate
    def test_reverse_needs_the_option_and_an_installed_hook(self):
        repo = self.add_repo()
        self.link_issue()
        with self.tasks():
            assert self.signed("issues", self.issue_payload("closed")).status_code == 204
        assert not responses.calls
        self.enable(sync_status_reverse=True)
        repo.update(config={**repo.config, "hook_id": None})
        data = {"issue_key": "41#7", "action": "closed"}
        assert self.installation.get_resolve_sync_action(data) == ResolveSyncAction.NOOP
        assert self.installation.get_resolve_sync_action(
            {"issue_key": "x", "action": "closed"}
        ) == (ResolveSyncAction.NOOP)
        assert not responses.calls
        self.group.refresh_from_db()
        assert self.group.status == GroupStatus.UNRESOLVED


@no_silo_test
class TestAssigneeSync(EventlessSyncFixture):
    @responses.activate
    def test_sentry_assignment_sets_the_mapped_gitea_user_and_unassign_clears(self):
        self.link_issue()
        self.enable(sync_assignee_forward=True, user_mappings={self.user.email: "alice"})
        self.mock_issue(assignees=None)
        responses.patch(ISSUE, json=self.remote_issue(), status=201)
        with self.tasks():
            GroupAssignee.objects.assign(self.group, self.user)
        assert json.loads(methods("PATCH")[-1].request.body) == {"assignees": ["alice"]}
        responses.replace(
            responses.GET, ISSUE, json=self.remote_issue(assignees=[{"id": 5, "login": "Alice"}])
        )
        with self.tasks():
            GroupAssignee.objects.deassign(self.group)
        assert json.loads(methods("PATCH")[-1].request.body) == {"assignees": []}
        assert len(methods("PATCH")) == 2

    @responses.activate
    def test_unmapped_users_change_nothing_and_are_logged(self):
        external = self.link_issue()
        self.enable(sync_assignee_forward=True, user_mappings={"other@example.com": "bob"})
        user = user_service.get_user(self.user.id)
        with self.assertLogs("sentry_gitea.integration.issue_sync", level="WARNING") as logs:
            self.installation.sync_assignee_outbound(external, user)
        assert logs.records[0].reason == "unmapped_user"
        assert logs.records[0].getMessage() == "sentry-gitea: sync failed"
        assert not responses.calls

    @responses.activate
    def test_echo_guard_and_no_redundant_writes(self):
        external = self.link_issue()
        self.enable(sync_assignee_forward=True, user_mappings={self.user.email: "alice"})
        user = user_service.get_user(self.user.id)
        source = AssignmentSource.from_integration(self.integration)
        self.installation.sync_assignee_outbound(external, user, assignment_source=source)
        assert not responses.calls
        self.mock_issue(assignees=[{"id": 5, "login": "alice"}])
        self.installation.sync_assignee_outbound(external, user)
        assert not methods("PATCH")

    def member(self, email, organization=None, verified=True):
        user = self.create_user(email=email)
        if not verified:
            user.emails.update(is_verified=False)
        self.create_member(
            user=user, organization=organization or self.organization, teams=[self.team]
        )
        return user

    @responses.activate
    def test_gitea_assignment_assigns_lowest_mapped_member_without_echo(self):
        self.add_repo()
        self.link_issue()
        zed = self.member("zed@example.com")
        unverified = self.member("ghost@example.com", verified=False)
        outsider = self.create_user(email="outsider@example.com")
        self.enable(
            sync_assignee_reverse=True,
            sync_assignee_forward=True,
            user_mappings={
                self.user.email: "alice",
                zed.email: "zed",
                unverified.email: "ghost",
                outsider.email: "out",
            },
        )
        remote = [
            {"id": 2, "login": "ghost", "email": "2+ghost@noreply.gitea.local"},
            {"id": 3, "login": "out"},
            {"id": 9, "login": "zed"},
            {"id": 4, "login": "nobody"},
            {"id": 5, "login": "ALICE"},
        ]
        self.mock_issue(assignees=remote)
        with self.tasks():
            response = self.signed("issues", self.issue_payload("assigned", assignees=remote[:1]))
        assert response.status_code == 204
        assert GroupAssignee.objects.get(group=self.group).user_id == self.user.id
        assert not methods("PATCH")  # the inbound assignment is not echoed back
        responses.replace(responses.GET, ISSUE, json=self.remote_issue(assignees=None))
        with self.tasks():
            assert self.signed("issues", self.issue_payload("unassigned")).status_code == 204
        assert not GroupAssignee.objects.filter(group=self.group).exists()
        assert not methods("PATCH")

    @responses.activate
    def test_unmapped_gitea_assignees_leave_sentry_unchanged(self):
        self.add_repo()
        self.link_issue()
        self.enable(sync_assignee_reverse=True, user_mappings={self.user.email: "alice"})
        GroupAssignee.objects.assign(self.group, self.user)
        self.mock_issue(assignees=[{"id": 4, "login": "nobody"}])
        # X-Gitea-Event is "issues"; the more specific event type is accepted as well.
        assert self.signed("issue_assign", self.issue_payload("assigned")).status_code == 204
        assert GroupAssignee.objects.get(group=self.group).user_id == self.user.id
        assert any("/issues/7" in c.request.url for c in responses.calls)

    @responses.activate
    def test_two_organizations_apply_their_own_mappings(self):
        other_org = self.create_organization(owner=self.create_user("owner2@example.com"))
        other_team = self.create_team(organization=other_org)
        other_project = self.create_project(organization=other_org, teams=[other_team])
        other_user = self.create_user(email="second@example.com")
        self.create_member(user=other_user, organization=other_org, teams=[other_team])
        self.integration.add_organization(other_org, self.user, default_auth_id=self.identity.id)
        other_group = self.create_group(project=other_project)
        self.add_repo()
        self.add_repo(organization=other_org)
        self.link_issue()
        self.link_issue(group=other_group)
        self.enable(sync_assignee_reverse=True, user_mappings={self.user.email: "alice"})
        self.enable(
            installation=self.integration.get_installation(other_org.id),
            sync_assignee_reverse=True,
            user_mappings={other_user.email: "alice"},
        )
        self.mock_issue(assignees=[{"id": 5, "login": "alice"}])
        assert self.signed("issues", self.issue_payload("assigned")).status_code == 204
        assert GroupAssignee.objects.get(group=self.group).user_id == self.user.id
        assert GroupAssignee.objects.get(group=other_group).user_id == other_user.id


@no_silo_test
class TestCommentSync(EventlessSyncFixture):
    def note(self, text="First", group=None):
        return Activity.objects.create(
            group=group or self.group,
            project=(group or self.group).project,
            type=ActivityType.NOTE.value,
            user_id=self.user.id,
            data={"text": text},
        )

    @responses.activate
    def test_notes_and_edits_flow_through_sentry_tasks_to_one_comment(self):
        self.link_issue()
        self.enable(sync_comments=True)
        self.mock_issue()
        responses.get(COMMENTS, json=[], headers={"X-Total-Count": "0"})
        responses.post(COMMENTS, json=gitea_comment(55, "x"), status=201)
        notes = f"/api/0/organizations/{self.organization.slug}/issues/{self.group.id}/comments/"
        with self.tasks():
            created = self.client.post(notes, {"text": "First **note**"}, format="json")
        assert created.status_code == 201, created.data
        note = Activity.objects.get(id=created.data["id"])
        body = json.loads(methods("POST")[-1].request.body)["body"]
        author = escape_markdown(self.user.get_display_name())
        assert body.startswith(f"**{author}** wrote in Sentry:\n\nFirst **note**\n\n")
        assert body.endswith(f"<!-- sentry-gitea-note {note.id} -->")
        assert note.data["external_id"] == 55
        responses.get(API + "/repos/team/repo/issues/comments/55", json=gitea_comment(55, body))
        responses.patch(
            API + "/repos/team/repo/issues/comments/55", json=gitea_comment(55, body), status=200
        )
        with self.tasks():
            edited = self.client.put(f"{notes}{note.id}/", {"text": "Edited"}, format="json")
        assert edited.status_code == 200, edited.data
        patched = json.loads(methods("PATCH")[-1].request.body)["body"]
        assert "\n\nEdited\n\n" in patched and patched.endswith(f"sentry-gitea-note {note.id} -->")
        assert len(methods("POST")) == 1

    @responses.activate
    def test_retries_recover_the_comment_by_marker(self):
        self.enable(sync_comments=True)
        note = self.note()
        self.mock_issue()
        marker = f"<!-- sentry-gitea-note {note.id} -->"
        responses.get(
            COMMENTS,
            json=[
                gitea_comment(50, f"quoted {marker} in the middle"),
                gitea_comment(51, f"**x** wrote in Sentry:\n\nFirst\n\n{marker}\n"),
            ],
            headers={"X-Total-Count": "2"},
        )
        assert self.installation.create_comment("41#7", self.user.id, note)["id"] == 51
        assert not methods("POST") and not methods("PATCH")
        key = comment_locator(self.integration.id, "41#7", note.id)
        assert len(key) <= 64
        assert GroupMeta.objects.get(group=self.group, key=key).value == "51"

    @responses.activate
    def test_edits_never_touch_comments_without_our_marker(self):
        self.enable(sync_comments=True)
        note = self.note("Edited")
        key = comment_locator(self.integration.id, "41#7", note.id)
        GroupMeta.objects.set_value(self.group, key, "60")
        self.mock_issue()
        responses.get(
            API + "/repos/team/repo/issues/comments/60", json=gitea_comment(60, "Human text")
        )
        responses.get(
            COMMENTS, json=[gitea_comment(60, "Human text")], headers={"X-Total-Count": "1"}
        )
        responses.post(COMMENTS, json=gitea_comment(61, "x"), status=201)
        self.installation.update_comment("41#7", self.user.id, note)
        assert not methods("PATCH")
        assert len(methods("POST")) == 1
        assert GroupMeta.objects.get(group=self.group, key=key).value == "61"

    @responses.activate
    def test_each_linked_issue_has_its_own_locator_and_markers_cannot_be_spoofed(self):
        self.enable(sync_comments=True)
        note = self.note("Look: <!-- sentry-gitea-note 999 --> and <!--SENTRY-GITEA-x -->")
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        for number, comment_id in ((7, 70), (8, 80)):
            responses.get(
                f"{API}/repos/team/repo/issues/{number}/comments",
                json=[],
                headers={"X-Total-Count": "0"},
            )
            responses.post(
                f"{API}/repos/team/repo/issues/{number}/comments",
                json=gitea_comment(comment_id, "x", number=number),
            )
            self.installation.create_comment(f"41#{number}", self.user.id, note)
        bodies = [json.loads(c.request.body)["body"] for c in methods("POST")]
        assert all("&lt;!-- sentry-gitea-note 999" in b for b in bodies)
        assert all("&lt;!--SENTRY-GITEA-x" in b for b in bodies)
        assert all(b.endswith(f"<!-- sentry-gitea-note {note.id} -->") for b in bodies)
        keys = GroupMeta.objects.filter(group=self.group, key__startswith="gitea:comment:")
        assert sorted(keys.values_list("value", flat=True)) == ["70", "80"]

    @responses.activate
    def test_markers_in_other_users_comments_are_never_adopted_or_edited(self):
        self.enable(sync_comments=True)
        note = self.note("Edited")
        marker = f"<!-- sentry-gitea-note {note.id} -->"
        planted = gitea_comment(60, f"Planted\n\n{marker}", user=gitea_user(99, "mallory"))
        key = comment_locator(self.integration.id, "41#7", note.id)
        GroupMeta.objects.set_value(self.group, key, "60")
        self.mock_issue()
        responses.get(API + "/repos/team/repo/issues/comments/60", json=planted)
        responses.get(COMMENTS, json=[planted], headers={"X-Total-Count": "1"})
        responses.post(COMMENTS, json=gitea_comment(61, "x"), status=201)
        # Neither the stored ID nor a marker search hands another user's comment over.
        self.installation.update_comment("41#7", self.user.id, note)
        assert not methods("PATCH")
        assert len(methods("POST")) == 1
        assert GroupMeta.objects.get(group=self.group, key=key).value == "61"
        # A first sync (no stored ID) does not adopt it either.
        GroupMeta.objects.filter(group=self.group, key=key).delete()
        self.installation.create_comment("41#7", self.user.id, note)
        assert len(methods("POST")) == 2 and not methods("PATCH")

    @responses.activate
    def test_disabled_comment_sync_does_nothing(self):
        note = self.note()
        assert self.installation.create_comment("41#7", self.user.id, note) is None
        assert self.installation.update_comment("41#7", self.user.id, note) is None
        assert not responses.calls
