"""Issue forms and operations for Sentry's organization integration endpoints."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlencode

from django.core import signing
from django.urls import reverse
from sentry import options
from sentry.integrations.models.external_issue import ExternalIssue
from sentry.integrations.source_code_management.issues import SourceCodeIssueIntegration
from sentry.locks import locks
from sentry.models.group import Group
from sentry.shared_integrations.exceptions import ApiError, IntegrationError, IntegrationFormError
from sentry.utils.http import absolute_uri
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea.api.client import browser_issue_url, is_pull_request, issue_number
from sentry_gitea.render import (
    EventData,
    ensure_metadata,
    event_is_handled,
    parse_label_names,
    render_body,
    render_title,
    strip_metadata,
)

from .identity import OAuthSettings, log_failure
from .utils import issue_key, parse_issue_key

logger = logging.getLogger(__name__)
LINK_MARKER = re.compile(r"<!-- sentry-gitea-link ([1-9][0-9]*) -->")
LINK_SALT = "sentry-gitea.link-context.v1"
CREATE_SALT = "sentry-gitea.create-context.v1"
CONTEXT_MAX_AGE = 86400
# A saved default repository answering with these statuses moved, vanished or became private.
STALE_STATUSES = frozenset({301, 302, 307, 308, 403, 404})


class StaleRepository(IntegrationError):
    """A saved form default no longer resolves; let the user choose a current repository."""


class GiteaIssues(SourceCodeIssueIntegration):
    @contextmanager
    def _issue_operation(self) -> Iterator[None]:
        try:
            yield
        except (ApiError, IntegrationError, ValueError, UnableToAcquireLock) as error:
            log_failure(
                logger,
                "sentry-gitea: issue operation failed",
                error,
                integration_id=self.model.id,
                organization_id=self.organization_id,
            )
            self.raise_error(error)

    def get_persisted_default_config_fields(self) -> list[str]:
        return ["repo"]

    def _search_url(self) -> str:
        return reverse("sentry-gitea-search", args=[self.organization_id, self.model.id])

    def get_repository_choices(
        self, group: Any, params: Mapping[str, Any], page_number_limit: int | None = None
    ) -> tuple[str, list[tuple[str, str]]]:
        # Issue forms need a built-in tracker; SCM/code mappings also support code-only repos.
        available = self.get_client().search_repositories(page_number_limit=page_number_limit)
        choices = {item["full_name"]: item["full_name"] for item in available}
        requested = params.get("repo")
        if not requested and group is not None:
            requested = self.get_project_defaults(group.project_id).get("repo")
        if requested and requested not in choices:
            path, label = self.create_default_repo_choice(requested)
            choices = {path: label, **choices}
        selected = requested or next(iter(choices), "")
        return selected, list(choices.items())

    def _repository_field(self, group: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        with self._issue_operation():
            try:
                default, choices = self.get_repository_choices(group, params, page_number_limit=2)
            except StaleRepository:
                if params.get("repo"):
                    raise
                # Ignore only an obsolete persisted default, not an explicit user selection, and
                # select nothing: the user picks a repository instead of silently getting another.
                _, choices = self.get_repository_choices(None, {}, page_number_limit=2)
                default = ""
        return {
            "name": "repo",
            "label": "Repository",
            "type": "select",
            "required": True,
            "default": default,
            "choices": choices,
            "url": self._search_url(),
            "updatesForm": True,
        }

    def create_default_repo_choice(self, default_repo: str) -> tuple[str, str]:
        stale = False
        with self._issue_operation():
            try:
                repo = self.get_client().get_repo(repo=default_repo)
            except IntegrationFormError:
                # Wrong owner, issue tracker disabled or external, or not owner/name anymore.
                stale = True
            except ApiError as error:
                if error.code not in STALE_STATUSES:
                    raise
                stale = True
        if stale:
            raise StaleRepository("The saved repository is no longer available. Select one.")
        return repo["full_name"], repo["full_name"]

    def _event_data(self, group: Any, event: Any) -> EventData:
        data = event.data if event is not None else {}
        tags = dict(data.get("tags") or [])
        release = data.get("release") or tags.get("sentry:release") or ""
        if isinstance(release, Mapping):
            release = release.get("version") or ""
        event_id = str(getattr(event, "event_id", "") or "")
        return EventData(
            short_id=group.qualified_short_id,
            title=str(getattr(event, "title", "") or group.title),
            sentry_url=options.get("system.url-prefix") or absolute_uri("/"),
            project_id=group.project_id,
            group_id=group.id,
            issue_url=absolute_uri(group.get_absolute_url()),
            event_id=event_id,
            event_url=absolute_uri(group.get_absolute_url(event_id=event_id)) if event_id else "",
            level=str(data.get("level") or "error"),
            release=str(release),
            first_release=str(group.get_first_release() or "") if not release else "",
            environment=str(data.get("environment") or tags.get("environment") or ""),
            culprit=str(data.get("culprit") or group.culprit or ""),
            handled=event_is_handled(data),
            fingerprint=hashlib.sha256(f"{group.project_id}:{group.id}".encode()).hexdigest(),
        )

    def get_group_title(self, group: Any, event: Any, **kwargs: Any) -> str:
        return render_title(self._event_data(group, event)) if group is not None else "Sentry issue"

    def get_group_description(self, group: Any, event: Any, **kwargs: Any) -> str:
        return render_body(self._event_data(group, event)) if group is not None else ""

    def _signed_group_context(self, group: Any, salt: str) -> str:
        return signing.dumps(
            {
                "group_id": group.id,
                "organization_id": self.organization_id,
                "integration_id": self.model.id,
            },
            salt=salt,
        )

    def _signed_group_id(self, token: Any, salt: str) -> str:
        if not isinstance(token, str):
            raise ValueError("Group context must be text.")
        context = signing.loads(token, salt=salt, max_age=CONTEXT_MAX_AGE)
        if (
            context["organization_id"] != self.organization_id
            or context["integration_id"] != self.model.id
        ):
            raise ValueError("Wrong group context.")
        return issue_number(context["group_id"])

    def _create_context_group(self, data: Mapping[str, Any]) -> Any:
        token = data.get("createContext")
        if not token:
            return None
        try:
            return Group.objects.get(
                id=self._signed_group_id(token, CREATE_SALT),
                project__organization_id=self.organization_id,
            )
        except (signing.BadSignature, Group.DoesNotExist, ValueError, KeyError, TypeError):
            raise IntegrationFormError(
                {"description": ["Reload the form before creating the issue."]}
            ) from None

    def get_create_issue_config(self, group: Any, user: Any, **kwargs: Any) -> list[dict[str, Any]]:
        repo = self._repository_field(group, kwargs.get("params") or {})
        event = group.get_latest_event() if group is not None else None
        fields = [
            repo,
            {
                "name": "title",
                "label": "Title",
                "type": "string",
                "required": True,
                "default": self.get_group_title(group, event),
            },
            {
                "name": "description",
                "label": "Description",
                "type": "textarea",
                "autosize": True,
                "default": self.get_group_description(group, event),
            },
            {
                "name": "labels",
                "label": "Labels",
                "type": "string",
                "required": False,
                "default": "",
                "help": "Comma-separated names. Missing labels will be created.",
            },
            {
                "name": "assignee",
                "label": "Assignee",
                "type": "string",
                "required": False,
                "default": "",
                "help": "Gitea username.",
            },
        ]
        if group is not None:
            # Core does not pass group to create_issue, and the dedup marker stays out of the
            # editable description, so the form carries the group the marker is built for.
            context = self._signed_group_context(group, CREATE_SALT)
            fields.append({"name": "createContext", "type": "hidden", "default": context})
        return fields

    def get_link_issue_config(self, group: Any, **kwargs: Any) -> list[dict[str, Any]]:
        repo = self._repository_field(group, kwargs.get("params") or {})
        comment = ""
        if group is not None:
            comment = (
                f"Sentry issue: {absolute_uri(group.get_absolute_url())}\n\n"
                f"<!-- sentry-gitea-link {group.id} -->"
            )
        fields = [
            repo,
            {
                "name": "externalIssue",
                "label": "Issue",
                "type": "select",
                "required": True,
                "default": "",
                "choices": [],
                "url": self._search_url() + "?" + urlencode({"repo": repo["default"]}),
            },
            {
                "name": "comment",
                "label": "Comment",
                "type": "textarea",
                "required": False,
                "default": comment,
            },
        ]
        if group is not None:
            # Core does not pass group to after_link_issue. Keep context when the comment is edited.
            context = self._signed_group_context(group, LINK_SALT)
            fields.append({"name": "linkContext", "type": "hidden", "default": context})
        return fields

    def _normalized_issue(self, repo: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any]:
        if is_pull_request(issue):
            raise IntegrationFormError(
                {"externalIssue": ["Select an issue; pull requests cannot be linked."]}
            )
        number = issue_number(issue.get("number"))
        issue_repo = issue.get("repository")
        if issue_repo is not None and (
            not isinstance(issue_repo, dict) or str(issue_repo.get("id")) != str(repo["id"])
        ):
            raise IntegrationError("Gitea returned an issue from a different repository.")
        config = OAuthSettings.load()
        url = browser_issue_url(
            config.base_url, repo["full_name"], number, public_url=config.public_url
        )
        key = issue_key(repo["id"], number)
        metadata = {
            "display_name": f"{repo['full_name']}#{number}",
            "url": url,
            "repo_id": str(repo["id"]),
            "repo": repo["full_name"],
        }
        self.__dict__.setdefault("_issue_urls", {})[key] = url
        return {
            "key": key,
            "title": str(issue.get("title") or ""),
            "description": str(issue.get("body") or ""),
            "url": url,
            "metadata": metadata,
        }

    def create_issue(self, data: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        with self._issue_operation():
            title = data.get("title")
            if not isinstance(title, str) or not title.strip():
                raise IntegrationFormError({"title": ["Enter an issue title."]})
            description = data.get("description") or ""
            assignee = data.get("assignee") or ""
            if not isinstance(description, str):
                raise IntegrationFormError({"description": ["Description must be text."]})
            if not isinstance(assignee, str) or (
                assignee and not re.fullmatch(r"[\w.-]+", assignee)
            ):
                raise IntegrationFormError({"assignee": ["Enter a Gitea username."]})
            try:
                labels = parse_label_names(data.get("labels"))
            except ValueError:
                raise IntegrationFormError(
                    {"labels": ["Enter comma-separated label names."]}
                ) from None
            group = self._create_context_group(data)
            client = self.get_client()
            repo = client.get_repo(repo=data.get("repo"))
            if group is None:
                # No form context (a direct API call): a plain, non-idempotent create. Markers
                # typed into the description are dropped so they cannot alias another group.
                body = strip_metadata(description).rstrip()
                return self._create_issue(client, repo, title, body, labels, assignee)
            event = self._event_data(group, group.get_latest_event())
            body = ensure_metadata(description, event)
            dedup_id = event.dedup_id
            digest = hashlib.sha256(dedup_id.encode()).hexdigest()[:32]
            lock = locks.get(
                f"sentry-gitea:create:{self.model.id}:{repo['id']}:{digest}",
                duration=120,
                name="gitea_create_issue",
            )
            with lock.blocking_acquire(initial_delay=0.1, timeout=5):
                # A retry after an ambiguous or partially applied create links the issue the
                # installation already filed for this Sentry issue, instead of a duplicate.
                existing = client.find_marked_issue(dedup_id, repo=repo["full_name"])
                if existing is not None:
                    return self._normalized_issue(repo, existing)
                return self._create_issue(client, repo, title, body, labels, assignee)

    def _create_issue(
        self,
        client: Any,
        repo: dict[str, Any],
        title: str,
        description: str,
        labels: list[str],
        assignee: str,
    ) -> dict[str, Any]:
        label_ids = client.resolve_label_ids(labels, repo=repo["full_name"]) if labels else []
        issue = client.create_issue(
            title.strip(),
            description,
            repo=repo["full_name"],
            label_ids=label_ids,
            assignees=[assignee] if assignee else [],
        )
        return self._normalized_issue(repo, issue)

    def get_issue(self, issue_id: str, **kwargs: Any) -> dict[str, Any]:
        with self._issue_operation():
            try:
                repo_id, number = parse_issue_key(issue_id)
            except ValueError:
                raise IntegrationFormError(
                    {"externalIssue": ["Select an issue from Gitea search."]}
                ) from None
            client = self.get_client()
            selected = (kwargs.get("data") or {}).get("repo")
            repo = client.get_repo(repo=selected) if selected else client.get_repo_by_id(repo_id)
            if str(repo["id"]) != str(repo_id):
                raise IntegrationFormError(
                    {"externalIssue": ["Issue and repository do not match."]}
                )
            issue = client.get_issue(number, repo=repo["full_name"])
            if str(issue.get("number")) != str(number):
                raise IntegrationError("Gitea returned a different issue.")
            return self._normalized_issue(repo, issue)

    def search_issues(self, query: str | None, **kwargs: Any) -> list[dict[str, Any]]:
        with self._issue_operation():
            client = self.get_client()
            repo = client.get_repo(repo=kwargs.get("repo"))
            return [
                self._normalized_issue(repo, issue)
                for issue in client.search_issues(query, repo=repo["full_name"])
            ]

    def get_issue_url(self, key: str) -> str:
        # Activity and sidebar rendering must not make unbounded upstream requests.
        if cached := self.__dict__.get("_issue_urls", {}).get(key):
            return cached
        metadata = (
            ExternalIssue.objects.filter(
                organization_id=self.organization_id, integration_id=self.model.id, key=key
            )
            .values_list("metadata", flat=True)
            .first()
            or {}
        )
        return metadata.get("url", "")

    def get_issue_display_name(self, external_issue: Any) -> str:
        return (external_issue.metadata or {}).get("display_name") or external_issue.key

    def _backlink(self, data: Mapping[str, Any]) -> tuple[str, str]:
        comment = data.get("comment") or ""
        if not isinstance(comment, str):
            raise IntegrationFormError({"comment": ["Comment must be text."]})
        if not comment.strip():
            return "", ""
        if data.get("linkContext"):
            try:
                group_id = self._signed_group_id(data["linkContext"], LINK_SALT)
            except (signing.BadSignature, ValueError, KeyError, TypeError):
                raise IntegrationFormError(
                    {"comment": ["Reload the link form before adding a comment."]}
                ) from None
        else:
            match = LINK_MARKER.search(comment)
            if match is None:
                raise IntegrationFormError(
                    {"comment": ["Reload the link form before adding a comment."]}
                )
            group_id = match[1]
        marker = f"<!-- sentry-gitea-link {group_id} -->"
        return LINK_MARKER.sub("", comment).rstrip() + "\n\n" + marker, marker

    def after_link_issue(self, external_issue: Any, **kwargs: Any) -> None:
        with self._issue_operation():
            body, marker = self._backlink(kwargs.get("data") or {})
            if not body:
                return
            repo_id, number = parse_issue_key(external_issue.key)
            lock_key = f"sentry-gitea:backlink:{self.model.id}:{external_issue.key}:{marker}"
            lock = locks.get(lock_key, duration=120, name="gitea_backlink")
            with lock.blocking_acquire(initial_delay=0.1, timeout=5):
                client = self.get_client()
                repo = client.get_repo_by_id(repo_id)["full_name"]
                # Anyone can quote the marker; only the installation's own comment counts.
                if any(
                    marker in str(comment.get("body") or "") and client.authored(comment)
                    for comment in client.list_comments(number, repo=repo)
                ):
                    return
                client.create_comment(number, body, repo=repo)
