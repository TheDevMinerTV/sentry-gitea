"""Installation-scoped adapter; only reads can retry after an OAuth rotation."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from sentry.identity.services.identity import identity_service
from sentry.integrations.source_code_management.repository import RepositoryClient
from sentry.shared_integrations.exceptions import (
    ApiConflictError,
    ApiError,
    ApiForbiddenError,
    ApiInvalidRequestError,
    ApiRateLimitedError,
    ApiUnauthorized,
    IntegrationError,
    IntegrationFormError,
)

from sentry_gitea.api.client import (
    OPERATION_BUDGET,
    CommitRange,
    GiteaClient,
    git_ref,
    issue_number,
    normalize_repo,
    repository_file_path,
)
from sentry_gitea.api.errors import GiteaApiError
from sentry_gitea.render import parse_label_names

from .identity import GiteaIdentityProvider, OAuthSettings, log_failure
from .utils import identity_user_id, token_expiring

logger = logging.getLogger(__name__)


def api_error(error: GiteaApiError) -> ApiError:
    kinds = {
        401: ApiUnauthorized,
        403: ApiForbiddenError,
        429: ApiRateLimitedError,
        409: ApiConflictError,
        400: ApiInvalidRequestError,
        422: ApiInvalidRequestError,
    }
    messages = {
        401: "Gitea authorization expired. Reinstall the integration.",
        403: "Gitea denied access. Check repository permissions and OAuth scopes.",
        404: "Gitea repository or issue was not found.",
        409: "Gitea reported a conflicting change.",
        429: "Gitea rate limit reached. Retry later.",
        201: (
            "Gitea created the issue but did not apply all labels or assignees. "
            "Check permissions and link the created issue."
        ),
    }
    message = messages.get(
        error.status,
        "Gitea could not complete the request. Check its availability and the submitted fields.",
    )
    if error.status == 200 and "pull request" in error.message:
        message = "Select an issue; pull requests cannot be linked."
    return kinds.get(error.status, ApiError)(json.dumps({"message": message}), code=error.status)


class GiteaIntegrationClient(RepositoryClient):
    # Callers on sync, webhook and repository paths switch this to their contract prefix.
    failure_message = "sentry-gitea: issue operation failed"

    def __init__(self, installation: Any, identity: Any):
        self.installation = installation
        self.identity_id = identity.id
        # The Gitea user behind the installation's tokens. Only content this user wrote
        # (issue metadata markers, backlink and note markers) is trusted; None trusts nothing.
        self.author_id = identity_user_id(getattr(identity, "external_id", None))
        # At most one refresh (proactive or after a 401) per client, i.e. per operation.
        self.refresh_attempted = False
        self.metadata = installation.model.metadata
        self.base_url = self.metadata["base_url"]
        self.repositories: dict[str, dict[str, Any]] = {}
        # One deadline across repository verification, paginated reads and writes. In particular,
        # a delayed refresh may not admit a backlink POST after its lock lease has elapsed.
        self.deadline = time.monotonic() + OPERATION_BUDGET
        # A refresh must never send an old instance's credentials to a newly configured server.
        if OAuthSettings.load().base_url != self.metadata["base_url"]:
            raise IntegrationError("The Gitea instance changed. Reinstall the integration.")

    def access_token(self) -> str:
        """For git over HTTPS, which cannot go through `_call`'s refresh-and-retry."""
        return self._current_identity().data["access_token"]

    def extend_deadline(self, seconds: float) -> None:
        """Long background operations (release ranges, backfills) get their own budget."""
        self.deadline = time.monotonic() + seconds

    def _identity(self) -> Any:
        identity = identity_service.get_identity(filter={"id": self.identity_id})
        if identity is None or not identity.data.get("access_token"):
            raise ApiUnauthorized("Gitea identity is missing. Reinstall the integration.")
        return identity

    def _current_identity(self) -> Any:
        """The stored identity, refreshed first when its access token is about to expire."""
        identity = self._identity()
        if self.refresh_attempted or not token_expiring(identity.data, now=time.time()):
            return identity
        self.refresh_attempted = True
        try:
            GiteaIdentityProvider().refresh_identity(
                identity, failed_access_token=identity.data["access_token"]
            )
        except ApiUnauthorized:
            # Logged by refresh_identity. An expired token cannot succeed: say "reinstall" now.
            if identity.data["expires"] <= time.time():
                raise
            return identity
        return self._identity()

    def _call(
        self,
        method: str,
        *args: Any,
        write: bool = False,
        expected: frozenset[int] = frozenset(),
        **kwargs: Any,
    ) -> Any:
        for attempt in range(2):
            identity = self._current_identity()
            token = identity.data["access_token"]
            try:
                with GiteaClient(
                    self.metadata["base_url"], token, token_type="Bearer", deadline=self.deadline
                ) as client:
                    return getattr(client, method)(*args, **kwargs)
            except GiteaApiError as error:
                if error.status == 401 and attempt == 0 and not self.refresh_attempted:
                    self.refresh_attempted = True
                    GiteaIdentityProvider().refresh_identity(identity, failed_access_token=token)
                    if not write:
                        continue
                    # Renew for the next explicit action, but never replay an ambiguous write.
                if error.status not in expected:
                    log_failure(
                        logger,
                        self.failure_message,
                        error,
                        integration_id=self.installation.model.id,
                        identity_id=self.identity_id,
                    )
                raise api_error(error) from None
        raise AssertionError("Read retry exhausted.")

    def _verify_repository(self, data: dict[str, Any], *, issues: bool = True) -> dict[str, Any]:
        owner = data.get("owner")
        if not isinstance(owner, dict) or str(owner.get("id")) != str(self.metadata["owner_id"]):
            raise IntegrationFormError(
                {"repo": ["Select a repository under the installed Gitea owner."]}
            )
        try:
            issue_number(data.get("id"))
            path = normalize_repo(data.get("full_name"))
        except ValueError:
            raise IntegrationError("Gitea returned an invalid repository.") from None
        if issues and (data.get("has_issues") is False or data.get("has_external_tracker") is True):
            raise IntegrationFormError(
                {"repo": ["Enable Gitea's built-in issue tracker for this repository."]}
            )
        self.repositories[path.casefold()] = data
        return data

    def get_user(self) -> dict[str, Any]:
        return self._call("get_user")

    def get_repo(self, *, repo: str) -> dict[str, Any]:
        try:
            repo = normalize_repo(repo)
        except ValueError:
            raise IntegrationFormError({"repo": ["Select a repository as owner/name."]}) from None
        if repo.casefold() not in self.repositories:
            data = self._verify_repository(self._call("get_repo", repo=repo))
            if data["full_name"].casefold() != repo.casefold():
                raise IntegrationFormError({"repo": ["Repository changed; select it again."]})
        return self._verify_repository(self.repositories[repo.casefold()])

    def get_repo_by_id(self, repo_id: int) -> dict[str, Any]:
        data = self._verify_repository(self._call("get_repo_by_id", repo_id))
        if str(data["id"]) != str(repo_id):
            raise IntegrationError("Gitea returned a different repository.")
        return data

    def search_repositories(
        self, query: str | None = None, page_number_limit: int | None = None, *, issues: bool = True
    ) -> list[dict[str, Any]]:
        rows = self._call(
            "search_repositories",
            self.metadata["owner_id"],
            query=query,
            page_number_limit=page_number_limit,
        )
        # Do not trust the search filter as the authorization boundary.
        return [
            self._verify_repository(row, issues=issues)
            for row in rows
            if isinstance(row.get("owner"), dict)
            and str(row["owner"].get("id")) == str(self.metadata["owner_id"])
            and (
                not issues
                or (
                    row.get("has_issues") is not False
                    and row.get("has_external_tracker") is not True
                )
            )
        ]

    def get_issue(self, number: int, *, repo: str) -> dict[str, Any]:
        self.get_repo(repo=repo)
        return self._call("get_issue", number, repo=repo)

    def authored(self, row: Any) -> bool:
        """True for an issue or comment written by the installation's Gitea user."""
        user = row.get("user") if isinstance(row, dict) else None
        return (
            self.author_id is not None
            and isinstance(user, dict)
            and type(user.get("id")) is int
            and user["id"] == self.author_id
        )

    def find_marked_issue(self, dedup_id: str, *, repo: str) -> dict[str, Any] | None:
        """An issue carrying our metadata marker for `dedup_id`, written by the installation."""
        self.get_repo(repo=repo)
        if self.author_id is None:
            return None
        issue = self._call("search_issues_by_marker", dedup_id, author_id=self.author_id, repo=repo)
        # Anyone can copy a marker into an issue; trust only the installation's own issues.
        return issue if issue is not None and self.authored(issue) else None

    def search_issues(self, query: str | None, *, repo: str) -> list[dict[str, Any]]:
        self.get_repo(repo=repo)
        return self._call("search_issues", query, repo=repo)

    def create_issue(self, title: str, body: str, *, repo: str, **kwargs: Any) -> dict[str, Any]:
        self.get_repo(repo=repo)
        return self._call("create_issue", title, body, repo=repo, write=True, **kwargs)

    def resolve_label_ids(self, names: list[str], *, repo: str) -> list[int]:
        self.get_repo(repo=repo)
        names = parse_label_names(names)
        if not names:
            return []

        # Label pages are read only until every wanted name is found.
        known = self._call("label_ids_by_name", names, repo=repo)
        for name in names:
            if name in known:
                continue
            try:
                label = self._call("create_label", name, repo=repo, write=True)
            except ApiError as error:
                if error.code not in {409, 422}:
                    raise
                # Another writer may have created the label. Re-read, never replay its POST.
                known.update(self._call("label_ids_by_name", [name], repo=repo))
                if name not in known:
                    raise error from None
            else:
                if type(label.get("id")) is not int or label["id"] <= 0:
                    raise IntegrationError("Gitea returned an invalid label ID.")
                known[name] = label["id"]
        return [known[name] for name in names]

    def list_comments(self, number: int, *, repo: str) -> list[dict[str, Any]]:
        self.get_repo(repo=repo)
        return self._call("list_comments", number, repo=repo)

    def create_comment(self, number: int, body: str, *, repo: str) -> dict[str, Any]:
        self.get_repo(repo=repo)
        return self._call("create_comment", number, body, repo=repo, write=True)

    def update_issue(self, number: int, *, repo: str, **changes: Any) -> dict[str, Any]:
        self.get_repo(repo=repo)
        return self._call("update_issue", number, repo=repo, write=True, **changes)

    def get_comment(self, comment_id: int, *, repo: str) -> dict[str, Any] | None:
        self.get_repo(repo=repo)
        try:
            return self._call("get_comment", comment_id, repo=repo, expected=frozenset({404}))
        except ApiError as error:
            if error.code == 404:
                return None
            raise

    def update_comment(self, comment_id: int, body: str, *, repo: str) -> dict[str, Any]:
        self.get_repo(repo=repo)
        return self._call("update_comment", comment_id, body, repo=repo, write=True)

    # Source code management: repositories are identified by their numeric Gitea ID.

    def get_scm_repo(self, *, path: str | None = None, repo_id: int | None = None) -> dict:
        """A repository under the installed owner, whether or not its issue tracker is enabled."""
        if repo_id is not None:
            data = self._verify_repository(self._call("get_repo_by_id", repo_id), issues=False)
            if str(data["id"]) != str(repo_id):
                raise IntegrationError("Gitea returned a different repository.")
            return data
        try:
            path = normalize_repo(path)
        except ValueError:
            raise IntegrationError("Select a Gitea repository as owner/name.") from None
        data = self._verify_repository(self._call("get_repo", repo=path), issues=False)
        if data["full_name"].casefold() != path.casefold():
            raise IntegrationError("The Gitea repository moved; select it again.")
        return data

    def repository_path(self, repo: Any) -> str:
        """Current owner/name of a Sentry Repository row, resolved by its stable Gitea ID.

        Rows created by bulk code mappings carry a path-valued external_id and no config; those
        are resolved (and owner-checked) by path. Results are cached per installation instance.
        """
        if (
            repo.integration_id != self.installation.model.id
            or repo.organization_id != self.installation.organization_id
        ):
            raise IntegrationError("The repository belongs to another Gitea installation.")
        cache = self.installation.__dict__.setdefault("_gitea_repo_paths", {})
        if repo.id is not None and repo.id in cache:
            return cache[repo.id]
        stable = str((repo.config or {}).get("repo_id") or repo.external_id or "")
        if stable.isdigit():
            data = self.get_scm_repo(repo_id=int(stable))
        else:
            data = self.get_scm_repo(path=(repo.config or {}).get("path") or repo.name)
        if repo.id is not None:
            cache[repo.id] = data["full_name"]
        return data["full_name"]

    def check_file(self, repo: Any, path: str, version: str | None) -> Any:
        try:
            repository_file_path(path)
            if version:
                git_ref(version)
        except ValueError:
            # Not a valid repository path or ref: it cannot exist, so there is nothing to link.
            return None
        try:
            path_in_repo = self.repository_path(repo)
        except IntegrationError:
            # Moved out of the installed owner namespace: never read it, and link nothing.
            return None
        return self._call("check_file", path, version or None, repo=path_in_repo)

    def get_file(self, repo: Any, path: str, ref: str | None, codeowners: bool = False) -> str:
        try:
            repository_file_path(path)
            if ref:
                git_ref(ref)
        except ValueError:
            raise ApiInvalidRequestError(
                json.dumps({"message": "Invalid Gitea file path or reference."}), code=400
            ) from None
        return self._call("get_file", path, ref or None, repo=self.repository_path(repo))

    def get_ref(self, repo: Any, kind: str, name: str) -> dict[str, Any] | None:
        """Branch or tag metadata; None when Gitea has no such ref."""
        method = {"branch": "get_branch", "tag": "get_tag"}[kind]
        try:
            git_ref(name)
            return self._call(
                method, name, repo=self.repository_path(repo), expected=frozenset({404})
            )
        except ValueError:
            return None
        except ApiError as error:
            if error.code == 404:
                return None
            raise

    def list_hooks(self, path: str) -> list[dict[str, Any]]:
        return self._call("list_hooks", repo=path)

    def create_hook(self, path: str, url: str, secret: str) -> dict[str, Any]:
        return self._call("create_hook", url, secret, repo=path, write=True)

    def update_hook(self, path: str, hook_id: int, url: str, secret: str) -> dict[str, Any]:
        return self._call("update_hook", hook_id, url, secret, repo=path, write=True)

    def delete_hook(self, path: str, hook_id: int) -> bool:
        return self._call("delete_hook", hook_id, repo=path, write=True)

    def commit_range(
        self,
        path: str,
        head: str,
        *,
        exclude: str | None,
        max_commits: int,
        on_page: Any = None,
        stop_when_over: bool = False,
    ) -> CommitRange:
        return self._call(
            "commit_range",
            head,
            exclude=exclude,
            max_commits=max_commits,
            repo=path,
            on_page=on_page,
            stop_when_over=stop_when_over,
        )
