"""Real integration sync/webhook flows (see docs/INTEGRATION.md) plus host-only regressions.

Original test code. Sentry 26.3.1 and Gitea 28.1.0 sources were read only to confirm
routes, permission scopes and payload/template shapes (see docs/TESTING.md).
"""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
from browser import Browser
from flows import check, log_field, poll
from helpers import IntegrationEnvironment
from scm import (
    PUSH_PLAN,
    HookHistory,
    SyncFlow,
    deletion_script,
    event_frames,
    frame,
    git,
    git_environment,
    push_commits,
    run_repository_deletion,
    sentry_gitea_log_lines,
    sign_script,
    sign_webhook_bodies,
    synthetic_push,
    wait_stable,
    webhook_path,
    webhook_url_prefix,
)

# --- host-only regressions (no Docker, no network) ------------------------------------


def install_block(tmp_path: Path, monkeypatch, runs: int = 2) -> tuple[str, list[str]]:
    """Run the real installer twice with Docker/uv stubbed; return (runtime block, builds)."""
    import harness

    directory = tmp_path / "sentry"
    directory.mkdir()
    settings = directory / "sentry.conf.py"
    settings.write_text("# operator settings\n")
    monkeypatch.setattr(harness, "STATE", tmp_path / ".e2e")
    monkeypatch.setenv("SELF_HOSTED_DIR", str(tmp_path))
    monkeypatch.setattr(harness, "active_profiles", lambda: set())
    monkeypatch.setattr(
        harness,
        "compose_config",
        lambda: {"services": {"web": {"image": "sentry-self-hosted-local"}}},
    )
    built = []

    def fake_build(path):
        wheel = path / "sentry_gitea-test.whl"
        wheel.touch()
        return wheel

    def fake_run(command, **kwargs):
        if "ps" in command:
            return "web\n"
        if "build" in command:
            built.append(settings.read_text())
        return ""

    monkeypatch.setattr(harness, "build_wheel", fake_build)
    monkeypatch.setattr(harness, "run", fake_run)
    for _ in range(runs):
        harness.install()
    text = settings.read_text()
    assert text.count(harness.BEGIN) == 1 and text.count(harness.END) == 1
    return text.split(harness.BEGIN, 1)[1].split(harness.END, 1)[0], built


def active_lines(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.strip() and not line.startswith("#")]


def test_installer_sets_webhook_prefix_and_task_import_once(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GITEA_WEBHOOK_URL_PREFIX", raising=False)
    block, built = install_block(tmp_path, monkeypatch)
    lines = active_lines(block)
    assert lines.count("GITEA_WEBHOOK_URL_PREFIX = 'http://web:9000'") == 1
    assert sum("sentry_gitea.integration.tasks" in line for line in lines) == 1
    assert sum(line.startswith("GITEA_PLUGIN_ALLOWED_BASE_URLS") for line in lines) == 1
    # Non-secret settings are in the image build context too.
    assert built and all("GITEA_WEBHOOK_URL_PREFIX = 'http://web:9000'" in text for text in built)
    assert all("GITEA_PLUGIN_ALLOWED_BASE_URLS = ('http://gitea:3000',)" in text for text in built)
    namespace = {
        "TASKWORKER_IMPORTS": ("sentry.tasks.base",),
        "TASKWORKER_SCHEDULES": {},
        "SENTRY_DEFAULT_INTEGRATIONS": (),
        "SENTRY_API_PAGINATION_ALLOWLIST": set(),
    }
    exec(compile(block, "sentry.conf.py", "exec"), namespace)
    assert namespace["GITEA_WEBHOOK_URL_PREFIX"] == "http://web:9000"
    assert namespace["GITEA_PLUGIN_ALLOWED_BASE_URLS"] == ("http://gitea:3000",)
    assert namespace["TASKWORKER_IMPORTS"].count("sentry_gitea.integration.tasks") == 1
    assert "sentry.tasks.base" in namespace["TASKWORKER_IMPORTS"]
    # The shipped snippet's OAuth keep-alive schedule is installed (and in the image) too.
    assert namespace["TASKWORKER_SCHEDULES"] == {
        "gitea-identity-refresh": {
            "task": "integrations:sentry_gitea.integration.refresh_identities",
            "schedule": timedelta(hours=12),
        }
    }
    assert all('TASKWORKER_SCHEDULES["gitea-identity-refresh"]' in text for text in built)


def test_sync_settings_respect_snippet_and_validate_override(monkeypatch) -> None:
    from harness import sync_settings

    monkeypatch.delenv("GITEA_WEBHOOK_URL_PREFIX", raising=False)
    commented = (
        "# GITEA_WEBHOOK_URL_PREFIX = 'x'\n"
        "# TASKWORKER_IMPORTS += ('sentry_gitea.integration.tasks',)\n"
        "# GITEA_PLUGIN_ALLOWED_BASE_URLS = ('http://x',)\n"
        "# GITEA_BLAME_ENABLED = True"
    )
    assert sync_settings(commented) == (
        "GITEA_WEBHOOK_URL_PREFIX = 'http://web:9000'\n"
        "TASKWORKER_IMPORTS += ('sentry_gitea.integration.tasks',)\n"
        "GITEA_PLUGIN_ALLOWED_BASE_URLS = ('http://gitea:3000',)\n"
        "GITEA_BLAME_ENABLED = True"
    )
    enabled = (
        "GITEA_WEBHOOK_URL_PREFIX = os.environ['X']\n"
        'TASKWORKER_IMPORTS = (*TASKWORKER_IMPORTS, "sentry_gitea.integration.tasks")\n'
        "GITEA_PLUGIN_ALLOWED_BASE_URLS = ('http://gitea:3000', 'https://git.example')\n"
        "GITEA_BLAME_ENABLED = False"
    )
    assert sync_settings(enabled) == ""
    monkeypatch.setenv("GITEA_WEBHOOK_URL_PREFIX", "http://sentry-web:9000/")
    assert sync_settings("").startswith("GITEA_WEBHOOK_URL_PREFIX = 'http://sentry-web:9000'\n")
    for invalid in ("web:9000", "http://web:9000/path", "http://web'9000", "ftp://web"):
        monkeypatch.setenv("GITEA_WEBHOOK_URL_PREFIX", invalid)
        with pytest.raises(ValueError, match="origin"):
            sync_settings("")


HISTORY_HTML = """
<div class="flex-divided-list">
 <div class="item">
  <div class="flex-left-right"><div class="flex-text-inline">
   <span class="tw-text-green"><svg></svg></span>
   <button type="button" class="btn interact-bg tw-p-2 toggle show-panel"
     data-panel="#info-9">uuid-new</button>
  </div><span class="tw-text-text-light">now</span></div>
  <div class="info tw-hidden tw-mt-2" id="info-9">
   <div class="ui top attached tabular menu">
    <a class="item active" data-tab="request-9">
     <span class="resize-for-semibold" data-text="Request">Request</span></a>
    <a class="item" data-tab="response-9">
     <span class="resize-for-semibold" data-text="Response">Response</span>
     <span class="ui green label">204</span></a>
    <div class="right menu">
     <form class="tw-py-2" action="/e2e-user/sync-1/settings/hooks/4/replay/uuid-new"
       method="post"><span data-tooltip-content="Replay">
      <button type="submit" class="ui tiny button tw-mr-0"><svg></svg></button></span>
     </form>
    </div>
   </div>
   <div class="ui bottom attached tab segment active" data-tab="request-9">
    <h5>Headers</h5><pre class="webhook-info"><strong>Request URL:</strong> http://web:9000/hook/5/
<strong>Request method:</strong> POST
<strong>X-Gitea-Event:</strong> issues
<strong>X-Gitea-Event-Type:</strong> issue_assign
</pre>
    <h5>Payload</h5>
    <pre class="webhook-info">{
  &#34;action&#34;: &#34;unassigned&#34;,
  &#34;after&#34;: &#34;&lt;none&gt;&#34;
}</pre>
   </div>
   <div class="ui bottom attached tab segment" data-tab="response-9">
    <h5>Headers</h5><pre class="webhook-info"><strong>Vary:</strong> Cookie
</pre><h5>Body</h5><pre class="webhook-info"></pre>
   </div>
  </div>
 </div>
 <div class="item">
  <div class="flex-left-right"><div class="flex-text-inline">
   <span class="tw-text-orange"><svg></svg></span>
   <button type="button" class="btn interact-bg tw-p-2 toggle show-panel"
     data-panel="#info-8">uuid-old</button>
  </div></div>
  <div class="info tw-hidden tw-mt-2" id="info-8">
   <div class="ui top attached tabular menu">
    <a class="item" data-tab="response-8">
     <span class="resize-for-semibold">Response</span><span class="ui label">-</span></a>
   </div>
   <div class="ui bottom attached tab segment active" data-tab="request-8">-</div>
  </div>
 </div>
</div>
"""


def test_hook_history_parser_reads_gitea_28_template() -> None:
    newest, pending = HookHistory(HISTORY_HTML).deliveries
    assert newest.uuid == "uuid-new"
    assert newest.status == 204
    assert newest.event == "issues" and newest.event_type == "issue_assign"
    assert newest.headers["Request URL"] == "http://web:9000/hook/5/"
    assert newest.payload == {"action": "unassigned", "after": "<none>"}
    assert newest.replay == "/e2e-user/sync-1/settings/hooks/4/replay/uuid-new"
    assert newest.summary() == "issues/issue_assign(unassigned) -> 204"
    assert (pending.uuid, pending.status, pending.payload) == ("uuid-old", None, None)
    assert pending.headers == {} and pending.replay == ""


def test_git_environment_scopes_the_credential_and_drops_inherited_git_state(monkeypatch) -> None:
    for name in ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_COUNT"):
        monkeypatch.setenv(name, "1")
    token = "private-test-token-value"
    env = git_environment("http://127.0.0.1:13000", "e2e-admin", token)
    assert not {"GIT_TRACE", "GIT_TRACE_CURL", "GIT_ASKPASS", "SSH_ASKPASS"} & env.keys()
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_0"] == "http.http://127.0.0.1:13000/.extraHeader"
    header = base64.b64encode(f"e2e-admin:{token}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {header}"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("credential.helper", "")
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_TERMINAL_PROMPT"] == "0"
    assert all(token not in key for key in env)
    with pytest.raises(RuntimeError, match="origin|credentials"):
        git_environment("http://user:secret@127.0.0.1:13000", "e2e-admin", token)


def test_git_failures_withhold_output(monkeypatch, tmp_path) -> None:
    def failing(command, **kwargs):
        assert all("private-test-token-value" not in part for part in command)
        return subprocess.CompletedProcess(command, 128, stdout="private-test-token-value")

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(RuntimeError) as error:
        git(["push", "origin", "HEAD:refs/heads/main"], cwd=tmp_path, env={})
    assert str(error.value) == "git push failed (exit 128); output withheld"


def test_push_commits_creates_one_linear_seven_commit_push(tmp_path) -> None:
    # A local bare repository stands in for Gitea; the credential header is inert here.
    env = git_environment("file://", "e2e-admin", "unused-test-token")
    remote = tmp_path / "remote" / "e2e-user" / "repo.git"
    remote.parent.mkdir(parents=True)
    git(["init", "--quiet", "--bare", "--initial-branch=main", str(remote)], cwd=tmp_path, env=env)
    seed = tmp_path / "seed"
    git(["init", "--quiet", "--initial-branch=main", str(seed)], cwd=tmp_path, env=env)
    (seed / "README.md").write_text("seed\n")
    git(["add", "--all"], cwd=seed, env=env)
    git(["commit", "--quiet", "-m", "seed"], cwd=seed, env=env)
    git(["push", "--quiet", str(remote), "main"], cwd=seed, env=env)
    work = tmp_path / "work"
    work.mkdir()
    before, commits = push_commits(
        f"file://{tmp_path / 'remote'}", "e2e-user/repo", "e2e-admin", "unused", work, "case"
    )
    listed = git(["rev-list", "--reverse", f"{before}..main"], cwd=remote, env=env).split()
    assert listed == [commit.sha for commit in commits] and len(listed) == len(PUSH_PLAN) == 7
    names = git(
        ["log", "--reverse", "--format=", "--name-status", f"{before}..main"], cwd=remote, env=env
    ).split()
    assert list(zip(names[::2], names[1::2], strict=True)) == [
        (kind, f"push/{name}.txt") for kind, name in PUSH_PLAN
    ]
    assert [commit.message for commit in commits][0] == "E2E push case 1/7"


def stub_modules(monkeypatch, modules: dict[str, dict]) -> None:
    for name, attributes in modules.items():
        parts = name.split(".")
        for index in range(1, len(parts) + 1):
            module_name = ".".join(parts[:index])
            if module_name not in sys.modules or index == len(parts):
                stub = ModuleType(module_name)
                stub.__path__ = []
                monkeypatch.setitem(sys.modules, module_name, stub)
        sys.modules[name].__dict__.update(attributes)


def test_sign_script_derives_per_repository_secrets_without_printing_them(monkeypatch) -> None:
    secret = "private-webhook-test-secret"
    integration = SimpleNamespace(metadata={"webhook_secret": secret})
    lookups = []

    def get(**where):
        lookups.append(where)
        return integration

    stub_modules(
        monkeypatch,
        {
            "sentry.integrations.models.integration": {
                "Integration": SimpleNamespace(objects=SimpleNamespace(get=get))
            }
        },
    )
    items = [(41, b'{"a": 1}'), (42, b'{"a": 1}'), (41, b"{not json"), (None, b'{"a": 1}')]
    output = io.StringIO()
    with redirect_stdout(output):
        exec(sign_script(7, items), {})
    assert lookups == [{"id": 7, "provider": "gitea"}]

    def repo_secret(repo_id: int) -> str:
        # The shared spec: hex HMAC-SHA256(key=webhook_secret, msg=f"repo:{repo_id}").
        return hmac.new(secret.encode(), f"repo:{repo_id}".encode(), hashlib.sha256).hexdigest()

    for value in (secret, repo_secret(41), repo_secret(42)):
        assert value not in output.getvalue()
    keys = [secret if repo is None else repo_secret(repo) for repo, _ in items]
    expected = [
        hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        for key, (_, body) in zip(keys, items, strict=True)
    ]
    # The same body signs differently per repository; the integration-wide secret differs too.
    assert len(set(expected[:2] + expected[3:])) == 3
    assert output.getvalue() == f"SENTRY_GITEA_E2E_SIGNATURES {json.dumps(expected)}\n"

    import scm

    monkeypatch.setattr(scm, "self_hosted_compose", lambda: ["docker", "compose"])
    monkeypatch.setattr(scm, "run", lambda *a, **k: "noise\n" + output.getvalue())
    assert sign_webhook_bodies(7, items, ()) == expected
    monkeypatch.setattr(scm, "run", lambda *a, **k: "SENTRY_GITEA_E2E_SIGNATURES []\n")
    with pytest.raises(pytest.fail.Exception, match="malformed"):
        sign_webhook_bodies(7, items, ())
    with pytest.raises(pytest.fail.Exception, match="repository ID"):
        sign_webhook_bodies(7, [(0, b"{}")], ())


def test_webhook_paths_are_per_repository_and_the_retired_form_is_explicit() -> None:
    assert webhook_path(7, 41) == "/extensions/gitea/webhook/7/41/"
    assert webhook_path("7", "41") == "/extensions/gitea/webhook/7/41/"
    assert webhook_path(7, None) == "/extensions/gitea/webhook/7/"


def test_deletion_script_runs_only_the_due_repository_deletion(monkeypatch) -> None:
    rows = [SimpleNamespace(id=11), SimpleNamespace(id=12)]
    filters, ran = [], []

    class Objects:
        def filter(self, **where):
            filters.append(where)
            if "model_name" in where:
                return rows
            # A concurrent cron run already claimed deletion 12.
            return SimpleNamespace(update=lambda **values: int(where["id"] == 11))

    stub_modules(
        monkeypatch,
        {
            "django.utils": {"timezone": SimpleNamespace(now=lambda: "now")},
            "sentry.deletions.models.scheduleddeletion": {
                "CellScheduledDeletion": SimpleNamespace(objects=Objects())
            },
            "sentry.deletions.tasks.scheduled": {
                "run_deletion": lambda **kwargs: ran.append(kwargs)
            },
        },
    )
    output = io.StringIO()
    with redirect_stdout(output):
        exec(deletion_script(42), {})
    assert filters[0] == {
        "model_name": "Repository",
        "object_id": 42,
        "in_progress": False,
        "date_scheduled__lte": "now",
    }
    assert ran == [{"deletion_id": 11}]
    assert output.getvalue() == "SENTRY_GITEA_E2E_DELETIONS 1\n"

    import scm

    monkeypatch.setattr(scm, "self_hosted_compose", lambda: ["docker", "compose"])
    monkeypatch.setattr(scm, "run", lambda *a, **k: output.getvalue())
    assert run_repository_deletion(42, ()) == 1


def test_synthetic_push_matches_the_gitea_push_shape() -> None:
    payload = synthetic_push({"id": 3, "login": "e2e-user"}, 99, "sync-unknown", "case")
    assert re.fullmatch(r"[0-9a-f]{40}", payload["before"])
    assert re.fullmatch(r"[0-9a-f]{40}", payload["after"])
    assert payload["commits"][0]["id"] == payload["after"]
    assert payload["total_commits"] == len(payload["commits"]) == 1
    assert payload["repository"]["owner"] == {"id": 3, "login": "e2e-user"}
    assert payload["repository"]["full_name"] == "e2e-user/sync-unknown"
    datetime.fromisoformat(payload["commits"][0]["timestamp"])


# --- real stack ------------------------------------------------------------------------


@dataclass
class SeedCommit:
    sha: str
    message: str
    changes: tuple[tuple[str, str], ...]


@dataclass
class SyncStack:
    flow: SyncFlow
    repo: dict  # Gitea repository JSON
    repository_id: str  # Sentry repository ID
    hook_id: int
    initial_sha: str
    history: list[SeedCommit]


def seed_history(flow: SyncFlow, repo: dict, case: str) -> list[SeedCommit]:
    """Three commits created before the hook exists: only compare_commits can see them."""
    name = repo["full_name"]
    commits = []
    message = f"E2E history {case} 1/3"
    sha = flow.change_files(
        name,
        message,
        [flow.upload("history/a.txt", "a1\n"), flow.upload("history/b.txt", "b1\n")],
    )
    commits.append(SeedCommit(sha, message, (("history/a.txt", "A"), ("history/b.txt", "A"))))
    message = f"E2E history {case} 2/3"
    sha = flow.change_files(name, message, [flow.upload("history/a.txt", "a2\n")])
    commits.append(SeedCommit(sha, message, (("history/a.txt", "M"),)))
    message = f"E2E history {case} 3/3"
    sha = flow.change_files(
        name,
        message,
        [
            {
                "operation": "delete",
                "path": "history/b.txt",
                "sha": flow.file_sha(name, "history/b.txt"),
            }
        ],
    )
    commits.append(SeedCommit(sha, message, (("history/b.txt", "D"),)))
    return commits


@pytest.fixture(scope="module")
def sync_stack(
    integration_env: IntegrationEnvironment, integration_browser: Browser
) -> Iterator[SyncStack]:
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("Sync E2E shares one installation, repository and hook; run without xdist")
    flow = SyncFlow(integration_env, integration_browser)
    try:
        # Fresh Gitea cookies and grant; the module keeps its single Sentry login.
        flow.browser.reset(integration_env)
        flow.install()
        repo = flow.create_gitea_repo("sync")
        initial = flow.head(repo["full_name"])
        history = seed_history(flow, repo, uuid4().hex[:12])
        created = flow.add_repository(repo["full_name"])
        hook = flow.single_hook(repo["full_name"], repo["id"])
        flow.browser.login_gitea(integration_env)
        yield SyncStack(flow, repo, str(created["id"]), hook["id"], initial, history)
    finally:
        flow.close()


def wait_group(flow: SyncFlow, group: dict, description: str, predicate, detail) -> dict:
    return poll(
        description,
        lambda: current if predicate(current := flow.group(group)) else None,
        failure_detail=detail,
    )


@pytest.mark.e2e
class TestSync:
    def test_repository_hook_lifecycle(self, sync_stack: SyncStack) -> None:
        """Scenario 1: add → exactly one contract hook (re-add reuses it); delete → removed."""
        flow = sync_stack.flow
        remote = flow.create_gitea_repo("sync-hook")
        name = remote["full_name"]
        created = flow.add_repository(name)
        assert created["name"] == name
        assert created["externalId"] == str(remote["id"])
        assert created["url"] == f"{flow.env.gitea_url}/{name}"
        assert created["provider"]["id"] == "integrations:gitea"
        assert str(created["integrationId"]) == flow.integration
        hook = flow.single_hook(name, remote["id"])
        again = flow.add_repository(name, expected=(201, 400))
        if isinstance(again, dict) and again.get("id"):
            assert str(again["id"]) == str(created["id"]), "Re-adding created a second repository"
        assert [item["id"] for item in flow.hooks(name)] == [hook["id"]], "Re-add duplicated hook"
        flow.delete_repository(str(created["id"]))
        # No commits: Sentry schedules the deletion as immediately due (normally run by
        # the */15 cron). Running it now calls on_delete_repository via pending_delete.
        check(
            run_repository_deletion(int(created["id"]), flow.credentials) == 1,
            "Sentry had no due scheduled deletion for the deleted repository",
        )
        poll(
            "Gitea hook removal after the Sentry repository deletion",
            lambda: flow.hooks(name) == [],
            failure_detail=lambda: flow.diagnostics(name, hook["id"]),
        )
        flow.sentry.request("GET", f"{flow.org_path}/repos/{created['id']}/", expected=(404,))

    def test_hook_at_the_retired_url_is_replaced_on_add(self, sync_stack: SyncStack) -> None:
        """Upgrade: adding the repository replaces a hook at the URL without a repository ID.

        Gitea never reveals or changes a hook secret, so the hook is recreated; a real Gitea
        delivery through it then proves Gitea holds the derived per-repository secret.
        """
        flow = sync_stack.flow
        remote = flow.create_gitea_repo("sync-legacy")
        name = remote["full_name"]
        legacy = flow.gitea.request(
            "POST",
            f"/api/v1/repos/{name}/hooks",
            expected=(201,),
            json={
                "type": "gitea",
                "active": True,
                "events": ["push"],
                "config": {
                    "url": webhook_url_prefix() + webhook_path(flow.integration, None),
                    "content_type": "json",
                    "secret": secrets.token_hex(16),
                },
            },
        )
        created = flow.add_repository(name)
        hook = flow.single_hook(name, remote["id"])
        assert hook["id"] != legacy["id"], "The hook at the retired URL was kept"
        head = flow.head(name)
        # Gitea's own "Test delivery": a signed push of the head commit.
        flow.gitea.request(
            "POST", f"/api/v1/repos/{name}/hooks/{hook['id']}/tests", expected=(204,)
        )
        delivery = poll(
            "Gitea's test delivery through the recreated hook",
            lambda: flow.find_delivery(name, hook["id"], event="push", after=head),
            failure_detail=lambda: flow.diagnostics(name, hook["id"]),
        )
        assert delivery.status == 204, delivery.summary()
        poll(
            "the test-delivered head commit in Sentry",
            lambda: head in {row["id"] for row in flow.sentry_commits(str(created["id"]))},
            failure_detail=lambda: flow.diagnostics(name, hook["id"]),
        )

    def test_webhook_authentication(self, sync_stack: SyncStack) -> None:
        """Scenario 2: 401 bad/missing/integration-wide signature, 404 unknown integration or
        retired URL, 204 unknown repository, 400 malformed payload."""
        flow = sync_stack.flow
        since = datetime.now(UTC).isoformat()
        case = uuid4().hex
        unknown_id = 2_000_000_000
        unknown_name = f"sync-unknown-{case[:12]}"
        payload = synthetic_push(flow.gitea_user, unknown_id, unknown_name, case)
        body = json.dumps(payload).encode()
        malformed = b'{"repository": ' + case.encode()
        # Unhandled events still name the repository their per-repository URL belongs to.
        release = json.dumps(
            {"action": "published", "case": case, "repository": payload["repository"]}
        ).encode()
        zeros = "0" * 64
        integration = flow.integration

        def post(
            data: bytes,
            signature: str | None,
            *,
            event: str = "push",
            target: str = integration,
            repo: int | None = unknown_id,
        ) -> int:
            return flow.post_webhook(target, repo, data, event=event, signature=signature)

        assert post(body, None) == 401
        assert post(body, zeros) == 401
        # Verify-before-parse: a bad signature on malformed JSON is still 401, not 400.
        assert post(malformed, zeros) == 401
        missing = "2147483647"
        flow.sentry.request("GET", f"{flow.org_path}/integrations/{missing}/", expected=(404,))
        assert post(body, zeros, target=missing) == 404
        signed, signed_malformed, signed_release, integration_wide = sign_webhook_bodies(
            int(integration),
            [(unknown_id, body), (unknown_id, malformed), (unknown_id, release), (None, body)],
            flow.credentials,
        )
        # Hooks hold per-repository secrets; the integration-wide secret signs nothing.
        assert post(body, integration_wide) == 401
        # The retired URL without a repository ID is gone, whatever the signature.
        assert post(body, signed, repo=None) == 404
        # A valid signature for a different body must not authenticate this one.
        assert post(release, signed) == 401
        assert post(body, signed) == 204
        assert post(malformed, signed_malformed) == 400
        assert post(release, signed_release, event="release") == 204
        assert (
            flow.sentry.request(
                "GET", f"{flow.org_path}/repos/", params={"query": unknown_name, "status": ""}
            )
            == []
        ), "An unknown-repository push created a Sentry repository"
        keys = {row["id"] for row in flow.sentry_commits(sync_stack.repository_id)}
        assert payload["after"] not in keys, "An unknown-repository push stored a commit"

        def rejected() -> list[str] | None:
            # The webhook endpoint runs in the web service; bodies must never be logged.
            logged = sentry_gitea_log_lines(
                since, flow.credentials, services=("web",), forbidden=(case,)
            )
            lines = [line for line in logged if "sentry-gitea: webhook rejected" in line]
            reasons = {
                (reason, status)
                for line in lines
                for reason, status in (
                    ("signature", "401"),
                    ("integration", "404"),
                    ("payload", "400"),
                )
                if log_field(line, "reason", reason) and log_field(line, "status", status)
            }
            return lines if len(reasons) == 3 else None

        poll("sentry-gitea: webhook rejected lines for signature/integration/payload", rejected)

    def test_webhook_secret_is_bound_to_its_repository(self, sync_stack: SyncStack) -> None:
        """A delivery signed with repository A's secret but naming repository B is refused.

        Gitea shows hook secrets to repository admins, so a leaked secret must not forge
        deliveries for any other repository of the installation.
        """
        flow, repo_a = sync_stack.flow, sync_stack.repo
        remote_b = flow.create_gitea_repo("sync-bound")
        created_b = flow.add_repository(remote_b["full_name"])
        flow.single_hook(remote_b["full_name"], remote_b["id"])
        integration = flow.integration
        forged = synthetic_push(flow.gitea_user, remote_b["id"], remote_b["name"], uuid4().hex)
        genuine = synthetic_push(flow.gitea_user, remote_b["id"], remote_b["name"], uuid4().hex)
        forged_body, genuine_body = json.dumps(forged).encode(), json.dumps(genuine).encode()
        with_a, with_b = sign_webhook_bodies(
            int(integration),
            [(repo_a["id"], forged_body), (remote_b["id"], genuine_body)],
            flow.credentials,
        )
        # A's secret at A's URL, but the payload names B: the URL's repository must match.
        assert (
            flow.post_webhook(
                integration, repo_a["id"], forged_body, event="push", signature=with_a
            )
            == 400
        )
        # A's secret presented at B's URL does not verify against B's secret.
        assert (
            flow.post_webhook(
                integration, remote_b["id"], forged_body, event="push", signature=with_a
            )
            == 401
        )
        # Control: B's own secret is accepted, so the refusals above were not incidental.
        assert (
            flow.post_webhook(
                integration, remote_b["id"], genuine_body, event="push", signature=with_b
            )
            == 204
        )

        def stored_b() -> set[str] | None:
            keys = {row["id"] for row in flow.sentry_commits(str(created_b["id"]))}
            return keys if genuine["after"] in keys else None

        keys = poll("the genuinely signed push commit in repository B", stored_b)
        assert forged["after"] not in keys, "Forged push stored for B"
        stored_a = {row["id"] for row in flow.sentry_commits(sync_stack.repository_id)}
        assert forged["after"] not in stored_a, "Forged push stored for A"

    def test_truncated_push_backfill_and_replay(self, sync_stack: SyncStack) -> None:
        """Scenario 3: one 7-commit push → all 7 (with files) in Sentry; replay is idempotent."""
        flow, repo = sync_stack.flow, sync_stack.repo
        name, hook_id = repo["full_name"], sync_stack.hook_id
        case = uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="sentry-gitea-push-") as workdir:
            before, commits = push_commits(
                flow.env.gitea_url,
                name,
                flow.admin_login,
                flow.env.gitea_token,
                Path(workdir),
                case,
            )
        shas = [commit.sha for commit in commits]
        head = shas[-1]

        def ingested() -> list[dict] | None:
            rows = flow.sentry_commits(sync_stack.repository_id)
            return rows if set(shas) <= {row["id"] for row in rows} else None

        rows = poll(
            "all 7 pushed commits in Sentry (5 from the payload, 2 from the backfill task)",
            ingested,
            failure_detail=lambda: flow.diagnostics(name, hook_id),
        )
        keys = [row["id"] for row in rows]
        check(len(keys) == len(set(keys)), "Sentry lists a commit twice")
        by_key = {row["id"]: row for row in rows}
        for commit in commits:
            row = by_key[commit.sha]
            assert row["message"].strip() == commit.message
            assert (row.get("author") or {}).get("email") == "e2e-push@example.invalid"
        delivery = poll(
            "Gitea's recorded push delivery",
            lambda: flow.find_delivery(name, hook_id, event="push", after=head),
            failure_detail=lambda: flow.diagnostics(name, hook_id),
        )
        assert delivery.status == 204, delivery.summary()
        delivered = [row["id"] for row in delivery.payload["commits"]]
        check(
            delivery.payload["before"] == before
            and delivery.payload["total_commits"] == 7
            and len(delivered) == 5
            and set(delivered) == set(shas[2:]),
            "Gitea did not deliver the expected truncated push (5 newest of 7 commits)",
        )
        version = f"sentry-gitea-e2e-push-{case}"
        flow.create_release(
            version, commits=[{"id": sha, "repository": name} for sha in reversed(shas)]
        )
        expected_files = {
            (commit.message, path, kind) for commit in commits for path, kind in commit.changes
        }
        files = flow.release_files(version)
        assert files == expected_files, "Push/backfill file changes differ from the pushed commits"
        baseline = sorted(keys)
        flow.replay(name, hook_id, delivery)
        replayed = poll(
            "Gitea's redelivery of the same push",
            lambda: flow.find_delivery(
                name, hook_id, event="push", after=head, exclude={delivery.uuid}
            ),
            failure_detail=lambda: flow.diagnostics(name, hook_id),
        )
        assert replayed.status == 204, replayed.summary()
        assert replayed.payload == delivery.payload

        def unchanged() -> bool:
            current = [row["id"] for row in flow.sentry_commits(sync_stack.repository_id)]
            check(len(current) == len(set(current)), "Replay created duplicate commits")
            return sorted(current) == baseline and flow.release_files(version) == files

        # The replay re-enqueues the backfill; give it time to (not) change anything.
        wait_stable("Sentry commits/file changes after the replay", unchanged, seconds=30)

    def test_release_commit_range(self, sync_stack: SyncStack) -> None:
        """Scenario 4: release refs (range) → compare_commits, including pre-hook commits."""
        flow, repo = sync_stack.flow, sync_stack.repo
        version = f"sentry-gitea-e2e-range-{uuid4().hex[:12]}"
        flow.create_release(
            version,
            refs=[
                {
                    "repository": repo["full_name"],
                    "previousCommit": sync_stack.initial_sha,
                    "commit": sync_stack.history[-1].sha,
                }
            ],
        )
        rows = poll(
            "release commits fetched through compare_commits",
            lambda: flow.release_commits(version),
            failure_detail=lambda: flow.diagnostics(repo["full_name"], sync_stack.hook_id),
        )
        expected = [commit.sha for commit in sync_stack.history]
        assert sorted(row["id"] for row in rows) == sorted(expected), (
            "Release commits must be exactly the range after previousCommit"
        )
        assert flow.release_files(version) == {
            (commit.message, path, kind)
            for commit in sync_stack.history
            for path, kind in commit.changes
        }

    def test_status_sync(self, sync_stack: SyncStack) -> None:
        """Scenario 5: resolve in Sentry closes the issue; reopening in Gitea unresolves."""
        flow, repo, hook_id = sync_stack.flow, sync_stack.repo, sync_stack.hook_id
        flow.configure("sync_status_forward", "sync_status_reverse")
        group = flow.send_event()
        number = flow.link_new_issue(group, repo)
        assert flow.issue(repo, number)["state"] == "open"
        assert flow.update_group(group, status="resolved")["status"] == "resolved"
        poll(
            "Gitea issue closed by Sentry's resolution",
            lambda: flow.issue(repo, number)["state"] == "closed",
            failure_detail=lambda: flow.diagnostics(repo["full_name"], hook_id),
        )
        flow.edit_issue(repo, number, state="open")
        wait_group(
            flow,
            group,
            "Sentry group unresolved after reopening the Gitea issue",
            lambda current: current["status"] == "unresolved",
            lambda: flow.diagnostics(repo["full_name"], hook_id),
        )
        # No echo: the inbound unresolve leaves the reopened issue open.
        wait_stable(
            "reopened Gitea issue state",
            lambda: flow.issue(repo, number)["state"] == "open",
            seconds=10,
        )

    def test_comment_sync(self, sync_stack: SyncStack) -> None:
        """Scenario 6: a Sentry note → one marked Gitea comment; editing edits that comment."""
        flow, repo, hook_id = sync_stack.flow, sync_stack.repo, sync_stack.hook_id
        flow.configure("sync_comments")
        group = flow.send_event()
        number = flow.link_new_issue(group, repo)
        text = f"E2E note {flow.case_id}"
        note = flow.sentry.request(
            "POST", f"{flow.group_path(group)}comments/", expected=(201,), json={"text": text}
        )
        marker = f"<!-- sentry-gitea-note {note['id']} -->"

        def marked() -> list[dict] | None:
            rows = [row for row in flow.issue_comments(repo, number) if marker in row["body"]]
            return rows or None

        rows = poll(
            "Gitea comment for the Sentry note",
            marked,
            failure_detail=lambda: flow.diagnostics(repo["full_name"], hook_id),
        )
        assert len(rows) == 1, "The note produced more than one Gitea comment"
        comment = rows[0]
        check(
            re.match(r"\*\*[^*\n]+\*\* wrote in Sentry:\n\n", comment["body"]) is not None,
            "Gitea comment does not start with '**<user name>** wrote in Sentry:'",
        )
        assert text in comment["body"]
        # Sentry stores the remote comment ID on the note when the task finishes; the
        # edit only syncs once it is there (group_notes_details.py update_external_comment).
        poll(
            "Sentry note to record the Gitea comment ID",
            lambda: any(
                str(row["id"]) == str(note["id"])
                and str((row.get("data") or {}).get("external_id")) == str(comment["id"])
                for row in flow.notes(group)
            ),
        )
        edited = f"E2E note edited {flow.case_id}"
        flow.sentry.request(
            "PUT", f"{flow.group_path(group)}comments/{note['id']}/", json={"text": edited}
        )

        def updated() -> list[dict] | None:
            rows = [row for row in flow.issue_comments(repo, number) if marker in row["body"]]
            return rows if any(edited in row["body"] for row in rows) else None

        rows = poll(
            "the same Gitea comment edited",
            updated,
            failure_detail=lambda: flow.diagnostics(repo["full_name"], hook_id),
        )
        assert [row["id"] for row in rows] == [comment["id"]], "Edit created another comment"
        assert text not in rows[0]["body"].replace(edited, "")

    def test_assignee_sync(self, sync_stack: SyncStack) -> None:
        """Scenario 7: Sentry assignment → mapped Gitea assignee; Gitea (un)assignment → Sentry."""
        flow, repo, hook_id = sync_stack.flow, sync_stack.repo, sync_stack.hook_id
        owner = flow.browser_user()
        check(bool(owner.get("email")), "Seeded Sentry owner has no email")
        gitea_login = flow.settings.gitea_e2e_username
        flow.configure(
            "sync_assignee_forward",
            "sync_assignee_reverse",
            user_mappings={owner["email"]: gitea_login},
        )
        group = flow.send_event()
        number = flow.link_new_issue(group, repo)

        def assignees() -> list[str]:
            return [row["login"] for row in flow.issue(repo, number).get("assignees") or []]

        assert assignees() == []
        flow.update_group(group, assignedTo=f"user:{owner['id']}")
        poll(
            "mapped Gitea assignee after the Sentry assignment",
            lambda: assignees() == [gitea_login],
            failure_detail=lambda: flow.diagnostics(repo["full_name"], hook_id),
        )
        flow.edit_issue(repo, number, assignees=[])
        wait_group(
            flow,
            group,
            "Sentry group unassigned after the Gitea unassignment",
            lambda current: current.get("assignedTo") is None,
            lambda: flow.diagnostics(repo["full_name"], hook_id),
        )
        flow.edit_issue(repo, number, assignees=[gitea_login])
        current = wait_group(
            flow,
            group,
            "Sentry group assigned to the mapped user after the Gitea assignment",
            lambda current: current.get("assignedTo") is not None,
            lambda: flow.diagnostics(repo["full_name"], hook_id),
        )
        assert current["assignedTo"]["type"] == "user"
        assert str(current["assignedTo"]["id"]) == str(owner["id"])
        assert assignees() == [gitea_login]

    def test_stacktrace_link(self, sync_stack: SyncStack) -> None:
        """Scenario 8: a mapped frame for an existing file links to the public Gitea URL."""
        flow, repo = sync_stack.flow, sync_stack.repo
        name = repo["full_name"]
        root = f"sync_{uuid4().hex[:12]}"
        flow.change_files(
            name, f"E2E source {root}", [flow.upload(f"src/{root}/existing.py", "VALUE = 1\n")]
        )
        mapping = flow.create_code_mapping(sync_stack.repository_id, f"{root}/", f"src/{root}/")
        group = flow.send_event(
            frames=[
                frame(f"{root}/missing.py", "caller", 7),
                frame(f"{root}/existing.py", "handler", 1),
            ]
        )
        frames = {item["filename"]: item for item in event_frames(flow, group)}
        check(
            {f"{root}/existing.py", f"{root}/missing.py"} <= frames.keys(),
            "Sentry did not store the E2E stack frames as sent",
        )
        base = f"{flow.env.gitea_url}/{name}/src/branch/main/src/{root}"
        results = {}
        for filename in (f"{root}/existing.py", f"{root}/missing.py"):
            item = frames[filename]
            # The parameters Sentry's frontend sends for a frame (absent values omitted).
            params = {
                "file": item["filename"],
                "absPath": item.get("absPath"),
                "module": item.get("module"),
                "lineNo": item.get("lineNo"),
                "platform": "python",
                "groupId": group["id"],
            }
            results[filename] = flow.stacktrace_link(
                **{key: str(value) for key, value in params.items() if value}
            )
        found = results[f"{root}/existing.py"]
        # `error` may still name an earlier, non-matching mapping of this project
        # (integrations/utils/stacktrace_link.py); the matched config and URL decide.
        assert found["config"]["id"] == mapping["id"]
        assert found["sourceUrl"] == f"{base}/existing.py"
        missing = results[f"{root}/missing.py"]
        assert missing["config"]["id"] == mapping["id"]
        assert missing["sourceUrl"] is None
        assert missing["error"] == "file_not_found"
        assert missing["attemptedUrl"] == f"{base}/missing.py"

    def test_codeowners(self, sync_stack: SyncStack) -> None:
        """Scenario 9: email-only CODEOWNERS is returned; @handles produce the contract error."""
        flow, repo = sync_stack.flow, sync_stack.repo
        name = repo["full_name"]
        root = f"owners_{uuid4().hex[:12]}"
        mapping = flow.create_code_mapping(sync_stack.repository_id, f"{root}/", "")
        emails = (
            f"# sentry-gitea E2E {root}\n"
            "*.py e2e-owner@example.invalid\n"
            "/docs/ e2e-user@example.invalid e2e-owner@example.invalid\n"
        )
        # A non-root location proves the lookup order beyond the first candidate.
        flow.change_files(
            name, f"E2E CODEOWNERS {root}", [flow.upload(".gitea/CODEOWNERS", emails)]
        )
        owners = flow.codeowners(mapping["id"])
        assert owners["filepath"] == ".gitea/CODEOWNERS"
        assert owners["raw"] == emails
        assert owners["html_url"] == (
            f"{flow.env.gitea_url}/{name}/src/branch/main/.gitea/CODEOWNERS"
        )
        # The root location is tried first, so it now shadows .gitea/CODEOWNERS.
        handles = "*.py @e2e-user\n/docs/ @e2e-org/maintainers e2e-owner@example.invalid\n"
        flow.change_files(
            name, f"E2E CODEOWNERS handles {root}", [flow.upload("CODEOWNERS", handles)]
        )
        error = flow.codeowners(mapping["id"], expected=(400,))
        detail = str(error.get("detail", ""))
        assert detail.startswith("Gitea CODEOWNERS supports email owners only; found @handles: "), (
            "CODEOWNERS @handle rejection does not use the contract message"
        )
        assert "@e2e-user" in detail
