"""Setup pages rendered from this package's templates inside Sentry's own page layout."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.template import engines


def render_page(request: HttpRequest, name: str, context: Mapping[str, Any]) -> HttpResponse:
    # The package is not a Django app, so Sentry's template loaders cannot find its templates by
    # name; compiling them with Sentry's engine still resolves {% extends %} to Sentry's layouts.
    source = (files("sentry_gitea") / "templates" / "sentry_gitea" / name).read_text("utf-8")
    response = HttpResponse(engines["django"].from_string(source).render(context, request))
    response["Cache-Control"] = "no-store"
    return response
