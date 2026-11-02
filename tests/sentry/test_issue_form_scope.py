"""Issue forms offer only the repository selection and default to the project's repository."""

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import responses
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.models.group import Group
from sentry.testutils.silo import no_silo_test
from test_integration_provider import API, REPO, IntegrationFixture

# Gitea lists alphabetically, and an unselected repository sorts first.
ALPHA = {**REPO, "id": 42, "name": "alpha", "full_name": "team/alpha"}
WEB = {**REPO, "id": 43, "name": "web", "full_name": "team/web"}
LISTED = [ALPHA, REPO, WEB]
SELECTED = [41, 43]
NOT_SELECTED = "Select a repository from the Gitea integration's repository selection."
SEARCH = "/extensions/gitea/search/{org}/{integration}/"


@no_silo_test
class TestIssueFormScope(IntegrationFixture):
    def setUp(self):
        super().setUp()
        feature = self.feature("organizations:integrations-issue-basic")
        feature.__enter__()
        self.addCleanup(feature.__exit__, None, None, None)
        latest = patch.object(Group, "get_latest_event", return_value=None)
        latest.start()
        self.addCleanup(latest.stop)

    def mock_owner(self):
        responses.get(API + "/repos/search", json={"data": LISTED}, headers={"X-Total-Count": "3"})
        for row in LISTED:
            responses.get(f"{API}/repos/{row['full_name']}", json=row)
            responses.get(f"{API}/repositories/{row['id']}", json=row)

    def store(self, **config):
        self.org_integration.update(config={**self.org_integration.config, **config})
        self.installation = self.integration.get_installation(self.organization.id)

    def remember(self, repo):
        self.store(project_issue_defaults={str(self.project.id): {"repo": repo}})

    def map_code_to(self, row, project=None):
        repo = self.create_repo(
            project=project or self.project,
            name=row["full_name"],
            provider="integrations:gitea",
            integration_id=self.integration.id,
            external_id=str(row["id"]),
        )
        self.create_code_mapping(
            project=project or self.project,
            repo=repo,
            organization_integration=self.org_integration,
        )

    def repo_field(self, action):
        form = self.client.get(self.issue_endpoint(), {"action": action})
        assert form.status_code == 200, form.data
        key = "createIssueConfig" if action == "create" else "linkIssueConfig"
        fields = {f["name"]: f for f in form.data[key]}
        return fields

    def defaults(self):
        create = self.repo_field("create")["repo"]
        link = self.repo_field("link")
        issue_search = parse_qs(urlsplit(link["externalIssue"]["url"]).query)["repo"]
        assert link["repo"]["default"] == create["default"]
        assert issue_search == [create["default"]]
        return create["default"]

    def choices(self, action):
        return [value for value, _ in self.repo_field(action)["repo"]["choices"]]

    def search_repositories(self):
        result = self.client.get(
            SEARCH.format(org=self.organization.slug, integration=self.integration.id),
            {"field": "repo", "query": "team"},
        )
        assert result.status_code == 200, result.data
        return [row["value"] for row in result.data]

    def gitea_writes(self):
        return [c for c in responses.calls if c.request.method in {"POST", "PATCH", "PUT"}]

    @responses.activate
    def test_forms_and_search_offer_only_the_selected_repositories(self):
        self.mock_owner()
        self.store(repository_ids=SELECTED)
        assert self.choices("create") == ["team/repo", "team/web"]
        assert self.choices("link") == ["team/repo", "team/web"]
        assert self.search_repositories() == ["team/repo", "team/web"]

    @responses.activate
    def test_forms_find_selected_repositories_past_the_first_two_pages(self):
        filler = [
            {**REPO, "id": 1000 + n, "name": f"a{n:03}", "full_name": f"team/a{n:03}"}
            for n in range(100)
        ]
        pages = {"1": filler[:50], "2": filler[50:], "3": [WEB]}

        def page(request):
            rows = pages[parse_qs(urlsplit(request.url).query)["page"][0]]
            return 200, {"X-Total-Count": "101"}, json.dumps({"data": rows})

        responses.add_callback(responses.GET, API + "/repos/search", callback=page)
        self.store(repository_ids=[43])
        assert self.choices("create") == ["team/web"]
        assert self.search_repositories() == ["team/web"]

    @responses.activate
    def test_forms_offer_every_repository_without_a_stored_selection(self):
        self.mock_owner()
        assert "repository_ids" not in self.org_integration.config
        assert self.choices("create") == ["team/alpha", "team/repo", "team/web"]
        assert self.search_repositories() == ["team/alpha", "team/repo", "team/web"]

    @responses.activate
    def test_create_link_and_issue_search_reject_an_unselected_repository(self):
        self.mock_owner()
        responses.get(API + "/repos/team/alpha/issues", json=[], headers={"X-Total-Count": "0"})
        responses.get(API + "/repos/team/alpha/issues/7", json=self.remote_issue())
        responses.post(API + "/repos/team/alpha/issues", json=self.remote_issue(), status=201)
        self.store(repository_ids=SELECTED)
        create = self.client.post(
            self.issue_endpoint(), {"repo": "team/alpha", "title": "Forged", "description": "x"}
        )
        assert create.status_code == 400, create.data
        link = self.client.put(
            self.issue_endpoint(), {"repo": "team/alpha", "externalIssue": "42#7"}
        )
        assert link.status_code == 400, link.data
        unselected_key = self.client.put(self.issue_endpoint(), {"externalIssue": "42#7"})
        assert unselected_key.status_code == 400, unselected_key.data
        issues = self.client.get(
            SEARCH.format(org=self.organization.slug, integration=self.integration.id),
            {"field": "externalIssue", "repo": "team/alpha", "query": "7"},
        )
        assert issues.status_code == 400, issues.data
        assert not ExternalIssue.objects.filter(integration_id=self.integration.id).exists()
        assert self.gitea_writes() == []
        explicit = self.client.get(
            self.issue_endpoint(), {"action": "create", "repo": "team/alpha"}
        )
        assert explicit.status_code == 400, explicit.data
        assert NOT_SELECTED in str(explicit.data)
