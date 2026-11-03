import json
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import responses
from django.test import override_settings
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.models.group import Group
from sentry.models.grouplink import GroupLink
from sentry.shared_integrations.exceptions import IntegrationError, IntegrationFormError
from sentry.testutils.silo import no_silo_test
from sync_fixtures import gitea_comment, gitea_user
from test_integration_provider import API, BASE, PUBLIC, REPO, TOKEN, IntegrationFixture

from sentry_gitea.render import parse_metadata

ISSUES = API + "/repos/team/repo/issues"
MALLORY = gitea_user(99, "mallory")


def marker(dedup_id):
    return f'<!-- sentry-gitea-meta v1 {{"source": "sentry", "dedup_id": "{dedup_id}"}} -->'


def posts(suffix):
    return [c for c in responses.calls if c.request.method == "POST" and c.request.url == suffix]


def posted_bodies(suffix):
    return [json.loads(c.request.body)["body"] for c in posts(suffix)]


def marker_searches():
    return [
        parse_qs(urlsplit(c.request.url).query)["q"]
        for c in responses.calls
        if c.request.method == "GET" and c.request.url.startswith(ISSUES + "?")
    ]


@no_silo_test
class TestIntegrationIssues(IntegrationFixture):
    def setUp(self):
        super().setUp()
        feature = self.feature("organizations:integrations-issue-basic")
        feature.__enter__()
        self.addCleanup(feature.__exit__, None, None, None)
        latest = patch.object(Group, "get_latest_event", return_value=None)
        latest.start()
        self.addCleanup(latest.stop)

    def dedup_id(self):
        return self.installation._event_data(self.group, None).dedup_id

    def create_fields(self):
        return {
            f["name"]: f for f in self.installation.get_create_issue_config(self.group, self.user)
        }

    @responses.activate
    def test_create_form_and_create_real_endpoint_then_unlink(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        form = self.client.get(self.issue_endpoint(), {"action": "create"})
        assert form.status_code == 200, form.data
        fields = {f["name"]: f for f in form.data["createIssueConfig"]}
        assert {"repo", "title", "description", "labels", "assignee"} <= fields.keys()
        assert "sentry-gitea-meta" not in fields["description"]["default"]
        assert fields["createContext"]["type"] == "hidden"
        responses.get(ISSUES, json=[], headers={"X-Total-Count": "0"})
        responses.get(API + "/repos/team/repo/labels", json=[])
        responses.post(API + "/repos/team/repo/labels", json={"id": 5, "name": "bug"}, status=201)
        responses.post(
            API + "/repos/team/repo/issues",
            json=self.remote_issue(labels=[{"id": 5}], assignees=[{"login": "alice"}]),
            status=201,
        )
        result = self.client.post(
            self.issue_endpoint(),
            {
                "repo": "team/repo",
                "title": "Edited title",
                "description": "Editable body",
                "labels": "bug",
                "assignee": "alice",
                "createContext": fields["createContext"]["default"],
            },
        )
        assert result.status_code == 201, result.data
        assert result.data["url"] == PUBLIC + "/team/repo/issues/7"
        external = ExternalIssue.objects.get(integration_id=self.integration.id)
        assert external.key == "41#7"
        assert external.metadata["display_name"] == "team/repo#7"
        assert external.metadata["url"] == PUBLIC + "/team/repo/issues/7"
        assert self.installation.get_issue_url(external.key) == PUBLIC + "/team/repo/issues/7"
        payload = json.loads(responses.calls[-1].request.body)
        assert payload["title"] == "Edited title"
        assert payload["body"].startswith("Editable body\n\n<!-- sentry-gitea-meta v1 ")
        assert parse_metadata(payload["body"])["dedup_id"] == self.dedup_id()
        assert payload["labels"] == [5] and payload["assignees"] == ["alice"]
        self.org_integration.refresh_from_db()
        assert (
            self.org_integration.config["project_issue_defaults"][str(self.project.id)]["repo"]
            == "team/repo"
        )
        removed = self.client.delete(self.issue_endpoint() + f"?externalIssue={external.id}")
        assert removed.status_code == 204
        assert not GroupLink.objects.filter(group_id=self.group.id, linked_id=external.id).exists()
        assert all(c.request.method != "DELETE" for c in responses.calls)

    @responses.activate
    def test_link_retry_comment_marker_is_idempotent(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        form = self.client.get(self.issue_endpoint(), {"action": "link"})
        assert form.status_code == 200, form.data
        fields = {f["name"]: f for f in form.data["linkIssueConfig"]}
        comment = fields["comment"]["default"]
        assert f"<!-- sentry-gitea-link {self.group.id} -->" in comment
        responses.get(API + "/repos/team/repo/issues/7", json=self.remote_issue())
        comments_url = API + "/repos/team/repo/issues/7/comments"
        responses.get(comments_url, json=[])
        responses.post(comments_url, json=gitea_comment(3, comment), status=201)
        data = {"repo": "team/repo", "externalIssue": "41#7", "comment": comment}
        result = self.client.put(self.issue_endpoint(), data)
        assert result.status_code == 201, result.data
        assert result.data["url"] == PUBLIC + "/team/repo/issues/7"
        external = ExternalIssue.objects.get(integration_id=self.integration.id)
        assert external.metadata["url"] == PUBLIC + "/team/repo/issues/7"
        assert self.installation.get_issue_url(external.key) == PUBLIC + "/team/repo/issues/7"
        responses.replace(
            responses.GET,
            comments_url,
            json=[gitea_comment(3, comment)],
            headers={"X-Total-Count": "1"},
        )
        # Sentry 26.10 answers a repeated link with success and skips after_link_issue.
        retry = self.client.put(self.issue_endpoint(), data)
        assert retry.status_code == 200
        self.installation.after_link_issue(external, data=data)
        assert len([c for c in responses.calls if c.request.method == "POST"]) == 1

    @responses.activate
    def test_create_and_link_default_to_base_url_when_public_url_is_unset(self):
        self.mock_repo()
        # Even a formerly public URL returned by Gitea cannot override current settings.
        remote = self.remote_issue(html_url=PUBLIC + "/team/repo/issues/7")
        responses.post(API + "/repos/team/repo/issues", json=remote, status=201)
        responses.get(API + "/repos/team/repo/issues/7", json=remote)
        for public in [None, ""]:
            with self.subTest(public_url=public), override_settings(GITEA_PUBLIC_URL=public):
                for method in [self.client.post, self.client.put]:
                    result = method(
                        self.issue_endpoint(),
                        {"repo": "team/repo", "title": "Title", "externalIssue": "41#7"},
                    )
                    assert result.status_code == 201, result.data
                    assert result.data["url"] == BASE + "/team/repo/issues/7"
                    external = ExternalIssue.objects.get(integration_id=self.integration.id)
                    assert external.metadata["url"] == BASE + "/team/repo/issues/7"
                    assert self.installation.get_issue_url(external.key) == external.metadata["url"]
                    removed = self.client.delete(
                        self.issue_endpoint() + f"?externalIssue={external.id}"
                    )
                    assert removed.status_code == 204, removed.data

    @responses.activate
    def test_get_issue_and_search_build_urls_from_current_public_setting(self):
        self.mock_repo()
        responses.get(API + "/repos/team/repo/issues/7", json=self.remote_issue())
        responses.get(API + "/repos/team/repo/issues", json=[])
        for html_url in [
            BASE + "/team/repo/issues/7",
            "https://unrelated.example/team/repo/issues/7",
            "javascript:alert(1)",
            None,
        ]:
            remote = self.remote_issue(html_url=html_url, repository={"id": 41})
            responses.replace(responses.GET, API + "/repos/team/repo/issues/7", json=remote)
            responses.replace(
                responses.GET,
                API + "/repos/team/repo/issues",
                json=[remote],
                headers={"X-Total-Count": "1"},
            )
            for public, base in [
                ("https://NEW-PUBLIC.example/browser/", "https://new-public.example/browser"),
                (None, BASE),
                ("", BASE),
            ]:
                with (
                    self.subTest(html_url=html_url, public_url=public),
                    override_settings(GITEA_PUBLIC_URL=public),
                ):
                    expected = base + "/team/repo/issues/7"
                    result = self.installation.get_issue("41#7")
                    assert result["url"] == result["metadata"]["url"] == expected
                    assert self.installation.get_issue_url("41#7") == expected
                    result = self.installation.search_issues("Remote", repo="team/repo")[0]
                    assert result["url"] == result["metadata"]["url"] == expected

    @responses.activate
    def test_get_issue_rejects_inconsistent_numbers_and_repository_ids(self):
        self.mock_repo()
        responses.get(API + "/repos/team/repo/issues/7", json=self.remote_issue())
        for changes in [
            {"number": 8},
            {"number": True},
            {"repository": {"id": 42}},
            {"repository": {}},
        ]:
            with self.subTest(changes=changes):
                responses.replace(
                    responses.GET,
                    API + "/repos/team/repo/issues/7",
                    json=self.remote_issue(**changes),
                )
                with self.assertRaises(IntegrationError):
                    self.installation.get_issue("41#7")
        responses.replace(responses.GET, API + "/repositories/41", json={**REPO, "id": 42})
        with self.assertRaises(IntegrationError):
            self.installation.get_issue("41#7")

    @responses.activate
    def test_create_rejects_issue_from_another_repository(self):
        self.mock_repo()
        responses.post(
            API + "/repos/team/repo/issues",
            json=self.remote_issue(repository={"id": 42}),
            status=201,
        )
        with self.assertRaises(IntegrationError):
            self.installation.create_issue({"repo": "team/repo", "title": "Title"})

    @responses.activate
    def test_link_rejects_pr_repo_mismatch_and_wrong_owner(self):
        self.mock_repo()
        responses.get(API + "/repos/team/repo/issues/7", json=self.remote_issue(pull_request={}))
        for key in ["41#7", "42#7", "team/repo#7"]:
            result = self.client.put(
                self.issue_endpoint(), {"repo": "team/repo", "externalIssue": key}
            )
            assert result.status_code == 400, result.data
        responses.get(
            API + "/repos/elsewhere/repo",
            json={
                **REPO,
                "full_name": "elsewhere/repo",
                "owner": {"id": 999, "login": "elsewhere"},
            },
        )
        result = self.client.post(self.issue_endpoint(), {"repo": "elsewhere/repo", "title": "No"})
        assert result.status_code == 400
        assert not ExternalIssue.objects.filter(integration_id=self.integration.id).exists()

    @responses.activate
    def test_saved_repo_missing_from_choices_and_group_none(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": []})
        self.installation.update_organization_config(
            {"project_issue_defaults": {str(self.project.id): {"repo": "team/repo"}}}
        )
        fields = self.installation.get_create_issue_config(self.group, self.user)
        assert fields[0]["default"] == "team/repo"
        assert ("team/repo", "team/repo") in fields[0]["choices"]
        assert self.installation.get_create_issue_config(None, self.user)
        assert self.installation.get_persisted_default_config_fields() == ["repo"]

    @responses.activate
    def test_issue_errors_are_logged_with_status_without_credentials(self):
        responses.get(API + "/repos/team/repo", status=403, body=TOKEN)
        with self.assertLogs("sentry_gitea.integration", level="WARNING") as logs:
            result = self.client.post(self.issue_endpoint(), {"repo": "team/repo", "title": "Fail"})
        assert result.status_code == 400
        assert TOKEN not in str(logs.output) + str(result.data)
        assert any(getattr(record, "status", None) == 403 for record in logs.records)

    def test_sidebar_url_and_display_name_use_stored_metadata_without_network(self):
        external = ExternalIssue.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            key="41#7",
            title="Title",
            metadata={"url": PUBLIC + "/team/renamed/issues/7", "display_name": "team/renamed#7"},
        )
        assert self.installation.get_issue_url(external.key) == external.metadata["url"]
        assert self.installation.get_issue_display_name(external) == "team/renamed#7"

    @responses.activate
    def test_renamed_saved_repository_does_not_block_the_form(self):
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        responses.get(
            API + "/repos/team/old", status=301, headers={"Location": API + "/repos/team/repo"}
        )
        self.installation.update_organization_config(
            {"project_issue_defaults": {str(self.project.id): {"repo": "team/old"}}}
        )
        fields = self.installation.get_create_issue_config(self.group, self.user)
        assert fields[0]["default"] == "team/repo"
        assert fields[0]["choices"] == [("team/repo", "team/repo")]

    @responses.activate
    def test_saved_repository_that_no_longer_qualifies_is_stale_not_an_error(self):
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        old = {**REPO, "id": 43, "full_name": "team/old"}
        responses.get(API + "/repos/team/old", json=old)
        self.installation.update_organization_config(
            {"project_issue_defaults": {str(self.project.id): {"repo": "team/old"}}}
        )
        for name, change in [
            ("wrong owner", {"json": {**old, "owner": {"id": 99, "login": "elsewhere"}}}),
            ("issues disabled", {"json": {**old, "has_issues": False}}),
            ("external tracker", {"json": {**old, "has_external_tracker": True}}),
            ("forbidden", {"status": 403, "json": {"message": "denied"}}),
            ("removed", {"status": 404, "json": {"message": "not found"}}),
        ]:
            responses.replace(responses.GET, API + "/repos/team/old", **change)
            with self.subTest(name):
                form = self.client.get(self.issue_endpoint(), {"action": "create"})
                assert form.status_code == 200, form.data
                [repo] = [f for f in form.data["createIssueConfig"] if f["name"] == "repo"]
                assert repo["default"] == "team/repo"
                assert [list(choice) for choice in repo["choices"]] == [["team/repo", "team/repo"]]
                link = {f["name"]: f for f in self.installation.get_link_issue_config(self.group)}
                assert link["repo"]["default"] == "team/repo"
                assert link["externalIssue"]["url"].endswith("?repo=team%2Frepo")
                # An explicit selection is still validated and reported.
                with self.assertRaises(IntegrationError):
                    self.installation.get_create_issue_config(
                        self.group, self.user, params={"repo": "team/old"}
                    )
        # Server errors are not mistaken for a stale default.
        responses.replace(responses.GET, API + "/repos/team/old", status=502)
        assert self.client.get(self.issue_endpoint(), {"action": "create"}).status_code == 400

    @responses.activate
    def test_create_retry_recovers_the_installation_issue_by_marker(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        context = self.create_fields()["createContext"]["default"]
        responses.get(ISSUES, json=[], headers={"X-Total-Count": "0"})
        responses.get(API + "/repos/team/repo/labels", json=[{"id": 5, "name": "bug"}])
        # Gitea created the issue but dropped its label (no permission): the request fails.
        responses.post(ISSUES, json=self.remote_issue(labels=[]), status=201)
        data = {
            "repo": "team/repo",
            "title": "Title",
            "description": "Details",
            "labels": "bug",
            "createContext": context,
        }
        failed = self.client.post(self.issue_endpoint(), data)
        assert failed.status_code == 400, failed.data
        assert not ExternalIssue.objects.filter(integration_id=self.integration.id).exists()
        [body] = posted_bodies(ISSUES)
        assert parse_metadata(body)["dedup_id"] == self.dedup_id()
        # The retry finds the issue the installation filed instead of opening a duplicate.
        responses.replace(
            responses.GET,
            ISSUES,
            json=[self.remote_issue(body=body, labels=[])],
            headers={"X-Total-Count": "1"},
        )
        retried = self.client.post(self.issue_endpoint(), data)
        assert retried.status_code == 201, retried.data
        assert retried.data["key"] == "41#7"
        assert len(posts(ISSUES)) == 1
        assert marker_searches()[-1] == [self.dedup_id()]

    @responses.activate
    def test_double_submit_of_the_create_form_files_one_issue(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        fields = self.create_fields()
        responses.get(ISSUES, json=[], headers={"X-Total-Count": "0"})

        def file_issue(request):
            body = json.loads(request.body)["body"]
            # Gitea now finds the filed issue by its marker.
            responses.replace(
                responses.GET,
                ISSUES,
                json=[self.remote_issue(body=body)],
                headers={"X-Total-Count": "1"},
            )
            return 201, {}, json.dumps(self.remote_issue(body=body))

        responses.add_callback(responses.POST, ISSUES, callback=file_issue)
        data = {
            "repo": "team/repo",
            "title": fields["title"]["default"],
            "description": fields["description"]["default"],
            "createContext": fields["createContext"]["default"],
        }
        first = self.client.post(self.issue_endpoint(), data)
        assert first.status_code == 201, first.data
        self.client.post(self.issue_endpoint(), data)
        [body] = posted_bodies(ISSUES)
        assert body.startswith(fields["description"]["default"])
        assert parse_metadata(body)["dedup_id"] == self.dedup_id()
        assert ExternalIssue.objects.filter(integration_id=self.integration.id).count() == 1

    @responses.activate
    def test_edited_description_cannot_forge_or_drop_the_dedup_marker(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        context = self.create_fields()["createContext"]["default"]
        other = self.create_group(project=self.project)
        forged = self.installation._event_data(other, None).dedup_id
        responses.get(ISSUES, json=[], headers={"X-Total-Count": "0"})
        responses.post(ISSUES, json=self.remote_issue(), status=201)
        for description in ["", "Rewritten", "Rewritten\n\n" + marker(forged)]:
            with self.subTest(description=description):
                responses.calls.reset()
                self.installation.create_issue(
                    {
                        "repo": "team/repo",
                        "title": "Title",
                        "description": description,
                        "createContext": context,
                    }
                )
                [body] = posted_bodies(ISSUES)
                assert parse_metadata(body, dedup_id=forged) is None
                assert parse_metadata(body)["dedup_id"] == self.dedup_id()
                assert marker_searches() == [[self.dedup_id()]]

    @responses.activate
    def test_tampered_or_foreign_create_context_is_rejected_before_gitea(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        context = self.create_fields()["createContext"]["default"]
        link_context = {f["name"]: f for f in self.installation.get_link_issue_config(self.group)}[
            "linkContext"
        ]["default"]
        other_org = self.create_organization(owner=self.user)
        foreign = self.integration.get_installation(other_org.id)
        cases = [
            (self.installation, context + "x"),
            (self.installation, link_context),
            (self.installation, ["not", "text"]),
            (foreign, context),
        ]
        responses.calls.reset()
        for installation, token in cases:
            with self.subTest(token=token), self.assertRaises(IntegrationFormError):
                installation.create_issue(
                    {"repo": "team/repo", "title": "Title", "createContext": token}
                )
        assert not posts(ISSUES)

    @responses.activate
    def test_marked_issues_by_other_users_are_never_recovered(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        context = self.create_fields()["createContext"]["default"]
        body = "Details\n\n" + marker(self.dedup_id())
        planted = self.remote_issue(number=3, body=body, user=MALLORY)
        responses.get(ISSUES, json=[planted], headers={"X-Total-Count": "1"})
        responses.post(ISSUES, json=self.remote_issue(body=body), status=201)
        result = self.client.post(
            self.issue_endpoint(),
            {
                "repo": "team/repo",
                "title": "Title",
                "description": "Details",
                "createContext": context,
            },
        )
        assert result.status_code == 201, result.data
        assert result.data["key"] == "41#7"
        assert len(posts(ISSUES)) == 1
        # Without form context there is nothing to recover by: no search, a plain create, and a
        # marker typed into the description is not passed on to Gitea.
        responses.calls.reset()
        assert self.installation.create_issue(
            {"repo": "team/repo", "title": "Title", "description": body}
        )["key"]
        assert [c.request.method for c in responses.calls if c.request.url.startswith(ISSUES)] == [
            "POST"
        ]
        assert posted_bodies(ISSUES) == ["Details"]

    @responses.activate
    def test_backlink_markers_count_only_when_the_installation_wrote_them(self):
        self.mock_repo()
        link = f"<!-- sentry-gitea-link {self.group.id} -->"
        comments = API + "/repos/team/repo/issues/7/comments"
        responses.get(
            comments,
            json=[gitea_comment(3, f"Quoting {link}", user=MALLORY)],
            headers={"X-Total-Count": "1"},
        )
        responses.post(comments, json=gitea_comment(4, f"Linked\n\n{link}"), status=201)
        data = {"comment": f"Linked\n{link}"}
        self.installation.after_link_issue(SimpleNamespace(key="41#7"), data=data)
        assert len(posts(comments)) == 1
        responses.replace(
            responses.GET,
            comments,
            json=[
                gitea_comment(3, f"Quoting {link}", user=MALLORY),
                gitea_comment(4, f"Linked\n\n{link}"),
            ],
            headers={"X-Total-Count": "2"},
        )
        self.installation.after_link_issue(SimpleNamespace(key="41#7"), data=data)
        assert len(posts(comments)) == 1

    @responses.activate
    def test_numeric_lookup_repairs_renamed_repo_and_keeps_canonical_key(self):
        responses.get(API + "/repositories/41", json={**REPO, "full_name": "team/renamed"})
        responses.get(
            API + "/repos/team/renamed/issues/7",
            json=self.remote_issue(html_url=BASE + "/team/renamed/issues/7"),
        )
        result = self.installation.get_issue("41#7")
        assert result["key"] == "41#7"
        assert result["metadata"]["display_name"] == "team/renamed#7"
        assert result["url"] == result["metadata"]["url"] == PUBLIC + "/team/renamed/issues/7"

    @responses.activate
    def test_edited_backlink_uses_signed_context_and_rejects_tampering(self):
        self.mock_repo()
        responses.get(API + "/repos/search", json={"data": [REPO]}, headers={"X-Total-Count": "1"})
        fields = {f["name"]: f for f in self.installation.get_link_issue_config(self.group)}
        context = fields["linkContext"]["default"]
        external = SimpleNamespace(key="41#7")
        responses.get(API + "/repos/team/repo/issues/7/comments", json=[])
        responses.post(API + "/repos/team/repo/issues/7/comments", json={"id": 3}, status=201)
        self.installation.after_link_issue(
            external, data={"comment": "Edited comment", "linkContext": context}
        )
        body = json.loads(responses.calls[-1].request.body)["body"]
        assert body == f"Edited comment\n\n<!-- sentry-gitea-link {self.group.id} -->"
        with self.assertRaises(IntegrationFormError):
            self.installation.after_link_issue(
                external, data={"comment": "No", "linkContext": context + "x"}
            )

    @responses.activate
    def test_backlink_shared_budget_expires_before_a_late_post(self):
        now = [100.0]
        clock = SimpleNamespace(monotonic=lambda: now[0], time=time.time)
        self.mock_repo()

        def slow_comments(request):
            now[0] = 142.0
            return 200, {}, "[]"

        responses.add_callback(
            responses.GET, API + "/repos/team/repo/issues/7/comments", callback=slow_comments
        )
        with (
            patch("sentry_gitea.integration.client.time", clock),
            patch("sentry_gitea.api.client.time", clock),
        ):
            with self.assertRaises(IntegrationError):
                self.installation.after_link_issue(
                    SimpleNamespace(key="41#7"),
                    data={"comment": f"Note\n<!-- sentry-gitea-link {self.group.id} -->"},
                )
        assert all(call.request.method == "GET" for call in responses.calls)
