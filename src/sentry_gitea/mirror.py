"""Bare git mirrors and `git blame`, for Gitea's missing blame API. No Sentry imports.

Layout under the mirror root: `<integration id>/<gitea repository id>.git` (the mirror), a sibling
`.lock` file serializing clone, fetch and delete across processes, and `.<name>.tmp-*` clones in
progress, which become the mirror only through an atomic rename.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

FETCH_INTERVAL = 300
# Local reads and fetches must fit Sentry's 90 s commit-context deadline; clones run in a task.
LOCAL_TIMEOUT = 10
FETCH_TIMEOUT = 30
CLONE_TIMEOUT = 1500
LOCK_TIMEOUT = 10
FETCHED_STAMP = "sentry-gitea-fetched"
REFSPECS = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")


class GitError(Exception):
    pass


class MirrorBusy(GitError):
    pass


@dataclass(frozen=True)
class Remote:
    url: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class BlameCommit:
    sha: str
    author_name: str
    author_email: str
    committed_at: datetime
    summary: str


def parse_porcelain(output: str) -> dict[int, BlameCommit]:
    """`git blame --line-porcelain` output, keyed by final line number."""
    blamed: dict[int, BlameCommit] = {}
    header: tuple[str, int] | None = None
    fields: dict[str, str] = {}
    for line in output.split("\n"):
        if line.startswith("\t"):
            if header is not None:
                sha, lineno = header
                blamed[lineno] = BlameCommit(
                    sha=sha,
                    author_name=fields.get("author", ""),
                    author_email=fields.get("author-mail", "").removeprefix("<").removesuffix(">"),
                    committed_at=datetime.fromtimestamp(int(fields["committer-time"]), UTC),
                    summary=fields.get("summary", ""),
                )
            header, fields = None, {}
        elif header is None:
            if line:
                sha, _original, final = line.split(" ")[:3]
                header = (sha, int(final))
        else:
            key, _, value = line.partition(" ")
            fields[key] = value
    return blamed


def mirror_path(root: Path, integration_id: int, repo_id: int) -> Path:
    return root / str(int(integration_id)) / f"{int(repo_id)}.git"


def _is_full_sha(ref: str) -> bool:
    return len(ref) in (40, 64) and all(c in "0123456789abcdef" for c in ref)


class GitMirror:
    def __init__(self, path: Path, *, fetch_interval: float = FETCH_INTERVAL) -> None:
        self.path = path
        self.fetch_interval = fetch_interval
        self._lock_path = path.with_suffix(".lock")

    def exists(self) -> bool:
        return self.path.is_dir()

    def clone(self, remote: Callable[[], Remote]) -> bool:
        """Create the mirror unless it exists or another process is creating it.

        Returns whether this call created it.
        """
        try:
            with self._locked(timeout=0):
                return self._clone(remote)
        except MirrorBusy:
            return False

    def _clone(self, remote: Callable[[], Remote]) -> bool:
        if self.exists():
            return False
        self._remove_partial_clones()
        partial = self.path.with_name(f".{self.path.name}.tmp-{secrets.token_hex(4)}")
        try:
            init = self._git(None, "init", "--quiet", "--bare", "--template=", str(partial))
            if init.returncode != 0:
                raise GitError("git init failed.")
            self._fetch(partial, remote(), CLONE_TIMEOUT)
            self._stamp(partial)
            os.rename(partial, self.path)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
        return True

    def refresh(
        self, remote: Callable[[], Remote], *, timeout: float = FETCH_TIMEOUT, force: bool = False
    ) -> bool:
        """Fetch all branches and tags, at most once per fetch interval unless forced.

        Returns whether it fetched; a fetch already running in another process counts as no.
        """
        if not force and not self._fetch_due():
            return False
        try:
            with self._locked():
                if not self.exists() or not (force or self._fetch_due()):
                    return False
                # Stamped before fetching, so an unreachable Gitea is retried once per interval.
                self._stamp(self.path)
                self._fetch(self.path, remote(), timeout)
                return True
        except MirrorBusy:
            return False

    def mark_stale(self) -> None:
        """Make the next refresh fetch, however recent the last fetch was."""
        (self.path / FETCHED_STAMP).unlink(missing_ok=True)

    def resolve(self, ref: str) -> str | None:
        """The commit `ref` names in the mirror, or None."""
        if not ref or ref.startswith("-") or "\n" in ref:
            return None
        result = self._git(self.path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def has_commit(self, ref: str) -> bool:
        return _is_full_sha(ref) and self.resolve(ref) == ref

    def blame(self, sha: str, path: str, lines: Collection[int]) -> dict[int, BlameCommit]:
        """Blame `lines` of `path` at commit `sha`; missing files and lines are left out."""
        wanted = sorted({line for line in lines if line > 0})
        if not wanted or not _is_full_sha(sha):
            return {}
        ranges = [arg for line in wanted for arg in ("-L", f"{line},{line}")]
        result = self._git(self.path, "blame", "--line-porcelain", *ranges, sha, "--", path)
        if result.returncode == 0:
            return parse_porcelain(result.stdout)
        if len(wanted) == 1:
            return {}
        # git rejects the whole batch when one line is past the end of the file.
        return {
            line: commit
            for single in wanted
            for line, commit in self.blame(sha, path, [single]).items()
        }

    def delete(self) -> None:
        """Remove the mirror and partial clones; creates nothing when there is neither."""
        if not self.exists() and not self._partial_clones():
            return
        # The lock file stays: unlinking it would let a waiting process and a new one both lock.
        with self._locked(timeout=LOCK_TIMEOUT * 3):
            shutil.rmtree(self.path, ignore_errors=True)
            self._remove_partial_clones()

    def _fetch_due(self) -> bool:
        try:
            fetched = (self.path / FETCHED_STAMP).stat().st_mtime
        except FileNotFoundError:
            return True
        return time.time() - fetched >= self.fetch_interval

    @staticmethod
    def _stamp(git_dir: Path) -> None:
        (git_dir / FETCHED_STAMP).touch()

    def _partial_clones(self) -> list[Path]:
        return list(self.path.parent.glob(f".{self.path.name}.tmp-*"))

    def _remove_partial_clones(self) -> None:
        for partial in self._partial_clones():
            shutil.rmtree(partial, ignore_errors=True)

    def _fetch(self, git_dir: Path, remote: Remote, timeout: float) -> None:
        result = self._git(
            git_dir,
            "fetch",
            "--quiet",
            "--prune",
            "--no-write-fetch-head",
            remote.url,
            *REFSPECS,
            remote=remote,
            timeout=timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or [""])[-1].replace(remote.token, "***")
            raise GitError(f"git fetch failed ({result.returncode}): {detail[:200]}")

    @contextmanager
    def _locked(self, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a") as handle:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise MirrorBusy("Timed out waiting for the mirror lock.") from None
                    time.sleep(0.2)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _git(
        git_dir: Path | None,
        *args: str,
        remote: Remote | None = None,
        timeout: float = LOCAL_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "gc.autoDetach=false",
            # git sends http.extraHeader, the token, to wherever a redirect points.
            "-c",
            "http.followRedirects=false",
        ]
        if git_dir is not None:
            argv += ["--git-dir", str(git_dir)]
        process = _spawn([*argv, *args], remote)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(process)
            raise GitError(f"git {args[0]} timed out after {timeout:.0f} s.") from None
        except BaseException:
            # Such as the task's processing deadline: git must not outlive the lock it ran under.
            _kill(process)
            raise
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )


def _spawn(argv: list[str], remote: Remote | None) -> subprocess.Popen[bytes]:
    # Builds the token-bearing environment in a frame that is gone before git runs, so error
    # reports with local variables cannot capture it.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if remote is not None:
        # The token travels in the environment only: never in argv (visible in `ps`) or on disk.
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {remote.token}",
        }
    try:
        return subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise GitError("git is not installed in this image.") from None


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Kill git and its helpers, such as git-remote-https, which share its process group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.communicate()
