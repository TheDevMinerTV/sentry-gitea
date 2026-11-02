import socket
import subprocess
import threading
import time
from datetime import UTC, datetime

import pytest
from git_fixtures import ALICE, BOB, GIT_AVAILABLE, git, redirect_to, serve, source_repo

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
def test_a_branch_wins_over_a_tag_of_the_same_name_and_only_exact_names_resolve(
    published, remote, mirror
):
    repo, _server = published
    first, second = repo.commits["Add app"], repo.commits["Fix b"]
    repo.tag("main", first, annotated=True)
    repo.tag("v1", first, annotated=True)
    repo.branch("refs/heads/main", first)
    mirror.clone(remote, ["main", "v1", "refs/heads/main"])
    assert mirror.resolve("main") == second
    assert mirror.resolve("v1") == first
    assert mirror.resolve(first) == first
    assert mirror.resolve("refs/heads/main") == first
    for ref in ("main~1", "main^{tree}", first[:12], "v*", "HEAD", "refs/tags/v1", "-v1", ""):
        assert mirror.resolve(ref) is None, ref


@needs_git
def test_refresh_is_throttled_and_picks_up_new_commits(published, remote, tmp_path):
    repo, server = published
    mirror = GitMirror(mirror_path(tmp_path / "mirrors", 7, 41), fetch_interval=3600)
    mirror.clone(remote)
    third = repo.commit("Add d", {"app.py": "a\nB\nc\nd\n"}, author=ALICE, when=SECOND)
    requests = len(server.authorizations)
    assert mirror.refresh(remote, ["main"]) is False
    assert len(server.authorizations) == requests
    assert mirror.resolve(third) is None
    eager = GitMirror(mirror.path, fetch_interval=0)
    assert eager.refresh(remote, ["main"]) is True
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
    assert mirror.refresh(remote, ["main"], force=True) is False
    assert mirror.clone(remote) is True


@needs_git
def test_the_token_is_never_sent_to_a_redirect_target(published, mirror):
    _repo, target = published
    with redirect_to(target.url) as redirecting:
        with pytest.raises(GitError):
            mirror.clone(lambda: Remote(redirecting.url + "/team/repo.git", TOKEN))
    assert redirecting.authorizations == [f"Bearer {TOKEN}"]
    assert target.authorizations == []
    assert not mirror.exists()


def test_deleting_a_mirror_that_was_never_cloned_creates_nothing(tmp_path, mirror):
    mirror.delete()
    assert not (tmp_path / "mirrors").exists()


def test_git_is_killed_at_its_timeout(tmp_path, mirror):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/stalled.git"
    started = time.monotonic()
    with pytest.raises(GitError, match="timed out"):
        GitMirror._git(None, "ls-remote", url, remote=Remote(url, TOKEN), timeout=1)
    assert time.monotonic() - started < 5
    listener.close()


def test_missing_git_binary_is_a_git_error(mirror, monkeypatch):
    monkeypatch.setenv("PATH", str(mirror.path))
    with pytest.raises(GitError, match="not installed"):
        mirror.clone(lambda: Remote("http://127.0.0.1:9/x.git", TOKEN))


@needs_git
def test_a_stale_mirror_fetches_within_the_fetch_interval(published, remote, tmp_path):
    repo, _server = published
    mirror = GitMirror(mirror_path(tmp_path / "mirrors", 7, 41), fetch_interval=3600)
    mirror.clone(remote)
    third = repo.commit("Add d", {"app.py": "a\nB\nc\nd\n"}, author=ALICE, when=SECOND)
    mirror.mark_stale()
    assert mirror.refresh(remote, ["main"]) is True
    assert mirror.resolve("main") == third
    assert mirror.refresh(remote, ["main"]) is False


def refs(mirror):
    return set(git(mirror.path, "for-each-ref", "--format=%(refname)").splitlines())


def has_object(mirror, sha):
    return (
        subprocess.run(
            ["git", "--git-dir", str(mirror.path), "cat-file", "-e", sha], capture_output=True
        ).returncode
        == 0
    )


@pytest.fixture
def branched(published):
    repo, _server = published
    main = repo.commits["Fix b"]
    feature = repo.commit("Feature", {"feature.py": "x\n"}, author=ALICE, when=SECOND)
    repo.branch("feature", feature)
    repo.branch("main", main)
    repo.tag("v1", repo.commits["Add app"], annotated=True)
    repo.tag("v2", main)
    return repo, main, feature


@needs_git
def test_the_mirror_holds_only_the_default_branch_and_the_names_blame_asked_for(
    branched, remote, mirror
):
    repo, main, feature = branched
    assert mirror.clone(remote, ["v1"])
    assert refs(mirror) == {"refs/heads/main", "refs/tags/v1"}
    assert not has_object(mirror, feature)
    assert mirror.resolve("main") == main
    assert mirror.resolve("v1") == repo.commits["Add app"]
    assert mirror.refresh(remote, ["feature"], force=True)
    assert refs(mirror) == {"refs/heads/main", "refs/tags/v1", "refs/heads/feature"}
    assert mirror.resolve("feature") == feature
    assert mirror.sync(remote, ["v2"], timeout=30)
    assert refs(mirror) == {"refs/heads/main", "refs/tags/v2"}


@needs_git
def test_a_tracked_ref_follows_the_remote_and_disappears_with_it(branched, remote, mirror):
    repo, main, _feature = branched
    mirror.clone(remote, ["feature"])
    moved = repo.commit("Feature 2", {"feature.py": "y\n"}, author=BOB, when=SECOND)
    repo.branch("feature", moved)
    repo.branch("main", main)
    assert mirror.refresh(remote, ["main"], force=True)
    assert mirror.resolve("feature") == moved
    git(repo.served, "update-ref", "-d", "refs/heads/feature")
    assert mirror.refresh(remote, ["main"], force=True)
    assert refs(mirror) == {"refs/heads/main"}


@needs_git
def test_a_commit_named_by_sha_is_fetched_on_demand_and_kept(branched, remote, mirror):
    repo, main, feature = branched
    git(repo.served, "update-ref", "-d", "refs/heads/feature")
    git(repo.served, "update-ref", "refs/keep/feature", feature)
    mirror.clone(remote)
    assert not mirror.has_commit(feature)
    assert mirror.refresh(remote, [feature], force=True)
    assert mirror.has_commit(feature)
    assert mirror.blame(feature, "feature.py", [1])[1].summary == "Feature"
    assert mirror.sync(remote, [feature], timeout=30)
    assert refs(mirror) == {"refs/heads/main", f"refs/commits/{feature}"}
    assert mirror.has_commit(main)


@needs_git
def test_fetches_never_run_gc_and_maintenance_does(published, remote, mirror):
    repo, _server = published
    mirror.clone(remote)
    # Keep every fetch as a pack and ask for gc at the second pack, so a gc is observable.
    git(mirror.path, "config", "fetch.unpackLimit", "1")
    git(mirror.path, "config", "gc.autoPackLimit", "1")
    for line in "de":
        repo.commit(f"Add {line}", {"app.py": f"a\nB\nc\n{line}\n"}, author=ALICE, when=SECOND)
        assert mirror.refresh(remote, ["main"], force=True)
    packs = mirror.path / "objects" / "pack"
    assert len(list(packs.glob("*.pack"))) == 2
    mirror.maintain()
    assert len(list(packs.glob("*.pack"))) == 1
