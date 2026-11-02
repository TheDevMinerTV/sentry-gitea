"""Host-side helpers for the integration sync/webhook E2E. Original test code.

Upstream Sentry 26.3.1 and Gitea 28.1.0 sources were read only to confirm routes,
permission scopes and payload/template shapes. Rules kept from the other E2E helpers:

- git runs over HTTP with an origin-scoped ``Authorization`` header passed through the
  environment: never in argv, URLs, files or rendered output;
- webhook HMACs for synthetic deliveries are computed inside the Sentry web container,
  so the installation's webhook secret (and each per-repository secret derived from it)
  never reaches the host;
- polls are bounded (flows.poll), and failures never render response bodies, tokens
  or raw logs (only our own ``sentry-gitea:`` lines after a credential check).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import requests
import sentry_sdk
from browser import REDIRECTS, Browser, origin
from flows import PluginFlow, check, poll, poll_budget, without_credentials
from harness import ROOT, WEBHOOK_URL_PREFIX, run, self_hosted_compose
from helpers import IntegrationEnvironment
from redaction import Credential, exposure_message

GIT_TIMEOUT = 120
SYNC_KEYS = (
    "sync_status_forward",
    "sync_status_reverse",
    "sync_comments",
    "sync_assignee_forward",
    "sync_assignee_reverse",
)
# Contract hook events, as Gitea 28.1 reports them: `issues_only` is stored as
# `issues`, and `pull_request` also enables the pull_request_* sub-events
# (routers/api/v1/utils/hook.go updateHookEvents, models/webhook EventsArray).
HOOK_EVENTS = {"push", "issues", "issue_assign", "pull_request"}
SIGNATURES = re.compile(r"^SENTRY_GITEA_E2E_SIGNATURES (\[[0-9a-f\", ]*\])$", re.MULTILINE)
DELETIONS = re.compile(r"^SENTRY_GITEA_E2E_DELETIONS (\d+)$", re.MULTILINE)
PUSH_AUTHOR = ("E2E Push", "e2e-push@example.invalid")
FILE_AUTHOR = {"name": "E2E Sync", "email": "e2e-sync@example.invalid"}


def webhook_url_prefix() -> str:
    """The prefix the installer wrote into sentry.conf.py (same override variable)."""
    return (os.environ.get("GITEA_WEBHOOK_URL_PREFIX") or WEBHOOK_URL_PREFIX).rstrip("/")


def webhook_path(integration_id: str | int, repo_id: str | int | None) -> str:
    """Per-repository endpoint; `repo_id` (Gitea's repository ID) None is the retired form."""
    path = f"/extensions/gitea/webhook/{integration_id}/"
    return path if repo_id is None else f"{path}{repo_id}/"


# --- git over HTTP ------------------------------------------------------------------


def git_environment(gitea_url: str, username: str, token: str) -> dict[str, str]:
    """An isolated git configuration whose only credential is an origin-scoped header.

    Inherited GIT_*/SSH_* variables (tracing, askpass, alternate configs) are dropped,
    global/system config and credential helpers are ignored, and prompts fail fast.
    """
    base = origin(gitea_url)
    basic = base64.b64encode(f"{username}:{token}".encode()).decode()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "SSH_", "GCM_"))
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "2",
            # http.<url>.extraHeader: sent only to the Gitea origin, never to redirects elsewhere.
            "GIT_CONFIG_KEY_0": f"http.{base}/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_AUTHOR_NAME": PUSH_AUTHOR[0],
            "GIT_AUTHOR_EMAIL": PUSH_AUTHOR[1],
            "GIT_COMMITTER_NAME": PUSH_AUTHOR[0],
            "GIT_COMMITTER_EMAIL": PUSH_AUTHOR[1],
        }
    )
    return environment


def git(args: list[str], *, cwd: Path, env: dict[str, str], timeout: int = GIT_TIMEOUT) -> str:
    """Run git; failures name only the subcommand and exit status."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f"git {args[0]} could not run or timed out; output withheld") from None
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode}); output withheld")
    return result.stdout


@dataclass(frozen=True)
class PushedCommit:
    sha: str
    message: str
    changes: tuple[tuple[str, str], ...]  # (path, A/M/D)


# Seven commits in one push: Gitea 28.1 truncates the payload to the newest five
# (FEED_MAX_COMMIT_NUM), so the two oldest (and their file changes) need the backfill.
PUSH_PLAN = (
    ("A", "f1"),
    ("A", "f2"),
    ("A", "f3"),
    ("M", "f1"),
    ("A", "f4"),
    ("D", "f2"),
    ("M", "f3"),
)


def push_commits(
    gitea_url: str, full_name: str, username: str, token: str, workdir: Path, case: str
) -> tuple[str, list[PushedCommit]]:
    """Clone over HTTP, create PUSH_PLAN commits and push them once; return (before, commits)."""
    env = git_environment(gitea_url, username, token)
    clone = workdir / "clone"
    git(
        ["clone", "--quiet", "--branch", "main", f"{gitea_url}/{full_name}.git", str(clone)],
        cwd=workdir,
        env=env,
    )
    before = git(["rev-parse", "HEAD"], cwd=clone, env=env).strip()
    started = int(time.time()) - 60
    commits = []
    for index, (kind, name) in enumerate(PUSH_PLAN, 1):
        path = f"push/{name}.txt"
        target = clone / path
        if kind == "D":
            target.unlink()
        else:
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"{case} commit {index}\n")
        message = f"E2E push {case} {index}/{len(PUSH_PLAN)}"
        stamp = f"@{started + index} +0000"
        git(["add", "--all"], cwd=clone, env=env)
        git(
            ["commit", "--quiet", "--no-verify", "-m", message],
            cwd=clone,
            env=env | {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        )
        sha = git(["rev-parse", "HEAD"], cwd=clone, env=env).strip()
        commits.append(PushedCommit(sha, message, ((path, kind),)))
    listed = git(["rev-list", "--reverse", f"{before}..HEAD"], cwd=clone, env=env).split()
    check(listed == [commit.sha for commit in commits], "Local E2E commits are not linear")
    git(["push", "--quiet", "origin", "HEAD:refs/heads/main"], cwd=clone, env=env)
    return before, commits


# --- Gitea webhook history (templates/repo/settings/webhook/history.tmpl) ---------------


@dataclass(frozen=True)
class Delivery:
    uuid: str
    headers: dict[str, str]
    payload: Any
    status: int | None
    replay: str

    @property
    def event(self) -> str:
        return self.headers.get("X-Gitea-Event", "")

    @property
    def event_type(self) -> str:
        return self.headers.get("X-Gitea-Event-Type", "")

    def summary(self) -> str:
        """Event names, action and status only; never payload content."""
        action = self.payload.get("action", "") if isinstance(self.payload, dict) else ""
        name = self.event + (f"/{self.event_type}" if self.event_type != self.event else "")
        return f"{name}{f'({action})' if action else ''} -> {self.status or 'pending'}"


def parse_headers(text: str) -> dict[str, str]:
    headers = {}
    for line in text.splitlines():
        key, separator, value = line.partition(": ")
        if separator and key.strip():
            headers[key.strip()] = value.strip()
    return headers


class HookHistory(HTMLParser):
    """Recent deliveries of one hook: UUID, request headers/payload, status, replay form.

    Gitea 28.1 renders, per delivery, a ``button.show-panel`` holding the UUID, a
    ``form[action$=/replay/<uuid>]``, the response status as a ``span.label`` in the
    ``a[data-tab=response-<id>]`` tab, and ``pre.webhook-info`` blocks: request headers
    (starting with "Request URL:"), request payload, response headers and body.
    """

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.deliveries: list[Delivery] = []
        self._item: dict[str, Any] | None = None
        self._capture: str | None = None
        self._text: list[str] = []
        self._response_tab = False
        self.feed(html)
        self.close()
        self._finish()

    def _begin(self, kind: str) -> None:
        self._capture, self._text = kind, []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "button" and "show-panel" in classes:
            self._finish()
            self._item = {"uuid": "", "pres": [], "status": "", "replay": ""}
            self._begin("uuid")
        elif self._item is None:
            return
        elif tag == "form" and "/replay/" in (attributes.get("action") or ""):
            self._item["replay"] = attributes["action"]
        elif tag == "a" and (attributes.get("data-tab") or "").startswith("response-"):
            self._response_tab = True
        elif tag == "span" and self._response_tab and "label" in classes:
            self._begin("status")
        elif tag == "pre" and "webhook-info" in classes:
            self._begin("pre")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._response_tab = False
        if self._capture is None or self._item is None:
            return
        if tag != {"uuid": "button", "status": "span", "pre": "pre"}[self._capture]:
            return
        text = "".join(self._text)
        if self._capture == "pre":
            self._item["pres"].append(text)
        else:
            self._item[self._capture] = text.strip()
        self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._text.append(data)

    def _finish(self) -> None:
        item, self._item = self._item, None
        if not item or not item["uuid"]:
            return
        headers: dict[str, str] = {}
        payload = None
        pres = item["pres"]
        for index, text in enumerate(pres):
            if text.lstrip().startswith("Request URL:"):
                headers = parse_headers(text)
                if index + 1 < len(pres):
                    try:
                        payload = json.loads(pres[index + 1])
                    except ValueError:
                        payload = None
                break
        status = int(item["status"]) if item["status"].isdigit() else None
        self.deliveries.append(Delivery(item["uuid"], headers, payload, status, item["replay"]))


# --- privileged helpers executed inside the Sentry web container ----------------------


def sentry_exec(script: str, *, label: str, credentials: tuple[Credential, ...]) -> str:
    """Run a short script with `sentry exec`; the caller parses one marker line."""
    output = run(
        self_hosted_compose() + ["exec", "-T", "web", "sentry", "exec"],
        label=label,
        input_text=script,
        timeout=180,
    )
    if message := exposure_message(output, credentials, where=f"{label} output"):
        raise RuntimeError(message)
    return output


# (Gitea repository ID, raw body); repository None signs with the integration-wide
# secret itself, which no hook uses any more (a negative control).
Signable = tuple[int | None, bytes]


def sign_script(integration_id: int, items: list[Signable]) -> str:
    encoded = json.dumps([[repo_id, base64.b64encode(body).decode()] for repo_id, body in items])
    return "\n".join(
        [
            "import base64, hashlib, hmac, json",
            "from sentry.integrations.models.integration import Integration",
            f"integration = Integration.objects.get(id={integration_id:d}, provider='gitea')",
            "secret = integration.metadata['webhook_secret'].encode()",
            "def key(repo_id):",
            "    if repo_id is None:",
            "        return secret",
            "    # The hook secret Gitea holds: hex HMAC-SHA256(secret, 'repo:<id>').",
            "    message = ('repo:%d' % repo_id).encode()",
            "    return hmac.new(secret, message, hashlib.sha256).hexdigest().encode()",
            f"items = json.loads({encoded!r})",
            "signatures = [",
            "    hmac.new(key(repo_id), base64.b64decode(body), hashlib.sha256).hexdigest()",
            "    for repo_id, body in items",
            "]",
            "print('SENTRY_GITEA_E2E_SIGNATURES ' + json.dumps(signatures))",
        ]
    )


def sign_webhook_bodies(
    integration_id: int, items: list[Signable], credentials: tuple[Credential, ...]
) -> list[str]:
    """Gitea's X-Gitea-Signature (hex HMAC-SHA256 of the raw body) for synthetic bodies.

    Each body is signed with the per-repository hook secret of its repository ID. Only
    the HMACs leave the container; the secrets are read, derived and used there.
    """
    check(type(integration_id) is int and integration_id > 0, "Invalid integration ID")
    check(
        all(repo_id is None or (type(repo_id) is int and repo_id > 0) for repo_id, _ in items),
        "Invalid repository ID to sign for",
    )
    output = sentry_exec(
        sign_script(integration_id, items),
        label="Sign E2E webhook bodies in the Sentry web container",
        credentials=credentials,
    )
    match = SIGNATURES.search(output)
    check(match is not None, "Sentry web container returned no webhook signatures")
    signatures = json.loads(match[1])
    check(
        len(signatures) == len(items)
        and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in signatures),
        "Sentry web container returned malformed webhook signatures",
    )
    return signatures


def deletion_script(repository_id: int) -> str:
    return "\n".join(
        [
            "from django.utils import timezone",
            "from sentry.deletions.models.scheduleddeletion import CellScheduledDeletion",
            "from sentry.deletions.tasks.scheduled import run_deletion",
            "due = CellScheduledDeletion.objects.filter(",
            f"    model_name='Repository', object_id={repository_id:d}, in_progress=False,",
            "    date_scheduled__lte=timezone.now())",
            "claimed = [row.id for row in due if CellScheduledDeletion.objects.filter(",
            "    id=row.id, in_progress=False).update(in_progress=True)]",
            "for deletion_id in claimed:",
            "    run_deletion(deletion_id=deletion_id)",
            "print('SENTRY_GITEA_E2E_DELETIONS ' + str(len(claimed)))",
        ]
    )


def run_repository_deletion(repository_id: int, credentials: tuple[Credential, ...]) -> int:
    """Run the due scheduled deletion of one repository now, as the */15 cron would.

    Sentry's repository DELETE only schedules deletion (immediately due when the
    repository has no commits); `pending_delete` then calls the provider's
    on_delete_repository on the deletion task's first pass.
    """
    check(type(repository_id) is int and repository_id > 0, "Invalid repository ID")
    output = sentry_exec(
        deletion_script(repository_id),
        label="Run the scheduled Sentry repository deletion",
        credentials=credentials,
    )
    match = DELETIONS.search(output)
    check(match is not None, "Sentry web container did not report the repository deletion")
    return int(match[1])


def sentry_gitea_log_lines(
    since: str,
    credentials: tuple[Credential, ...],
    *,
    services: tuple[str, ...] = ("web", "taskworker"),
    forbidden: tuple[str, ...] = (),
) -> list[str]:
    """Our own `sentry-gitea:` lines, after checking the complete capture for leaks.

    `forbidden` holds unique, non-secret markers (e.g. a webhook body's case ID) that
    must never be logged. Raw logs are never rendered.
    """
    try:
        result = subprocess.run(
            self_hosted_compose()
            + ["logs", "--no-color", "--timestamps", "--since", since, "--tail", "5000"]
            + list(services),
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=poll_budget(15),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Read Sentry logs failed or timed out; output withheld") from None
    check(result.returncode == 0, "Read Sentry logs failed; output withheld")
    service = "/".join(services)
    without_credentials(result.stdout, credentials, service=service)
    check(
        not any(marker and marker in result.stdout for marker in forbidden),
        f"Sentry logged webhook body content; log service={service}; content withheld",
    )
    return [line for line in result.stdout.splitlines() if "sentry-gitea:" in line]


def synthetic_push(owner: dict, repo_id: int, name: str, case: str) -> dict:
    """A Gitea 28.1 PushPayload (modules/structs/hook.go) for a repository Sentry lacks."""
    before, after = uuid4().hex + uuid4().hex[:8], uuid4().hex + uuid4().hex[:8]
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    person = {"name": "E2E Synthetic", "email": "e2e-synthetic@example.invalid", "username": ""}
    commit = {
        "id": after,
        "message": f"E2E synthetic {case}\n",
        "url": f"http://127.0.0.1:13000/{owner['login']}/{name}/commit/{after}",
        "author": person,
        "committer": person,
        "verification": None,
        "timestamp": now,
        "added": [f"synthetic/{case}.txt"],
        "removed": [],
        "modified": [],
    }
    return {
        "ref": "refs/heads/main",
        "before": before,
        "after": after,
        "compare_url": "",
        "commits": [commit],
        "total_commits": 1,
        "head_commit": commit,
        "repository": {
            "id": repo_id,
            "owner": {"id": owner["id"], "login": owner["login"]},
            "name": name,
            "full_name": f"{owner['login']}/{name}",
            "private": True,
            "default_branch": "main",
        },
        "pusher": {"id": owner["id"], "login": owner["login"]},
        "sender": {"id": owner["id"], "login": owner["login"]},
    }


# --- the flow -----------------------------------------------------------------------


class SyncFlow(PluginFlow):
    """Reuse PluginFlow's redacting APIs and ingestion; never configure plugins/rules."""

    def __init__(self, env: IntegrationEnvironment, browser: Browser) -> None:
        super().__init__(env.stack)
        self.settings = env
        self.release = "sentry-gitea-e2e@0.2-sync"
        self.credentials = env.credentials
        for api in (self.sentry, self.gitea, self.plugin_gitea):
            api.credentials = self.credentials
        self.browser = browser
        self.browser.private_credentials.update(self.credentials)
        user = self.gitea.request("GET", f"/api/v1/users/{env.gitea_e2e_username}")
        check(user["is_admin"] is False, "Sync E2E must use the non-admin Gitea user")
        self.gitea_user = user
        self.admin_login = self.gitea.request("GET", "/api/v1/user")["login"]
        self.org_path = f"/api/0/organizations/{env.stack.sentry_org}"
        self.integration = ""
        self.repository_ids: list[str] = []
        self.code_mapping_ids: list[str] = []
        self.since = datetime.now(UTC).isoformat()
        # Webhook deliveries are unauthenticated: never send the Sentry token there.
        self.webhooks = requests.Session()

    # Sentry installation and configuration

    def install(self) -> str:
        self.integration = self.browser.install(
            self.settings, f"user:{self.gitea_user['id']}", self.sentry
        )
        return self.integration

    @property
    def integration_path(self) -> str:
        return f"{self.org_path}/integrations/{self.integration}/"

    def configure(self, *enabled: str, user_mappings: dict[str, str] | None = None) -> dict:
        check(set(enabled) <= set(SYNC_KEYS), "Unknown sync option in the E2E")
        data: dict[str, Any] = {key: key in enabled for key in SYNC_KEYS}
        data["user_mappings"] = user_mappings or {}
        self.sentry.request("POST", self.integration_path, json=data)
        config = self.sentry.request("GET", self.integration_path).get("configData") or {}
        check(
            all(config.get(key) is data[key] for key in SYNC_KEYS),
            "Sentry did not persist the Gitea sync options (configData)",
        )
        return config

    def browser_user(self) -> dict:
        """The module browser's Sentry identity (the seeded non-superuser owner)."""
        response = self.browser.request(
            "GET", self.env.sentry_url + "/api/0/users/me/", expected=(200,)
        )
        try:
            user = response.json()
        except ValueError:
            raise RuntimeError("Sentry identity response was not JSON; content withheld") from None
        check(
            user.get("username") == self.settings.sentry_owner_username,
            "Module browser is not the seeded Sentry owner",
        )
        return user

    # Gitea repositories, files and hooks (admin token, host URL)

    def create_gitea_repo(self, prefix: str) -> dict:
        name = f"{prefix}-{uuid4().hex[:12]}"
        repo = self.gitea.request(
            "POST",
            f"/api/v1/admin/users/{self.settings.gitea_e2e_username}/repos",
            expected=(201,),
            json={"name": name, "auto_init": True, "default_branch": "main", "private": True},
        )
        check(
            repo["full_name"] == f"{self.settings.gitea_e2e_username}/{name}",
            "Gitea created the E2E repository under an unexpected owner",
        )
        return repo

    def head(self, full_name: str) -> str:
        branch = self.gitea.request("GET", f"/api/v1/repos/{full_name}/branches/main")
        return branch["commit"]["id"]

    def change_files(self, full_name: str, message: str, files: list[dict]) -> str:
        """One commit via the contents API; returns its SHA."""
        result = self.gitea.request(
            "POST",
            f"/api/v1/repos/{full_name}/contents",
            expected=(201,),
            json={
                "branch": "main",
                "message": message,
                "author": FILE_AUTHOR,
                "committer": FILE_AUTHOR,
                "files": files,
            },
        )
        return result["commit"]["sha"]

    def upload(self, path: str, content: str) -> dict:
        return {
            "operation": "upload",
            "path": path,
            "content": base64.b64encode(content.encode()).decode(),
        }

    def file_sha(self, full_name: str, path: str) -> str:
        return self.gitea.request(
            "GET", f"/api/v1/repos/{full_name}/contents/{path}", params={"ref": "main"}
        )["sha"]

    def hooks(self, full_name: str) -> list[dict]:
        hooks = self.gitea.request(
            "GET", f"/api/v1/repos/{full_name}/hooks", params={"page": 1, "limit": 50}
        )
        check(isinstance(hooks, list) and len(hooks) < 50, "Unexpected Gitea hook listing")
        return hooks

    def single_hook(self, full_name: str, repo_id: int) -> dict:
        hooks = self.hooks(full_name)
        check(len(hooks) == 1, f"Expected exactly one Gitea hook, found {len(hooks)}")
        hook = hooks[0]
        expected_url = webhook_url_prefix() + webhook_path(self.integration, repo_id)
        assert hook["type"] == "gitea"
        assert hook["active"] is True
        assert hook["config"]["content_type"] == "json"
        assert hook["config"]["url"] == expected_url
        events = set(hook["events"])
        assert HOOK_EVENTS <= events, "Hook lacks a contract event"
        extra = {event for event in events - HOOK_EVENTS if not event.startswith("pull_request_")}
        assert not extra, f"Hook subscribes to events outside the contract: {sorted(extra)}"
        return hook

    def hook_history(self, full_name: str, hook_id: int) -> list[Delivery]:
        path = f"/{full_name}/settings/hooks/{hook_id}"
        for attempt in range(2):
            response = self.browser.follow(self.browser.request("GET", self.env.gitea_url + path))
            if urlsplit(response.url).path == path:
                return HookHistory(response.text).deliveries
            check(attempt == 0, "Gitea hook settings need the repository owner's browser session")
            self.browser.login_gitea(self.settings)
        raise AssertionError("Unreachable hook history state")

    def find_delivery(
        self, full_name: str, hook_id: int, *, event: str, after: str, exclude=frozenset()
    ) -> Delivery | None:
        return next(
            (
                delivery
                for delivery in self.hook_history(full_name, hook_id)
                if delivery.event == event
                and delivery.uuid not in exclude
                and delivery.status is not None
                and isinstance(delivery.payload, dict)
                and delivery.payload.get("after") == after
            ),
            None,
        )

    def replay(self, full_name: str, hook_id: int, delivery: Delivery) -> None:
        """Gitea's own "Redeliver" (routers/web/repo/setting/webhook.go ReplayWebhook)."""
        page = f"{self.env.gitea_url}/{full_name}/settings/hooks/{hook_id}"
        check(bool(delivery.replay), "Gitea history offered no replay form for the delivery")
        target = urljoin(page, delivery.replay)
        check(origin(target) == origin(page), "Gitea replay form left the Gitea origin")
        response = self.browser.request(
            "POST",
            target,
            data={},
            headers={
                "Origin": origin(page),
                "Referer": origin(page) + "/",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        check(response.status_code in REDIRECTS, "Gitea did not accept the delivery replay")

    def delivery_summary(self, full_name: str, hook_id: int) -> str:
        try:
            deliveries = self.hook_history(full_name, hook_id)[:6]
        except (RuntimeError, AssertionError):
            return "Gitea delivery history unavailable"
        listed = "; ".join(delivery.summary() for delivery in deliveries) or "none"
        return f"Recent Gitea deliveries (newest first): {listed}"

    def diagnostics(self, full_name: str, hook_id: int) -> str:
        try:
            logs = "\n".join(sentry_gitea_log_lines(self.since, self.credentials)[-8:])
        except RuntimeError:
            logs = "unavailable"
        return (
            self.delivery_summary(full_name, hook_id)
            + "\nRecent sentry-gitea log lines (web/taskworker):\n"
            + (logs or "none")
        )

    # Sentry repositories, commits and releases

    def add_repository(self, full_name: str, *, expected=(201,)) -> dict:
        created = self.sentry.request(
            "POST",
            f"{self.org_path}/repos/",
            expected=expected,
            json={
                "provider": "integrations:gitea",
                "installation": self.integration,
                "identifier": full_name,
            },
        )
        if isinstance(created, dict) and created.get("id"):
            if str(created["id"]) not in self.repository_ids:
                self.repository_ids.append(str(created["id"]))
        return created

    def delete_repository(self, repository_id: str) -> None:
        self.sentry.request(
            "DELETE", f"{self.org_path}/repos/{repository_id}/", expected=(202, 404)
        )
        if repository_id in self.repository_ids:
            self.repository_ids.remove(repository_id)

    def sentry_commits(self, repository_id: str) -> list[dict]:
        rows = self.sentry.request(
            "GET", f"{self.org_path}/repos/{repository_id}/commits/", params={"per_page": 100}
        )
        check(isinstance(rows, list) and len(rows) < 100, "Unexpected Sentry commit listing")
        return rows

    def create_release(self, version: str, **data: Any) -> dict:
        return self.sentry.request(
            "POST",
            f"{self.org_path}/releases/",
            expected=(201,),
            json={"version": version, "projects": [self.env.sentry_project], **data},
        )

    def release_commits(self, version: str) -> list[dict]:
        return self.sentry.request(
            "GET", f"{self.org_path}/releases/{version}/commits/", params={"per_page": 100}
        )

    def release_files(self, version: str) -> set[tuple[str, str, str]]:
        rows = self.sentry.request(
            "GET", f"{self.org_path}/releases/{version}/commitfiles/", params={"per_page": 100}
        )
        check(isinstance(rows, list) and len(rows) < 100, "Unexpected release file listing")
        return {(row["commitMessage"].strip(), row["filename"], row["type"]) for row in rows}

    # Sentry groups, linked issues and notes

    def send_event(self, *, frames: list[dict] | None = None) -> dict:
        """PluginFlow.send_and_wait, plus optional stack frames."""
        self.case_id = uuid4().hex
        self.message = f"sentry-gitea-sync-e2e-{self.case_id}"
        event_id = uuid4().hex
        exception: dict[str, Any] = {
            "type": "RuntimeError",
            "value": self.message,
            "mechanism": {"type": "generic", "handled": False},
        }
        if frames:
            exception["stacktrace"] = {"frames": frames}
        with sentry_sdk.Client(
            dsn=self.ingest_dsn(),
            default_integrations=False,
            auto_enabling_integrations=False,
            send_client_reports=False,
            shutdown_timeout=15,
            transport_queue_size=10,
        ) as client:
            captured = client.capture_event(
                {
                    "event_id": event_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "message": self.message,
                    "level": "error",
                    "platform": "python",
                    "fingerprint": [self.case_id],
                    "release": self.release,
                    "environment": self.environment,
                    "tags": {"e2e_case": self.case_id},
                    "exception": {"values": [exception]},
                }
            )
            check(captured == event_id, "Sentry SDK discarded the E2E event")
            client.flush(timeout=15)
        self.last_event_id = event_id

        def find_event() -> dict | None:
            events = self.sentry.request(
                "GET", f"{self.project_path}/events/", params={"query": self.message, "limit": 100}
            )
            return next((event for event in events if event["eventID"] == event_id), None)

        event = poll(f"Sentry project event {event_id} to be indexed", find_event)
        groups = poll(
            f"Sentry project issue for event {event_id}",
            lambda: self.sentry.request(
                "GET", f"{self.project_path}/issues/", params={"query": event_id}
            ),
        )
        check(len(groups) == 1, "Event lookup did not return exactly one Sentry group")
        check(str(groups[0]["id"]) == str(event["groupID"]), "Issue/event group IDs disagree")
        return groups[0]

    def group(self, group: dict) -> dict:
        return self.sentry.request("GET", self.group_path(group))

    def update_group(self, group: dict, **data: Any) -> dict:
        return self.sentry.request("PUT", self.group_path(group), json=data)

    def link_new_issue(self, group: dict, repo: dict) -> int:
        """Create a Gitea issue through the integration; return its number."""
        linked = self.sentry.request(
            "POST",
            f"{self.group_path(group)}integrations/{self.integration}/",
            expected=(201,),
            json={
                "repo": repo["full_name"],
                "title": f"Sync {self.message}",
                "description": f"E2E sync fixture for {group['permalink']}",
            },
        )
        repo_id, _, number = str(linked.get("key", "")).partition("#")
        check(
            repo_id == str(repo["id"]) and number.isdigit(),
            "Integration create returned an unexpected canonical key",
        )
        return int(number)

    def issue(self, repo: dict, number: int) -> dict:
        return self.gitea.request("GET", f"/api/v1/repos/{repo['full_name']}/issues/{number}")

    def edit_issue(self, repo: dict, number: int, **data: Any) -> dict:
        # Gitea 28.1 issueEditIssue answers 201.
        return self.gitea.request(
            "PATCH",
            f"/api/v1/repos/{repo['full_name']}/issues/{number}",
            expected=(201,),
            json=data,
        )

    def issue_comments(self, repo: dict, number: int) -> list[dict]:
        # Gitea 28.1 returns the whole list here (no page/limit on issueGetComments).
        comments = self.gitea.request(
            "GET", f"/api/v1/repos/{repo['full_name']}/issues/{number}/comments"
        )
        check(isinstance(comments, list), "Gitea issue comments did not return a list")
        return comments

    def notes(self, group: dict) -> list[dict]:
        return self.sentry.request("GET", f"{self.group_path(group)}comments/")

    # Webhook endpoint (host ingress, no Sentry credentials)

    def post_webhook(
        self,
        integration_id: str | int,
        repo_id: str | int | None,
        body: bytes,
        *,
        event: str,
        signature: str | None,
    ) -> int:
        headers = {"Content-Type": "application/json", "X-Gitea-Event": event}
        if signature is not None:
            headers["X-Gitea-Signature"] = signature
        path = webhook_path(integration_id, repo_id)
        # Like Gitea's own redelivery, resending after a dropped connection is safe:
        # webhook handlers are idempotent. Never retried on a read timeout or any status.
        for attempt in range(3):
            try:
                response = self.webhooks.post(
                    self.env.sentry_url + path,
                    data=body,
                    headers=headers,
                    timeout=poll_budget(15),
                    allow_redirects=False,
                )
                break
            except requests.ConnectionError:
                if attempt == 2:
                    raise RuntimeError(
                        f"POST {path} could not connect; response withheld"
                    ) from None
                time.sleep(2)
            except requests.RequestException:
                raise RuntimeError(f"POST {path} failed; response withheld") from None
        without_credentials(response.text, self.credentials, where=f"response POST {path}")
        return response.status_code

    # Code mappings, stack-trace links and CODEOWNERS

    def project_id(self) -> str:
        return str(self.sentry.request("GET", f"{self.project_path}/")["id"])

    def create_code_mapping(self, repository_id: str, stack_root: str, source_root: str) -> dict:
        mapping = self.sentry.request(
            "POST",
            f"{self.org_path}/code-mappings/",
            expected=(201,),
            json={
                "projectId": self.project_id(),
                "integrationId": self.integration,
                "repositoryId": int(repository_id),
                "stackRoot": stack_root,
                "sourceRoot": source_root,
                "defaultBranch": "main",
            },
        )
        self.code_mapping_ids.append(str(mapping["id"]))
        return mapping

    def stacktrace_link(self, **params: str) -> dict:
        return self.sentry.request("GET", f"{self.project_path}/stacktrace-link/", params=params)

    def codeowners(self, mapping_id: str, *, expected=(200,)) -> dict:
        return self.sentry.request(
            "GET", f"{self.org_path}/code-mappings/{mapping_id}/codeowners/", expected=expected
        )

    def close(self) -> None:
        failures = []
        try:
            if self.integration:
                try:
                    self.configure()  # every option back to its default (false), no mappings
                except (RuntimeError, AssertionError) as error:
                    failures.append(f"reset sync options: {error}")
            for mapping_id in list(self.code_mapping_ids):
                try:
                    self.sentry.request(
                        "DELETE",
                        f"{self.org_path}/code-mappings/{mapping_id}/",
                        expected=(204, 404),
                    )
                    self.code_mapping_ids.remove(mapping_id)
                except RuntimeError as error:
                    failures.append(str(error))
            for repository_id in list(self.repository_ids):
                try:
                    # Repositories with commits are deleted (and unhooked) by Sentry's
                    # scheduled deletion an hour later; Gitea repositories stay for inspection.
                    self.delete_repository(repository_id)
                except RuntimeError as error:
                    failures.append(str(error))
        finally:
            self.webhooks.close()
            super().close()
        check(not failures, "Sync E2E cleanup failed: " + "; ".join(failures))


def frame(filename: str, function: str, lineno: int) -> dict:
    return {
        "filename": filename,
        "abs_path": f"/srv/app/{filename}",
        "module": filename.removesuffix(".py").replace("/", "."),
        "function": function,
        "lineno": lineno,
        "in_app": True,
    }


def event_frames(flow: SyncFlow, group: dict) -> list[dict]:
    event = flow.sentry.request("GET", f"{flow.group_path(group)}events/{flow.last_event_id}/")
    for entry in event.get("entries", []):
        if entry.get("type") == "exception":
            values = entry["data"]["values"]
            return (values[-1].get("stacktrace") or {}).get("frames") or []
    return []


def wait_stable(description: str, probe, *, seconds: int) -> None:
    """Require `probe()` to stay true for `seconds` (bounded by flows.poll)."""
    end = time.monotonic() + seconds

    def stable() -> bool:
        check(bool(probe()), f"{description} changed during the observation window")
        return time.monotonic() >= end

    poll(description, stable, seconds=seconds + 30)
