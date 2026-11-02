"""Install entry points. Sentry 26.10's own setup modal only knows its built-in providers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_GET
from sentry.integrations.pipeline import IntegrationPipeline
from sentry.models.organization import Organization
from sentry.web.helpers import render_to_response


class GiteaIntegrationPipeline(IntegrationPipeline):
    # Sentry's IntegrationPipeline no longer runs provider views; this one does, under its
    # own session key so Sentry's pipeline endpoints never pick it up.
    pipeline_name = "gitea_integration_pipeline"

    def get_pipeline_views(self) -> Sequence[Any]:
        return self.provider.get_pipeline_views()


def _error(request: HttpRequest, message: str, status: int) -> HttpResponse:
    return render_to_response(
        "sentry/pipeline-error.html", {"error": message}, request, status=status
    )


def _organization_choice(organizations: Sequence[Organization]) -> HttpResponse:
    options = format_html_join(
        "",
        '<option value="{}">{}</option>',
        ((organization.slug, organization.name) for organization in organizations),
    )
    response = HttpResponse(
        format_html(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            "<title>Install Gitea</title></head><body><h1>Choose a Sentry organization</h1>"
            '<form method="get"><label for="organization">Organization</label>'
            '<select name="organization" id="organization" required>{}</select>'
            '<button type="submit">Continue</button></form></body></html>',
            options,
        )
    )
    response["Cache-Control"] = "no-store"
    return response


@require_GET
@login_required
def install(request: HttpRequest) -> HttpResponseBase:
    organizations = Organization.objects.get_for_user(request.user, scope="org:integrations")
    if not organizations:
        return _error(
            request, "You must be an organization owner, manager or admin to install Gitea.", 403
        )
    slug = request.GET.get("organization")
    chosen = [o for o in organizations if o.slug == slug] if slug else organizations
    if len(chosen) != 1:
        return _organization_choice(organizations)
    pipeline = GiteaIntegrationPipeline(
        request=request, organization=chosen[0], provider_key="gitea"
    )
    pipeline.initialize()
    return pipeline.current_step()


def setup(request: HttpRequest) -> HttpResponseBase:
    """OAuth callback and owner form, at the redirect URI operators already registered."""
    pipeline = GiteaIntegrationPipeline.get_for_request(request)
    if pipeline is None or not pipeline.is_valid():
        return _error(request, "This Gitea setup expired. Start the installation again.", 400)
    return pipeline.current_step()
