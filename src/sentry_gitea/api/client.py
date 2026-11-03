"""Small bounded REST transport. No Sentry dependencies or automatic write retries."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from datetime import datetime
from typing import Any, Self
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests
from urllib3.exceptions import HTTPError as TransportError

from sentry_gitea.render import parse_label_names, parse_metadata

from .errors import GiteaApiError, redact

TIMEOUT = (5, 15)
MAX_PAGES = 20
PAGE_SIZE = 50
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_ALLOWANCE = sum(TIMEOUT)
OPERATION_BUDGET = 43
# Per client (one operation): total response bytes, and rows across paginated list responses.
# Each response is also bounded by MAX_RESPONSE_BYTES and each walk by MAX_PAGES.
OPERATION_MAX_BYTES = 32 * 1024 * 1024
OPERATION_MAX_ROWS = 5000
DEFAULT_PORTS = {"http": 80, "https": 443}
DATA_BUDGET_EXHAUSTED = "Gitea responses exceed this operation's data budget."


SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
COMMIT_LIMIT = 50
HOOK_EVENTS = ("push", "issues_only", "issue_assign", "pull_request")


def is_sha(value: Any) -> bool:
    """Full SHA-1 or SHA-256 object names, as Gitea serializes them (lowercase hex)."""
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def repository_file_path(value: str) -> str:
    """Encode a repository-relative file path; reject traversal and ambiguous separators."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or value.startswith("/")
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Invalid repository file path.")
    return "/".join(quote(part, safe="") for part in value.split("/"))


def git_ref(value: str) -> str:
    """A branch, tag or commit name suitable for a query parameter or a ref path."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value.startswith(("-", "/"))
        or value.endswith("/")
        or ".." in value
        or "//" in value
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in "~^:?*[\\" for c in value)
    ):
        raise ValueError("Invalid Git reference.")
    return value


def normalize_base_url(value: str) -> str:
    message = "Gitea base URL must be http(s), without credentials, query or fragment."
    if not isinstance(value, str) or any(ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError(message)
    try:
        url = urlsplit(value.strip())
        port = url.port
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError(message)
        path = unquote(url.path)
        if any(p in {".", ".."} for p in path.split("/")) or "\\" in path:
            raise ValueError(message)
        if any(c.isspace() for c in url.netloc) or "%" in url.netloc:
            raise ValueError(message)
        host = url.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{port}" if port is not None else "")
        return urlunsplit((url.scheme, netloc, url.path.rstrip("/"), "", ""))
    except (ValueError, AttributeError):
        raise ValueError(message) from None


def url_origin(value: str) -> str:
    """`scheme://host[:port]` of an HTTP(S) URL, omitting the scheme's default port."""
    url = urlsplit(normalize_base_url(value))
    host = url.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = url.port
    if port is None or port == DEFAULT_PORTS[url.scheme]:
        return f"{url.scheme}://{host}"
    return f"{url.scheme}://{host}:{port}"


def normalize_repo(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Repository must be owner/name.")
    value = value.strip()
    parts = value.split("/")
    if len(parts) != 2 or any(p in {".", ".."} or not re.fullmatch(r"[\w.-]+", p) for p in parts):
        raise ValueError("Repository must be owner/name.")
    return value


ISSUE_STATES = frozenset({"all", "open", "closed"})


def parse_gitea_time(value: Any) -> datetime | None:
    """A timezone-aware datetime from a Gitea RFC 3339 timestamp; anything else is None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1:] in {"Z", "z"}:
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def issue_number(value: str | int) -> str:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("Issue number must be a positive integer in the configured repository.")
    return str(value)


def browser_issue_url(
    base_url: str, repo: str, number: str | int, *, public_url: str | None = None
) -> str:
    """Build browser links from configuration, independent of Gitea's response URLs."""
    base = normalize_base_url(base_url if public_url in (None, "") else public_url)
    path = "/".join(quote(part, safe="") for part in normalize_repo(repo).split("/"))
    return f"{base}/{path}/issues/{issue_number(number)}"


def owner_domain_name(public_url: str, owner_login: str) -> str:
    """The owner's Gitea location as Sentry shows an integration's `domain_name`, such as
    `gitea.example/team`, the way GitHub's is `github.com/<owner>`."""
    base = normalize_base_url(public_url)
    owner = normalize_repo(f"{owner_login}/namespace").split("/")[0]
    # Sentry's settings page links a domain name without a scheme as https://.
    return f"{base.removeprefix('https://')}/{owner}"


def label_color(name: str) -> str:
    if name.startswith("source:"):
        return "#5319e7"
    return {
        "severity:critical": "#b60205",
        "severity:high": "#d93f0b",
        "severity:medium": "#fbca04",
        "severity:low": "#0e8a16",
        "class:crash": "#b60205",
        "class:other": "#c5def5",
    }.get(name, "#ededed")


def is_pull_request(issue: dict[str, Any]) -> bool:
    """Gitea always sends `pull_request`; it is null for plain issues (structs/issue.go)."""
    return issue.get("pull_request") is not None


class _Transport:
    def __init__(
        self,
        base_url: str,
        *,
        session: requests.Session | None = None,
        deadline: float | None = None,
        owns_session: bool | None = None,
        max_bytes: int = OPERATION_MAX_BYTES,
        max_rows: int = OPERATION_MAX_ROWS,
    ):
        self.base_url = normalize_base_url(base_url)
        self._token = ""
        self._prefix = "/api/v1"
        self._sensitive_response = False
        self.session = session or requests.Session()
        # A caller-supplied session stays open unless the caller hands its ownership over.
        self._owns_session = session is None if owns_session is None else owns_session
        self.max_bytes = max_bytes
        self.max_rows = max_rows
        self._bytes_used = 0
        self._rows_used = 0
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
        )
        self.deadline = deadline if deadline is not None else time.monotonic() + OPERATION_BUDGET

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        if self._owns_session:
            self.session.close()

    def _charge_bytes(self, count: int) -> None:
        self._bytes_used += count
        if self._bytes_used > self.max_bytes:
            raise GiteaApiError(0, DATA_BUDGET_EXHAUSTED)

    def _charge_rows(self, count: int) -> None:
        self._rows_used += count
        if self._rows_used > self.max_rows:
            raise GiteaApiError(
                0, "Gitea returned more rows than one operation allows; narrow the request."
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        read_deadline: float | None = None,
        raw: bool = False,
        **kwargs: Any,
    ) -> tuple[Any, dict[str, str]]:
        if time.monotonic() + REQUEST_ALLOWANCE > self.deadline:
            raise GiteaApiError(0, "Gitea operation time budget exhausted; retry later.")
        # All paths are constructed here. Server-provided Link/Location URLs are never followed.
        url = self.base_url + self._prefix + path
        cutoff = min(self.deadline, read_deadline) if read_deadline is not None else self.deadline
        try:
            with self.session.request(
                method, url, timeout=TIMEOUT, stream=True, allow_redirects=False, **kwargs
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise GiteaApiError(
                        status, "Gitea redirect refused; configure the final base URL."
                    )
                if self._sensitive_response and not 200 <= status < 300:
                    # OAuth error bodies can echo codes, verifiers or previously unknown tokens.
                    raise GiteaApiError(status, "Gitea rejected the OAuth grant; authorize again.")
                if status == 204:
                    return None, dict(response.headers)
                announced = response.headers.get("Content-Length", "")
                if announced.isdigit() and int(announced) > MAX_RESPONSE_BYTES:
                    raise GiteaApiError(status, "Gitea response exceeds the size limit.")
                if announced.isdigit() and self._bytes_used + int(announced) > self.max_bytes:
                    raise GiteaApiError(0, DATA_BUDGET_EXHAUSTED)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise GiteaApiError(status, "Gitea returned an unexpected content encoding.")
                chunks = bytearray()
                while True:
                    if time.monotonic() > cutoff:
                        raise GiteaApiError(
                            0, "Gitea operation time budget exhausted; retry later."
                        )
                    # read1 returns available bytes rather than filling a chunk across reads.
                    # Reject compression so decompression cannot hide more blocking reads.
                    read1 = getattr(response.raw, "read1", None)
                    chunk = (
                        read1(8192, decode_content=False)
                        if read1
                        else response.raw.read(1, decode_content=False)
                    )
                    if time.monotonic() > cutoff:
                        raise GiteaApiError(
                            0, "Gitea operation time budget exhausted; retry later."
                        )
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise GiteaApiError(status, "Gitea response exceeds the size limit.")
                    self._charge_bytes(len(chunk))
                if not 200 <= status < 300:
                    message = redact(chunks.decode("utf-8", errors="replace"), self._token)
                    if message:
                        raise GiteaApiError(status, message, upstream=True)
                    raise GiteaApiError(status, "Gitea rejected the request.")
                try:
                    data = chunks.decode("utf-8") if raw else json.loads(chunks)
                except (ValueError, UnicodeError, RecursionError):
                    kind = "text" if raw else "JSON"
                    raise GiteaApiError(status, f"Gitea returned invalid {kind}.") from None
                return data, dict(response.headers)
        except (requests.RequestException, TransportError, OSError):
            # requests exceptions can contain headers, URLs and proxy credentials.
            raise GiteaApiError(0, "Cannot reach Gitea, or the request timed out.") from None

    def _object(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        data, _ = self._request(method, path, **kwargs)
        if not isinstance(data, dict):
            raise GiteaApiError(200, "Gitea returned an invalid object.")
        return data

    @staticmethod
    def _rows(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise GiteaApiError(200, "Gitea returned an invalid list.")
        return data


class GiteaOAuthClient(_Transport):
    """Confidential OAuth grants, sharing the bounded transport but never API auth headers."""

    def __init__(
        self, base_url: str, client_id: str, client_secret: str, *, deadline: float | None = None
    ):
        super().__init__(base_url, deadline=deadline)
        self._prefix = ""
        self._sensitive_response = True
        self._credentials = {"client_id": client_id, "client_secret": client_secret}

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        return self._object(
            "POST",
            "/login/oauth/access_token",
            data={
                **self._credentials,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return self._object(
            "POST",
            "/login/oauth/access_token",
            data={
                **self._credentials,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )


class GiteaClient(_Transport):
    """PAT callers may bind a repo; integration callers supply a repo explicitly per operation."""

    def __init__(
        self,
        base_url: str,
        token: str,
        repo: str | None = None,
        *,
        session: requests.Session | None = None,
        deadline: float | None = None,
        on_recovered_error: Callable[[int], None] | None = None,
        token_type: str = "token",
        owns_session: bool | None = None,
        max_bytes: int = OPERATION_MAX_BYTES,
        max_rows: int = OPERATION_MAX_ROWS,
    ):
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise ValueError("A Gitea access token is required.")
        if token_type not in {"token", "Bearer"}:
            raise ValueError("Invalid Gitea token type.")
        self.repo = normalize_repo(repo) if repo is not None else None
        super().__init__(
            base_url,
            session=session,
            deadline=deadline,
            owns_session=owns_session,
            max_bytes=max_bytes,
            max_rows=max_rows,
        )
        self._token = token
        self.session.headers["Authorization"] = f"{token_type} {token}"
        self.on_recovered_error = on_recovered_error
        self._user_id: int | None = None

    def _repo_path(self, repo: str | None = None) -> str:
        value = normalize_repo(repo if repo is not None else self.repo)
        return "/repos/" + "/".join(quote(p, safe="") for p in value.split("/"))

    @property
    def repo_path(self) -> str:
        return self._repo_path()

    def _pages(
        self,
        path: str,
        *,
        page_number_limit: int | None = None,
        results_key: str | None = None,
        **params: Any,
    ) -> Iterator[list[dict[str, Any]]]:
        count = 0
        limit = (
            MAX_PAGES if page_number_limit is None else max(1, min(MAX_PAGES, page_number_limit))
        )
        for page in range(1, limit + 1):
            data, headers = self._request(
                "GET", path, params={**params, "page": page, "limit": PAGE_SIZE}
            )
            if results_key is not None:
                data = data.get(results_key) if isinstance(data, dict) else None
            rows = self._rows(data)
            self._charge_rows(len(rows))
            count += len(rows)
            total_value = next((v for k, v in headers.items() if k.lower() == "x-total-count"), "")
            total = int(total_value) if total_value.isdigit() else None
            if not rows:
                if total is not None and count < total:
                    raise GiteaApiError(200, "Gitea returned incomplete pagination results.")
                return
            yield rows
            if total is not None and count >= total:
                return
        if page_number_limit is None:
            raise GiteaApiError(
                200, "Gitea pagination limit reached; narrow the repository or search."
            )

    def get_current_user(self) -> dict[str, Any]:
        return self._object("GET", "/user")

    def get_user(self) -> dict[str, Any]:
        return self.get_current_user()

    def current_user_id(self) -> int:
        """The token owner's numeric Gitea ID, read once per client (one operation)."""
        if self._user_id is None:
            value = self.get_current_user().get("id")
            if type(value) is not int or value <= 0:
                raise GiteaApiError(200, "Gitea returned an invalid user ID.")
            self._user_id = value
        return self._user_id

    def list_user_orgs(self) -> list[dict[str, Any]]:
        return [row for page in self._pages("/user/orgs") for row in page]

    def get_org(self, org: str) -> dict[str, Any]:
        return self._object("GET", f"/orgs/{quote(org, safe='')}")

    def is_org_owner(self, org: str, user: str) -> bool:
        org, user = quote(org, safe=""), quote(user, safe="")
        try:
            self._request("GET", f"/orgs/{org}/members/{user}")
            permissions = self._object("GET", f"/users/{user}/orgs/{org}/permissions")
        except GiteaApiError as error:
            if error.status in {303, 403, 404}:
                return False
            raise
        return permissions.get("is_owner") is True

    def search_repositories(
        self,
        owner_id: int,
        query: str | None = None,
        page_number_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return [
            row
            for page in self._pages(
                "/repos/search",
                results_key="data",
                page_number_limit=page_number_limit,
                uid=issue_number(owner_id),
                exclusive="true",
                q=query or "",
                sort="alpha",
            )
            for row in page
        ]

    def get_repo(self, *, repo: str | None = None) -> dict[str, Any]:
        return self._object("GET", self._repo_path(repo))

    def get_repo_by_id(self, repo_id: int | str) -> dict[str, Any]:
        return self._object("GET", f"/repositories/{issue_number(repo_id)}")

    def get_issue(self, number: int | str, *, repo: str | None = None) -> dict[str, Any]:
        issue = self._object("GET", f"{self._repo_path(repo)}/issues/{issue_number(number)}")
        if is_pull_request(issue):
            raise GiteaApiError(200, "The selected item is a pull request, not an issue.")
        return issue

    def create_issue(
        self,
        title: str,
        body: str,
        *,
        label_ids: Sequence[int] = (),
        assignees: Sequence[str] = (),
        repo: str | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValueError("Issue title cannot be empty.")
        if any(type(label) is not int or label <= 0 for label in label_ids):
            raise ValueError("Labels must be positive numeric IDs.")
        if any(not isinstance(user, str) or not user.strip() for user in assignees):
            raise ValueError("Assignees must be usernames.")
        result = self._object(
            "POST",
            self._repo_path(repo) + "/issues",
            json={
                "title": title[:255],
                "body": body,
                "labels": list(label_ids),
                "assignees": list(assignees),
            },
        )
        applied_labels = {
            row.get("id") for row in result.get("labels") or [] if isinstance(row, dict)
        }
        applied_users = {
            row.get("login") for row in result.get("assignees") or [] if isinstance(row, dict)
        }
        if not set(label_ids) <= applied_labels or not set(assignees) <= applied_users:
            raise GiteaApiError(
                201,
                "Issue created but labels/assignees were discarded; check repository "
                "write permissions. Retry recovers the issue using its metadata marker.",
            )
        return result

    def create_comment(
        self,
        number: int | str,
        body: str,
        *,
        repo: str | None = None,
    ) -> dict[str, Any]:
        return self._object(
            "POST",
            f"{self._repo_path(repo)}/issues/{issue_number(number)}/comments",
            json={"body": body},
        )

    def list_comments(self, number: int | str, *, repo: str | None = None) -> list[dict[str, Any]]:
        path = f"{self._repo_path(repo)}/issues/{issue_number(number)}/comments"
        return [row for page in self._pages(path) for row in page]

    def list_labels(self, *, repo: str | None = None) -> list[dict[str, Any]]:
        return [row for page in self._pages(self._repo_path(repo) + "/labels") for row in page]

    def create_label(self, name: str, *, repo: str | None = None) -> dict[str, Any]:
        names = parse_label_names([name])
        if len(names) != 1:
            raise ValueError("A label name is required.")
        return self._object(
            "POST",
            self._repo_path(repo) + "/labels",
            json={"name": names[0], "color": label_color(names[0])},
        )

    def label_ids_by_name(self, names: Sequence[str], *, repo: str | None = None) -> dict[str, int]:
        """IDs of the wanted labels; label pages are read only until every name is found.

        Only matching labels are kept. Within the pages read, duplicate names resolve to the
        lowest ID. A name that is still missing after the last page is simply absent.
        """
        wanted = set(parse_label_names(names))
        found: dict[str, int] = {}
        if not wanted:
            return found
        for page in self._pages(self._repo_path(repo) + "/labels"):
            for label in page:
                name, number = label.get("name"), label.get("id")
                if isinstance(name, str) and name in wanted and type(number) is int and number > 0:
                    found[name] = min(number, found.get(name, number))
            if len(found) == len(wanted):
                break
        return found

    def resolve_label_ids(
        self,
        names: Sequence[str],
        create_missing: bool = True,
        *,
        repo: str | None = None,
    ) -> list[int]:
        names = parse_label_names(names)
        if not names:
            return []
        known = self.label_ids_by_name(names, repo=repo)
        for name in names:
            if name in known:
                continue
            if not create_missing:
                raise GiteaApiError(404, "Missing repository label; label creation is disabled.")
            try:
                label = self.create_label(name, repo=repo)
            except GiteaApiError as error:
                if error.status not in {409, 422}:
                    raise
                if self.on_recovered_error is not None:
                    self.on_recovered_error(error.status)
                known.update(self.label_ids_by_name([name], repo=repo))
                if name not in known:
                    raise error from None
            else:
                number = label.get("id")
                if type(number) is not int or number <= 0:
                    raise GiteaApiError(201, "Gitea returned an invalid label ID.")
                known[name] = number
        return [known[name] for name in names]

    def list_assignees(self, *, repo: str | None = None) -> list[dict[str, Any]]:
        # This Gitea endpoint ignores page/limit and returns the entire bounded response.
        data, _ = self._request("GET", self._repo_path(repo) + "/assignees")
        rows = self._rows(data)
        self._charge_rows(len(rows))
        return rows

    def search_issues(
        self,
        query: str | None,
        *,
        repo: str | None = None,
        page_number_limit: int = 2,
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if re.fullmatch(r"#?[1-9][0-9]*", query):
            try:
                # Fetch raw here so a PR is an empty search, rather than a link validation error.
                result = self._object("GET", f"{self._repo_path(repo)}/issues/{query.lstrip('#')}")
            except GiteaApiError as error:
                if error.status == 404:
                    return []
                raise
            return [] if is_pull_request(result) else [result]
        return [
            row
            for page in self._pages(
                self._repo_path(repo) + "/issues",
                q=query,
                type="issues",
                state="all",
                page_number_limit=page_number_limit,
            )
            for row in page
            if not is_pull_request(row)
        ]

    def search_issues_by_marker(
        self,
        dedup_id: str,
        *,
        author_id: int | None = None,
        repo: str | None = None,
        state: str = "all",
        exclude: Collection[str | int] = (),
    ) -> dict[str, Any] | None:
        """The issue whose metadata marker names `dedup_id`: the first open match, else the
        first closed one.

        Anyone who can open an issue can paste a marker. With `author_id`, only issues opened by
        that Gitea user count, so a planted marker cannot capture a Sentry group's link. A group
        that was re-filed after a close has several marked issues; `state` limits the search and
        numbers in `exclude` never match.
        """
        if author_id is not None and (type(author_id) is not int or author_id <= 0):
            raise ValueError("The marker author must be a positive Gitea user ID.")
        if state not in ISSUE_STATES:
            raise ValueError("Issue state must be all, open or closed.")
        excluded = {str(number) for number in exclude}
        path = self._repo_path(repo) + "/issues"
        closed_match = None
        for page in self._pages(path, state=state, type="issues", q=dedup_id):
            for issue in page:
                if is_pull_request(issue) or str(issue.get("number")) in excluded:
                    continue
                if state != "all" and issue.get("state") != state:
                    continue
                if author_id is not None:
                    user = issue.get("user")
                    user_id = user.get("id") if isinstance(user, dict) else None
                    if type(user_id) is not int or user_id != author_id:
                        continue
                metadata = parse_metadata(issue.get("body") or "", dedup_id=dedup_id)
                if metadata and metadata.get("dedup_id") == dedup_id:
                    if issue.get("state") != "closed":
                        return issue
                    closed_match = closed_match or issue
        return closed_match

    def compare_commits(
        self, base: str, head: str, *, read_deadline: float | None = None
    ) -> dict[str, Any]:
        """Direct-range compare for optional advisory content; response bytes remain bounded."""
        return self._object(
            "GET",
            f"{self.repo_path}/compare/{quote(base, safe='')}..{quote(head, safe='')}",
            read_deadline=read_deadline,
        )

    # Synchronization, repository hooks, source files and commit ranges.

    def update_issue(
        self,
        number: int | str,
        *,
        repo: str | None = None,
        state: str | None = None,
        assignees: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if state is not None:
            if state not in {"open", "closed"}:
                raise ValueError("Issue state must be open or closed.")
            changes["state"] = state
        if assignees is not None:
            if isinstance(assignees, str) or any(
                not isinstance(user, str) or not re.fullmatch(r"[\w.-]+", user)
                for user in assignees
            ):
                raise ValueError("Assignees must be usernames.")
            changes["assignees"] = list(assignees)
        if not changes:
            raise ValueError("No issue changes requested.")
        # Gitea answers an issue PATCH with 201 and the updated issue.
        return self._object(
            "PATCH", f"{self._repo_path(repo)}/issues/{issue_number(number)}", json=changes
        )

    def get_comment(self, comment_id: int | str, *, repo: str | None = None) -> dict[str, Any]:
        return self._object(
            "GET", f"{self._repo_path(repo)}/issues/comments/{issue_number(comment_id)}"
        )

    def update_comment(
        self, comment_id: int | str, body: str, *, repo: str | None = None
    ) -> dict[str, Any]:
        return self._object(
            "PATCH",
            f"{self._repo_path(repo)}/issues/comments/{issue_number(comment_id)}",
            json={"body": body},
        )

    def list_hooks(self, *, repo: str | None = None) -> list[dict[str, Any]]:
        return [row for page in self._pages(self._repo_path(repo) + "/hooks") for row in page]

    @staticmethod
    def hook_payload(url: str, secret: str, *, create: bool) -> dict[str, Any]:
        if not isinstance(secret, str) or not secret:
            raise ValueError("A webhook secret is required.")
        config = {"url": url, "content_type": "json"}
        if create:
            # Gitea can set a secret only at creation; GET hides it and PATCH ignores it.
            config["secret"] = secret
        return {
            "type": "gitea",
            "active": True,
            "events": list(HOOK_EVENTS),
            "branch_filter": "",
            "config": config,
        }

    def create_hook(self, url: str, secret: str, *, repo: str | None = None) -> dict[str, Any]:
        return self._object(
            "POST",
            self._repo_path(repo) + "/hooks",
            json=self.hook_payload(url, secret, create=True),
        )

    def update_hook(
        self, hook_id: int | str, url: str, secret: str, *, repo: str | None = None
    ) -> dict[str, Any]:
        return self._object(
            "PATCH",
            f"{self._repo_path(repo)}/hooks/{issue_number(hook_id)}",
            json=self.hook_payload(url, secret, create=False),
        )

    def delete_hook(self, hook_id: int | str, *, repo: str | None = None) -> bool:
        """Return False when the hook was already gone."""
        try:
            self._request("DELETE", f"{self._repo_path(repo)}/hooks/{issue_number(hook_id)}")
        except GiteaApiError as error:
            if error.status != 404:
                raise
            return False
        return True

    def check_file(
        self, path: str, ref: str | None, *, repo: str | None = None
    ) -> dict[str, Any] | None:
        """Regular-file metadata, never content; directories, links and missing paths are None.

        `contents-ext` without `includes` returns metadata only (`file_contents` for a file,
        `dir_contents` for a directory), unlike `contents`, which embeds the encoded file.
        """
        params = {"ref": git_ref(ref)} if ref else {}
        try:
            data, _ = self._request(
                "GET",
                f"{self._repo_path(repo)}/contents-ext/{repository_file_path(path)}",
                params=params,
            )
        except GiteaApiError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise GiteaApiError(200, "Gitea returned invalid file metadata.")
        entry = data.get("file_contents")
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise GiteaApiError(200, "Gitea returned invalid file metadata.")
        return entry if entry.get("type") == "file" else None

    def get_file(self, path: str, ref: str | None, *, repo: str | None = None) -> str:
        params = {"ref": git_ref(ref)} if ref else {}
        data, _ = self._request(
            "GET",
            f"{self._repo_path(repo)}/raw/{repository_file_path(path)}",
            params=params,
            raw=True,
        )
        return data or ""

    def _ref_path(self, kind: str, name: str, repo: str | None) -> str:
        segments = git_ref(name).split("/")
        return f"{self._repo_path(repo)}/{kind}/" + "/".join(quote(s, safe="") for s in segments)

    def get_branch(self, name: str, *, repo: str | None = None) -> dict[str, Any]:
        return self._object("GET", self._ref_path("branches", name, repo))

    def get_tag(self, name: str, *, repo: str | None = None) -> dict[str, Any]:
        return self._object("GET", self._ref_path("tags", name, repo))

    def commit_range(
        self,
        head: str,
        *,
        exclude: str | None = None,
        max_commits: int,
        repo: str | None = None,
        on_page: Callable[[list[dict[str, Any]]], None] | None = None,
        stop_when_over: bool = False,
    ) -> CommitRange:
        """Commits reachable from `head` but not from `exclude`, newest first.

        Pages use immutable SHAs, so concurrent pushes cannot shift them. Completion is proven by
        Gitea's total count or by an empty page, never by a short page: servers clamp page sizes.
        With `on_page`, each page is handed over and not retained: `rows` stays empty and
        `fetched` counts the commits delivered.
        """
        git_ref(head)
        if exclude is not None:
            git_ref(exclude)
        if type(max_commits) is not int or max_commits <= 0:
            raise ValueError("A positive commit bound is required.")
        params: dict[str, Any] = {
            "sha": head,
            "files": "true",
            "stat": "false",
            "verification": "false",
            "limit": COMMIT_LIMIT,
        }
        if exclude is not None:
            params["not"] = exclude
        rows: list[dict[str, Any]] = []
        fetched = 0
        seen: set[str] = set()
        total: int | None = None
        page = 0
        while True:
            page += 1
            data, headers = self._request(
                "GET", self._repo_path(repo) + "/commits", params={**params, "page": page}
            )
            batch = self._rows(data)
            value = next((v for k, v in headers.items() if k.lower() == "x-total-count"), "")
            total = int(value) if value.isdigit() else total
            if stop_when_over and total is not None and total > max_commits:
                return CommitRange(rows, False, total, fetched)
            fresh = []
            for row in batch:
                sha = row.get("sha")
                if not is_sha(sha):
                    raise GiteaApiError(200, "Gitea returned an invalid commit.")
                if sha not in seen:
                    seen.add(sha)
                    fresh.append(row)
            if batch and not fresh:
                raise GiteaApiError(200, "Gitea repeated a commit page.")
            # Hold no page across `on_page` or the next request; streamed pages are not retained.
            del batch, data
            if not fresh:
                if total is not None and fetched < total:
                    raise GiteaApiError(200, "Gitea returned an incomplete commit range.")
                return CommitRange(rows, True, fetched if total is None else total, fetched)
            room = max_commits - fetched
            if len(fresh) > room:
                fresh = fresh[:room]
            if fresh:
                fetched += len(fresh)
                if on_page is not None:
                    on_page(fresh)
                else:
                    rows.extend(fresh)
            del fresh
            if total is not None and fetched >= total:
                return CommitRange(rows, True, total, fetched)
            if fetched >= max_commits:
                if total is not None:
                    return CommitRange(rows, False, total, fetched)
                # Without a total, one more page distinguishes an exact fit from truncation.
                data, _ = self._request(
                    "GET",
                    self._repo_path(repo) + "/commits",
                    params={**params, "page": page + 1},
                )
                more = any(row.get("sha") not in seen for row in self._rows(data))
                return CommitRange(rows, not more, None if more else fetched, fetched)


class CommitRange:
    """Result of a bounded commit walk; `total` is None when Gitea did not report it.

    `fetched` counts the commits walked; `rows` holds them only when no `on_page` consumer
    took them.
    """

    __slots__ = ("rows", "complete", "total", "fetched")

    def __init__(
        self,
        rows: list[dict[str, Any]],
        complete: bool,
        total: int | None,
        fetched: int | None = None,
    ):
        self.rows = rows
        self.complete = complete
        self.total = total
        self.fetched = len(rows) if fetched is None else fetched
