import subprocess
import threading
from datetime import UTC, datetime

import pytest
from git_fixtures import ALICE, BOB, GIT_AVAILABLE, serve, source_repo

from sentry_gitea import mirror as mirror_module
from sentry_gitea.mirror import (
    BlameCommit,
    GitError,
    GitMirror,
    Remote,
    mirror_path,
    parse_porcelain,
)

TOKEN = "synthetic-mirror-token-7f3a"
FIRST = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SECOND = datetime(2026, 9, 2, 8, 30, tzinfo=UTC)

needs_git = pytest.mark.skipif(not GIT_AVAILABLE, reason="git is not installed")

PORCELAIN = (
    "1111111111111111111111111111111111111111 1 1 1\n"
    "author Alice Example\n"
    "author-mail <alice@example.com>\n"
    "author-time 1756728000\n"
    "author-tz +0200\n"
    "committer Carol\n"
    "committer-mail <carol@example.com>\n"
    "committer-time 1756731600\n"
    "committer-tz +0200\n"
    "summary Add app\n"
    "boundary\n"
    "filename app.py\n"
    "\ta\n"
    "2222222222222222222222222222222222222222 2 3\n"
    "author Bob Example\n"
    "author-mail <bob@example.com>\n"
    "author-time 1756800000\n"
    "author-tz +0000\n"
    "committer Bob Example\n"
    "committer-mail <bob@example.com>\n"
    "committer-time 1756800000\n"
    "committer-tz +0000\n"
    "summary Fix: tabs\tand spaces\n"
    "previous 1111111111111111111111111111111111111111 app.py\n"
    "filename app.py\n"
    "\tauthor not-a-header\n"
)


def test_porcelain_lines_carry_author_committer_time_and_summary():
    assert parse_porcelain(PORCELAIN) == {
        1: BlameCommit(
            sha="1" * 40,
            author_name="Alice Example",
            author_email="alice@example.com",
            committed_at=datetime.fromtimestamp(1756731600, UTC),
            summary="Add app",
        ),
        3: BlameCommit(
            sha="2" * 40,
            author_name="Bob Example",
            author_email="bob@example.com",
            committed_at=datetime.fromtimestamp(1756800000, UTC),
            summary="Fix: tabs\tand spaces",
        ),
    }
    assert parse_porcelain("") == {}


def test_mirror_path_is_keyed_by_integration_and_stable_repository_id(tmp_path):
    assert mirror_path(tmp_path, 7, 41) == tmp_path / "7" / "41.git"


@pytest.fixture
def published(tmp_path):
    repo = source_repo(tmp_path / "src", FIRST, SECOND)
    with serve(tmp_path / "src" / "served") as server:
        yield repo, server


@pytest.fixture
def remote(published):
    _repo, server = published
    return lambda: Remote(server.url + "/team/repo.git", TOKEN)


@pytest.fixture
def mirror(tmp_path):
    return GitMirror(mirror_path(tmp_path / "mirrors", 7, 41))


@pytest.fixture
def git_calls(monkeypatch):
    calls = []
    popen = subprocess.Popen

    def recording(argv, **kwargs):
        calls.append((list(argv), dict(kwargs.get("env") or {})))
        return popen(argv, **kwargs)

    monkeypatch.setattr(mirror_module.subprocess, "Popen", recording)
    return calls


@needs_git
def test_clone_sends_the_token_only_as_a_header_from_the_environment(
    published, remote, mirror, tmp_path, git_calls
):
    _repo, server = published
    assert mirror.clone(remote) is True
    assert mirror.exists()
    assert server.authorizations and set(server.authorizations) == {f"Bearer {TOKEN}"}
    assert all(TOKEN not in arg for argv, _env in git_calls for arg in argv)
    header = f"Authorization: Bearer {TOKEN}"
    assert any(env.get("GIT_CONFIG_VALUE_0") == header for _argv, env in git_calls)
    stored = [p for p in (tmp_path / "mirrors").rglob("*") if p.is_file()]
    assert stored and not [p for p in stored if TOKEN.encode() in p.read_bytes()]
    assert "url" not in (mirror.path / "config").read_text()
    assert not (mirror.path / "hooks").exists()


@needs_git
def test_blame_attributes_each_line_and_skips_missing_lines_files_and_revisions(
    published, remote, mirror
):
    repo, _server = published
    mirror.clone(remote)
    sha = mirror.resolve("main")
    assert sha == repo.commits["Fix b"]
    blamed = mirror.blame(sha, "app.py", [1, 2, 3, 99])
    assert set(blamed) == {1, 2, 3}
    assert blamed[2] == BlameCommit(
        sha=repo.commits["Fix b"],
        author_name=BOB[0],
        author_email=BOB[1],
        committed_at=SECOND,
        summary="Fix b",
    )
    assert blamed[1].sha == blamed[3].sha == repo.commits["Add app"]
    assert (blamed[1].author_email, blamed[1].committed_at) == (ALICE[1], FIRST)
    assert mirror.blame(sha, "missing.py", [1]) == {}
    assert mirror.blame(sha, "app.py", [99]) == {}
    assert mirror.blame("0" * 40, "app.py", [1]) == {}
    assert mirror.resolve("no-such-branch") is None
    assert mirror.resolve("--output=/tmp/x") is None
    assert mirror.has_commit(sha) and not mirror.has_commit("main")


@needs_git
def test_refresh_is_throttled_and_picks_up_new_commits(published, remote, tmp_path):
    repo, server = published
    mirror = GitMirror(mirror_path(tmp_path / "mirrors", 7, 41), fetch_interval=3600)
    mirror.clone(remote)
    third = repo.commit("Add d", {"app.py": "a\nB\nc\nd\n"}, author=ALICE, when=SECOND)
    requests = len(server.authorizations)
    assert mirror.refresh(remote) is False
    assert len(server.authorizations) == requests
    assert mirror.resolve(third) is None
    eager = GitMirror(mirror.path, fetch_interval=0)
    assert eager.refresh(remote) is True
    assert eager.resolve("main") == third
    assert eager.blame(third, "app.py", [4])[4].summary == "Add d"


@needs_git
def test_clone_is_idempotent_and_ignores_crashed_partial_clones(published, remote, mirror):
    _repo, server = published
    crashed = mirror.path.with_name(f".{mirror.path.name}.tmp-dead")
    crashed.mkdir(parents=True)
    (crashed / "HEAD").write_text("ref: refs/heads/main\n")
    assert not mirror.exists()
    assert mirror.clone(remote) is True
    assert not crashed.exists()
    requests = len(server.authorizations)
    assert mirror.clone(remote) is False
    assert len(server.authorizations) == requests


@needs_git
def test_concurrent_clones_create_one_mirror(published, remote, mirror):
    results = []
    threads = [threading.Thread(target=lambda: results.append(mirror.clone(remote))) for _ in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
    assert mirror.resolve("main")


@needs_git
def test_failed_clone_leaves_nothing_and_does_not_leak_the_token(tmp_path, mirror):
    with serve(tmp_path) as server:
        with pytest.raises(GitError) as error:
            mirror.clone(lambda: Remote(server.url + "/missing.git", TOKEN))
    assert TOKEN not in str(error.value)
    assert not mirror.exists()
    assert not list(mirror.path.parent.glob(".*.tmp-*"))


@needs_git
def test_delete_removes_the_mirror_and_is_idempotent(published, remote, mirror):
    mirror.clone(remote)
    mirror.delete()
    mirror.delete()
    assert not mirror.exists()
    assert not mirror.path.with_suffix(".lock").exists()


def test_missing_git_binary_is_a_git_error(mirror, monkeypatch):
    monkeypatch.setenv("PATH", str(mirror.path))
    with pytest.raises(GitError, match="not installed"):
        mirror.clone(lambda: Remote("http://127.0.0.1:9/x.git", TOKEN))
