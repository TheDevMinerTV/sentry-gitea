"""Suspect commits from `git blame` on local mirrors (opt-in: GITEA_BLAME_ENABLED)."""

from __future__ import annotations

import functools
import logging
import time
from collections import defaultdict
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from django.conf import settings
from sentry.integrations.source_code_management.commit_context import (
    CommitInfo,
    FileBlameInfo,
    SourceLineInfo,
)
from sentry.shared_integrations.exceptions import ApiError, IntegrationError

from sentry_gitea.mirror import CLONE_TIMEOUT, GitError, GitMirror, Remote, mirror_path

from .identity import log_failure

logger = logging.getLogger(__name__)
DEFAULT_MIRROR_DIR = "/data/sentry-gitea/mirrors"
BLAME_FAILED = "sentry-gitea: blame failed"
# Leaves room for one last git call inside Sentry's 90 s commit-context deadline.
BLAME_BUDGET = 45


def blame_enabled() -> bool:
    return bool(getattr(settings, "GITEA_BLAME_ENABLED", False))


def repository_mirror(integration_id: int, repo_id: int) -> GitMirror:
    root = Path(getattr(settings, "GITEA_BLAME_MIRROR_DIR", None) or DEFAULT_MIRROR_DIR)
    return GitMirror(mirror_path(root, integration_id, repo_id))


def stable_repo_id(repo: Any) -> int | None:
    """The Gitea repository ID, which survives renames (and Sentry's pending-delete rename)."""
    value = str((repo.config or {}).get("repo_id") or "")
    return int(value) if value.isdigit() else None


def mark_mirror_stale(repo: Any) -> None:
    """After a push, a branch blame must fetch instead of reading the branch's old head.

    Also with blame off: a mirror left from before must not serve old heads once it is back on.
    """
    repo_id = stable_repo_id(repo)
    if repo_id is None or repo.integration_id is None:
        return
    try:
        repository_mirror(repo.integration_id, repo_id).mark_stale()
    except OSError as error:
        # An unreadable mirror root must not fail the push webhook, least of all with blame off.
        log_failure(logger, BLAME_FAILED, error, operation="mark_stale", repository_id=repo.id)


def remote_for(installation: Any, repo_id: int) -> Remote:
    client = installation.get_client()
    current = client.get_scm_repo(repo_id=repo_id)
    return Remote(f"{client.base_url}/{current['full_name']}.git", client.access_token())


def sync_mirror(installation: Any, repo: Any, refs: Collection[str]) -> None:
    """Clone the mirror, or else fetch into it now, with a clone's time budget, keeping only the
    default branch and `refs`; then run the git maintenance that fetches never run."""
    repo_id = stable_repo_id(repo)
    if repo_id is None:
        return
    mirror = repository_mirror(installation.model.id, repo_id)
    remote = functools.partial(remote_for, installation, repo_id)
    if not mirror.clone(remote, refs):
        mirror.sync(remote, refs, timeout=CLONE_TIMEOUT)
    mirror.maintain()


def _queue_sync(repo: Any) -> None:
    from .tasks import mirror_repository

    mirror_repository.delay(repository_id=repo.id)


def blame_for_files(installation: Any, files: Sequence[SourceLineInfo]) -> list[FileBlameInfo]:
    by_repo: dict[int, list[SourceLineInfo]] = defaultdict(list)
    for file in files:
        by_repo[file.repo.id].append(file)
    blamed: list[FileBlameInfo] = []
    deadline = time.monotonic() + BLAME_BUDGET
    for repo_files in by_repo.values():
        repo = repo_files[0].repo
        try:
            blamed += _blame_repository(installation, repo, repo_files, deadline)
        except (GitError, ApiError, IntegrationError, OSError) as error:
            log_failure(
                logger,
                BLAME_FAILED,
                error,
                integration_id=installation.model.id,
                repository_id=repo.id,
            )
    return blamed


def _blame_repository(
    installation: Any, repo: Any, files: Sequence[SourceLineInfo], deadline: float
) -> list[FileBlameInfo]:
    repo_id = stable_repo_id(repo)
    if repo_id is None:
        return []
    mirror = repository_mirror(installation.model.id, repo_id)
    if not mirror.exists():
        # Clones can outlast the commit-context task's deadline; they get a task of their own.
        _queue_sync(repo)
        if not mirror.exists():
            logger.info(
                "sentry-gitea: blame skipped, mirror not ready",
                extra={"integration_id": installation.model.id, "repository_id": repo.id},
            )
            return []
    by_ref: dict[str, list[SourceLineInfo]] = defaultdict(list)
    for file in files:
        by_ref[file.ref].append(file)
    unknown = [ref for ref in by_ref if not mirror.has_commit(ref)]
    if unknown and time.monotonic() < deadline:
        try:
            mirror.refresh(lambda: remote_for(installation, repo_id), unknown)
        except GitError:
            _queue_sync(repo)  # A slow fetch gets the task's longer budget.
            raise
    blamed: list[FileBlameInfo] = []
    for ref, ref_files in by_ref.items():
        sha = mirror.resolve(ref)
        if sha is None:
            continue
        by_path: dict[str, list[SourceLineInfo]] = defaultdict(list)
        for file in ref_files:
            by_path[file.path].append(file)
        for path, path_files in by_path.items():
            if time.monotonic() >= deadline:
                logger.info(
                    "sentry-gitea: blame budget exhausted",
                    extra={"integration_id": installation.model.id, "repository_id": repo.id},
                )
                return blamed
            lines = mirror.blame(sha, path, [f.lineno for f in path_files if f.lineno])
            blamed += [
                FileBlameInfo(
                    lineno=file.lineno,
                    path=file.path,
                    ref=file.ref,
                    repo=file.repo,
                    code_mapping=file.code_mapping,
                    commit=CommitInfo(
                        commitId=commit.sha,
                        committedDate=commit.committed_at,
                        commitMessage=commit.summary,
                        commitAuthorName=commit.author_name,
                        commitAuthorEmail=commit.author_email,
                    ),
                )
                for file in path_files
                if file.lineno and (commit := lines.get(file.lineno))
            ]
    return blamed
