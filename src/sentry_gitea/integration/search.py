"""Normal Sentry organization permissions protect these async issue-form choices."""

from typing import Any

from rest_framework.response import Response
from sentry.api.base import control_silo_endpoint
from sentry.integrations.source_code_management.search import SourceCodeSearchEndpoint
from sentry.shared_integrations.exceptions import IntegrationError, IntegrationFormError

from .installation import GiteaIntegration
from .utils import parse_issue_key


@control_silo_endpoint
class GiteaSearchEndpoint(SourceCodeSearchEndpoint):
    repository_field = "repo"
    integration_provider = "gitea"
    installation_class = GiteaIntegration

    def handle_search_issues(
        self, installation: GiteaIntegration, query: str, repo: str | None
    ) -> Response:
        try:
            issues = installation.search_issues(query, repo=repo)
            return Response(
                [
                    {
                        "label": f"#{parse_issue_key(issue['key'])[1]} {issue['title']}",
                        "value": issue["key"],
                    }
                    for issue in issues
                ]
            )
        except IntegrationFormError as error:
            return Response(error.field_errors, status=400)
        except IntegrationError as error:
            return Response({"detail": str(error)}, status=400)

    def handle_search_repositories(
        self, integration: Any, installation: GiteaIntegration, query: str
    ) -> Response:
        try:
            with installation._issue_operation():
                repos = installation.issue_repositories(query, page_number_limit=2)
            return Response(
                [{"label": repo["full_name"], "value": repo["full_name"]} for repo in repos]
            )
        except IntegrationFormError as error:
            return Response(error.field_errors, status=400)
        except IntegrationError as error:
            return Response({"detail": str(error)}, status=400)
