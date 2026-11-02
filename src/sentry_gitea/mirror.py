"""Bare git mirrors and `git blame`, for Gitea's missing blame API. No Sentry imports.

Layout under the mirror root: `<integration id>/<gitea repository id>.git` (the mirror), a sibling
`.lock` file serializing clone, fetch and delete across processes, and `.<name>.tmp-*` clones in
progress, which become the mirror only through an atomic rename.
"""

from __future__ import annotations

import fcntl
import math
import os
import re
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
# Written by the first sync that pruned the mirror to the refs blame reads. Mirrors from earlier
# versions, which hold every branch and tag, lack it.
SYNCED_STAMP = "sentry-gitea-synced"


class GitError(Exception):
    pass


class MirrorBusy(GitError):
    pass


class BudgetExhausted(GitError):
    pass


@dataclass(frozen=True)
class Deadline:
    """A point on the monotonic clock that no git command, lock wait or API call may pass."""

    at: float

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        return cls(time.monotonic() + seconds)

    def within(self, seconds: float) -> Deadline:
        return Deadline(min(self.at, time.monotonic() + seconds))

    def cap(self, seconds: float) -> float:
        """`seconds`, or the time left if that is less; raises once no time is left."""
        left = self.at - time.monotonic()
        if left <= 0:
            raise BudgetExhausted("The time budget is exhausted.")
        return min(seconds, left)


UNBOUNDED = Deadline(math.inf)


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


_REF_NAME = re.compile(r"(?!-)(?!.*\.\.)(?!.*@\{)(?!.*//)[^\x00-\x20\x7f~^:?*\[\\]+(?<![/.])")


def _is_ref_name(name: str) -> bool:
    """A short branch or tag name git accepts; also keeps for-each-ref glob characters out."""
    return bool(_REF_NAME.fullmatch(name)) and not name.endswith(".lock") and name != "@"


class GitMirror:
    def __init__(
        self, path: Path, *, fetch_interval: float = FETCH_INTERVAL, deadline: Deadline = UNBOUNDED
    ) -> None:
        self.path = path
        self.fetch_interval = fetch_interval
        self.deadline = deadline
        self._lock_path = path.with_suffix(".lock")

    def exists(self) -> bool:
        return self.path.is_dir()

    def clone(self, remote: Callable[[], Remote], names: Collection[str] = ()) -> bool:
        """Create the mirror, with the remote's default branch and `names`, unless it exists or
        another process is creating it.

        Returns whether this call created it.
        """
        try:
            with self._locked(timeout=0):
                return self._clone(remote, names)
        except MirrorBusy:
            return False

    def _clone(self, remote: Callable[[], Remote], names: Collection[str]) -> bool:
        if self.exists():
            return False
        self._remove_partial_clones()
        partial = self.path.with_name(f".{self.path.name}.tmp-{secrets.token_hex(4)}")
        try:
            init = self._git(None, "init", "--quiet", "--bare", "--template=", str(partial))
            if init.returncode != 0:
                raise GitError("git init failed.")
            self._update(partial, remote(), names, CLONE_TIMEOUT, prune=True)
            self._stamp(partial)
            os.rename(partial, self.path)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
        return True

    def refresh(
        self,
        remote: Callable[[], Remote],
        names: Collection[str],
        *,
        timeout: float = FETCH_TIMEOUT,
        force: bool = False,
    ) -> bool:
        """Fetch `names` and every branch and tag the mirror already has, at most once per fetch
        interval unless forced.

        Returns whether it fetched; a fetch already running in another process counts as no.
        """
        return self._refresh(remote, names, timeout, force=force, prune=False)

    def sync(self, remote: Callable[[], Remote], names: Collection[str], *, timeout: float) -> bool:
        """Fetch now, and keep exactly the remote's default branch and `names`."""
        return self._refresh(remote, names, timeout, force=True, prune=True)

    def maintain(self) -> None:
        """`git gc --auto`, which fetches never run: it could outlast their deadline."""
        with self._locked():
            if self.exists():
                result = self._git(self.path, "gc", "--auto", "--quiet", timeout=CLONE_TIMEOUT)
                if result.returncode != 0:
                    raise GitError(f"git gc failed ({result.returncode}).")

    def _refresh(
        self,
        remote: Callable[[], Remote],
        names: Collection[str],
        timeout: float,
        *,
        force: bool,
        prune: bool,
    ) -> bool:
        if not force and not self._fetch_due():
            return False
        try:
            with self._locked():
                if not self.exists() or not (force or self._fetch_due()):
                    return False
                # Stamped before fetching, so an unreachable Gitea is retried once per interval.
                self._stamp(self.path)
                self._update(self.path, remote(), names, timeout, prune=prune)
                return True
        except MirrorBusy:
            return False

    def needs_sync(self) -> bool:
        """Cloned by an earlier version, with every branch and tag, and never pruned since."""
        return self.exists() and not (self.path / SYNCED_STAMP).exists()

    def mark_stale(self) -> None:
        """Make the next refresh fetch, however recent the last fetch was."""
        (self.path / FETCHED_STAMP).unlink(missing_ok=True)

    def resolve(self, ref: str) -> str | None:
        """The commit `ref` names in the mirror: a full SHA as is, else branch `ref`, else tag
        `ref`. Never git's own lookup order, which prefers a tag over a same-named branch."""
        if _is_full_sha(ref):
            return ref if self._peel(ref) == ref else None
        if not _is_ref_name(ref):
            return None
        candidates = (f"refs/heads/{ref}", f"refs/tags/{ref}")
        listed = self._git(self.path, "for-each-ref", "--format=%(refname)", *candidates)
        existing = set(listed.stdout.splitlines()) if listed.returncode == 0 else set()
        for qualified in candidates:
            if qualified in existing:
                return self._peel(qualified)
        return None

    def _peel(self, revision: str) -> str | None:
        result = self._git(
            self.path,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{revision}^{{commit}}",
        )
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

    def _update(
        self,
        git_dir: Path,
        remote: Remote,
        names: Collection[str],
        timeout: float,
        *,
        prune: bool,
    ) -> None:
        """Fetch only what blame reads: the remote's default branch, the branch and tag of each
        name, the branches and tags the mirror already tracks (unless pruning, or before its
        first sync) and commits named by SHA that the mirror lacks. Refs the remote no longer
        has, or that pruning does not keep, are deleted first: a deleted `x` must not block a
        new `x/y`."""
        network = self.deadline.within(timeout)
        local = self._local_refs(git_dir)
        shas = {name for name in names if _is_full_sha(name)}
        requested = {
            qualified
            for name in names
            if _is_ref_name(name) and not _is_full_sha(name)
            for qualified in (f"refs/heads/{name}", f"refs/tags/{name}")
        }
        tracking = not prune and (git_dir / SYNCED_STAMP).exists()
        tracked = requested | {r for r in local if _is_named_ref(r)} if tracking else requested
        advertised, head = self._ls_remote(git_dir, remote, ["HEAD", *sorted(tracked)], network)
        wanted = {ref for ref in tracked if ref in advertised}
        if head is not None and "HEAD" in advertised:
            wanted.add(head)
        missing = sorted(sha for sha in shas if not self._has_object(git_dir, sha))
        keep = wanted | {f"refs/commits/{sha}" for sha in shas}
        if prune:
            stale = local - keep
        else:
            stale = {ref for ref in tracked if ref in local and ref not in advertised}
        self._delete_refs(git_dir, stale)
        refspecs = [f"+{ref}:{ref}" for ref in sorted(wanted)]
        refspecs += [f"+{sha}:refs/commits/{sha}" for sha in missing]
        if refspecs:
            self._fetch(git_dir, remote, refspecs, network)
        if prune:
            (git_dir / SYNCED_STAMP).touch()

    def _ls_remote(
        self, git_dir: Path, remote: Remote, patterns: list[str], deadline: Deadline
    ) -> tuple[dict[str, str], str | None]:
        """Advertised refs among `patterns` (exact names only), and the branch HEAD points to."""
        result = self._git(
            git_dir,
            "ls-remote",
            "--symref",
            remote.url,
            *patterns,
            remote=remote,
            timeout=deadline.cap(CLONE_TIMEOUT),
        )
        _raise_for_network(result, "ls-remote", remote)
        advertised: dict[str, str] = {}
        head = None
        wanted = set(patterns)
        for line in result.stdout.splitlines():
            value, _, ref = line.partition("\t")
            target = value.removeprefix("ref: ")
            if (
                ref == "HEAD"
                and target != value
                and _is_ref_name(target.removeprefix("refs/heads/"))
            ):
                head = target if target.startswith("refs/heads/") else None
            elif ref in wanted and _is_full_sha(value):
                advertised[ref] = value
        return advertised, head

    def _fetch(
        self, git_dir: Path, remote: Remote, refspecs: list[str], deadline: Deadline
    ) -> None:
        result = self._git(
            git_dir,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            remote.url,
            *refspecs,
            # Auto gc inside a fetch could outlast the fetch's deadline; maintain() runs it.
            config=("gc.auto=0", "maintenance.auto=false"),
            remote=remote,
            timeout=deadline.cap(CLONE_TIMEOUT),
        )
        _raise_for_network(result, "fetch", remote)

    def _local_refs(self, git_dir: Path) -> set[str]:
        result = self._git(git_dir, "for-each-ref", "--format=%(refname)")
        if result.returncode != 0:
            raise GitError("git for-each-ref failed.")
        return set(result.stdout.splitlines())

    def _has_object(self, git_dir: Path, sha: str) -> bool:
        return self._git(git_dir, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0

    def _delete_refs(self, git_dir: Path, refs: Collection[str]) -> None:
        if not refs:
            return
        commands = "".join(f"delete {ref}\n" for ref in sorted(refs))
        result = self._git(git_dir, "update-ref", "--stdin", stdin=commands)
        if result.returncode != 0:
            raise GitError("git update-ref failed.")

    @contextmanager
    def _locked(self, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
        wait = Deadline.after(self.deadline.cap(timeout))
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= wait.at:
                        raise MirrorBusy("Timed out waiting for the mirror lock.") from None
                    time.sleep(min(0.2, max(wait.at - time.monotonic(), 0.0)))
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _git(
        self,
        git_dir: Path | None,
        *args: str,
        config: Collection[str] = (),
        stdin: str | None = None,
        remote: Remote | None = None,
        timeout: float = LOCAL_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        timeout = self.deadline.cap(timeout)
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
        argv += [arg for setting in config for arg in ("-c", setting)]
        process = _spawn([*argv, *args], remote, stdin is not None)
        try:
            stdout, stderr = process.communicate(
                None if stdin is None else stdin.encode(), timeout=timeout
            )
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


def _raise_for_network(
    result: subprocess.CompletedProcess[str], command: str, remote: Remote
) -> None:
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or [""])[-1].replace(remote.token, "***")
        raise GitError(f"git {command} failed ({result.returncode}): {detail[:200]}")


def _is_named_ref(ref: str) -> bool:
    return ref.startswith(("refs/heads/", "refs/tags/"))


def _spawn(argv: list[str], remote: Remote | None, with_stdin: bool) -> subprocess.Popen[bytes]:
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
            stdin=subprocess.PIPE if with_stdin else subprocess.DEVNULL,
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
