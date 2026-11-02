"""OAuth for a self-hosted monolith. State and PKCE handling are intentionally local."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpRequest, HttpResponseBase, HttpResponseRedirect
from django.urls import reverse
from sentry.identity.oauth2 import OAuth2Provider
from sentry.identity.services.identity import identity_service
from sentry.locks import locks
from sentry.shared_integrations.exceptions import ApiUnauthorized, IntegrationError
from sentry.utils.http import absolute_uri
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea.api.client import (
    REQUEST_ALLOWANCE,
    GiteaClient,
    GiteaOAuthClient,
    issue_number,
    normalize_base_url,
)
from sentry_gitea.api.errors import GiteaApiError

from .utils import OAUTH_SCOPES, normalize_tokens, stamp_issued, validate_scopes

logger = logging.getLogger(__name__)
REFRESH_LOCK_WAIT = 45


class ConfigurationError(IntegrationError):
    """Only static operator-facing messages belong in this exception."""


@dataclass(frozen=True, repr=False)
class OAuthSettings:
    base_url: str
    public_url: str
    client_id: str
    client_secret: str

    @classmethod
    def load(cls) -> OAuthSettings:
        required = ("GITEA_BASE_URL", "GITEA_CLIENT_ID", "GITEA_CLIENT_SECRET")
        missing = [key for key in required if not getattr(settings, key, "")]
        if missing:
            raise ConfigurationError("Configure " + ", ".join(missing) + " in sentry.conf.py.")
        try:
            base = normalize_base_url(settings.GITEA_BASE_URL)
            public = normalize_base_url(getattr(settings, "GITEA_PUBLIC_URL", None) or base)
        except ValueError:
            raise ConfigurationError(
                "GITEA_BASE_URL and GITEA_PUBLIC_URL must be valid HTTP(S) URLs."
            ) from None
        if any(not isinstance(getattr(settings, key), str) for key in required[1:]):
            raise ConfigurationError("GITEA_CLIENT_ID and GITEA_CLIENT_SECRET must be strings.")
        return cls(base, public, settings.GITEA_CLIENT_ID, settings.GITEA_CLIENT_SECRET)

    def oauth_client(self, deadline: float | None = None) -> GiteaOAuthClient:
        return GiteaOAuthClient(
            self.base_url, self.client_id, self.client_secret, deadline=deadline
        )


def setup_path() -> str:
    return reverse("sentry-gitea-setup")


def log_failure(log: logging.Logger, prefix: str, error: Exception, **ids: Any) -> None:
    # No exc_info: external exceptions and traceback locals may contain credentials.
    log.warning(
        prefix,
        extra={
            **ids,
            "status": getattr(error, "status", getattr(error, "code", None)),
            "error_type": type(error).__name__,
        },
    )


def fail_pipeline(pipeline: Any, message: str) -> HttpResponseBase:
    pipeline.clear_session()
    parent = getattr(pipeline, "parent_pipeline", None)
    if parent is not None:
        parent.clear_session()
    return pipeline.error(message)


def identity_from_state(
    base_url: str,
    user: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    tokens = state.get("tokens")
    if (
        not isinstance(tokens, Mapping)
        or any(
            not isinstance(tokens.get(key), str) or not tokens[key]
            for key in ("access_token", "refresh_token")
        )
        or type(tokens.get("expires")) is not int
    ):
        raise ValueError("Gitea authorization is incomplete; start setup again.")
    data = {key: tokens[key] for key in ("access_token", "refresh_token", "expires")}
    if type(tokens.get("issued_at")) is int:
        data["issued_at"] = tokens["issued_at"]
    return {
        "type": "gitea",
        "external_id": f"{base_url}|{issue_number(user.get('id'))}",
        "data": data,
        "scopes": validate_scopes(state.get("scopes")),
    }


class OAuthAuthorizeView:
    def dispatch(self, request: HttpRequest, pipeline: Any) -> HttpResponseBase:
        try:
            config = OAuthSettings.load()
            scopes = validate_scopes(OAUTH_SCOPES)
        except (ConfigurationError, ValueError) as error:
            log_failure(logger, "sentry-gitea: oauth failed", error)
            return fail_pipeline(pipeline, str(error))
        if any(key in request.GET for key in ("code", "state", "error")):
            return pipeline.next_step()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        redirect_uri = absolute_uri(setup_path())
        for key, value in {
            "oauth_state": state,
            "code_verifier": verifier,
            "oauth_started": time.time(),
            "scopes": scopes,
            "redirect_uri": redirect_uri,
        }.items():
            pipeline.bind_state(key, value)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        params = {
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
        }
        return HttpResponseRedirect(
            config.public_url + "/login/oauth/authorize?" + urlencode(params)
        )


class OAuthCallbackView:
    def dispatch(self, request: HttpRequest, pipeline: Any) -> HttpResponseBase:
        try:
            config = OAuthSettings.load()
            expected = pipeline.fetch_state("oauth_state")
            received = request.GET.get("state", "")
            verifier = pipeline.fetch_state("code_verifier")
            started = pipeline.fetch_state("oauth_started")
            code = request.GET.get("code", "")
            if (
                request.method != "GET"
                or "error" in request.GET
                or not code
                or len(request.GET.getlist("code")) != 1
                or len(request.GET.getlist("state")) != 1
                or not isinstance(expected, str)
                or not expected
                or len(received) > 512
                or not hmac.compare_digest(expected.encode(), received.encode())
                or not isinstance(verifier, str)
                or not verifier
                or not isinstance(started, (float, int))
                or not 0 <= time.time() - started <= 600
            ):
                raise ValueError("Invalid OAuth callback.")
            validate_scopes(pipeline.fetch_state("scopes"))
            redirect_uri = pipeline.fetch_state("redirect_uri")
            if redirect_uri != absolute_uri(setup_path()):
                raise ValueError("OAuth redirect configuration changed.")
            # Consume before exchanging, so a callback cannot be replayed after an upstream failure.
            pipeline.bind_state("oauth_state", None)
            pipeline.bind_state("code_verifier", None)
            with config.oauth_client() as client:
                tokens = stamp_issued(
                    normalize_tokens(client.exchange_code(code, redirect_uri, verifier))
                )
            pipeline.bind_state("tokens", tokens)
        except (ConfigurationError, ValueError, GiteaApiError) as error:
            log_failure(logger, "sentry-gitea: oauth failed", error)
            return fail_pipeline(
                pipeline, "Gitea authorization failed. Start setup again and approve access."
            )
        return pipeline.next_step()


class GiteaIdentityProvider(OAuth2Provider):
    key = "gitea"
    name = "Gitea"
    oauth_scopes = OAUTH_SCOPES

    def get_pipeline_views(self) -> list[Any]:
        return [OAuthAuthorizeView(), OAuthCallbackView()]

    def build_identity(self, state: Mapping[str, Any]) -> dict[str, Any]:
        try:
            config = OAuthSettings.load()
            with GiteaClient(
                config.base_url, state["tokens"]["access_token"], token_type="Bearer"
            ) as client:
                result = identity_from_state(config.base_url, client.get_user(), state)
        except (ConfigurationError, GiteaApiError, ValueError, KeyError, TypeError) as error:
            log_failure(logger, "sentry-gitea: oauth failed", error)
            raise IntegrationError("Gitea authorization is incomplete. Authorize again.") from None
        result["id"] = result.pop("external_id")
        return result

    def get_refresh_token_params(
        self, refresh_token: str, identity: Any, **kwargs: Any
    ) -> dict[str, str]:
        config = OAuthSettings.load()
        return {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        }

    def get_refresh_token_url(self) -> str:
        return OAuthSettings.load().base_url + "/login/oauth/access_token"

    def refresh_identity(self, identity: Any, **kwargs: Any) -> None:
        failed_token = kwargs.get("failed_access_token", identity.data.get("access_token"))
        # A monotonic time by which the refresh, lock wait and OAuth request included, must end.
        deadline: float | None = kwargs.get("deadline")
        lock_wait = REFRESH_LOCK_WAIT
        if deadline is not None:
            lock_wait = min(lock_wait, deadline - time.monotonic() - REQUEST_ALLOWANCE)
        try:
            if lock_wait <= 0:
                raise ValueError("No time left to refresh the token.")
            config = OAuthSettings.load()
            lock = locks.get(
                f"sentry-gitea:identity:{identity.id}", duration=90, name="gitea_token_refresh"
            )
            with lock.blocking_acquire(initial_delay=0.1, timeout=lock_wait):
                current = identity_service.get_identity(filter={"id": identity.id})
                if current is None or not current.data.get("refresh_token"):
                    raise ValueError("Missing identity.")
                if current.external_id.rsplit("|", 1)[0] != config.base_url:
                    raise ValueError("Gitea instance changed; reinstall the integration.")
                if current.data.get("access_token") != failed_token:
                    return
                with config.oauth_client(deadline) as client:
                    data = stamp_issued(
                        normalize_tokens(
                            client.refresh_token(current.data["refresh_token"]),
                            previous=current.data,
                        )
                    )
                if identity_service.update_data(identity_id=identity.id, data=data) is None:
                    raise ValueError("Missing identity.")
        except (ConfigurationError, GiteaApiError, ValueError, UnableToAcquireLock) as error:
            log_failure(
                logger, "sentry-gitea: token refresh failed", error, identity_id=identity.id
            )
            raise ApiUnauthorized(
                "Gitea authorization could not be renewed. Reinstall the integration."
            ) from None
