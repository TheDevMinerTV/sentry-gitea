"""Real git repositories served over git's dumb HTTP protocol, for mirror and blame tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ALICE = ("Alice Example", "alice@example.com")
BOB = ("Bob Example", "bob@example.com")
GIT_AVAILABLE = shutil.which("git") is not None


def git(cwd: Path, *args: str, author: tuple[str, str] = ALICE, date: str = "") -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": author[0],
        "GIT_COMMITTER_EMAIL": author[1],
    }
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class SourceRepo:
    """A working repository plus the bare copy the HTTP server publishes as team/repo.git."""

    work: Path
    served: Path
    commits: dict[str, str] = field(default_factory=dict)

    def commit(
        self, name: str, files: dict[str, str], *, author: tuple[str, str], when: datetime
    ) -> str:
        for path, text in files.items():
            (self.work / path).parent.mkdir(parents=True, exist_ok=True)
            (self.work / path).write_text(text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", name, author=author, date=when.isoformat())
        self.commits[name] = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-q", "--force", str(self.served), "HEAD:refs/heads/main")
        git(self.served, "update-server-info")
        return self.commits[name]


def source_repo(root: Path, first: datetime, second: datetime) -> SourceRepo:
    """`app.py` with line 2 from Bob's "Fix b" and lines 1 and 3 from Alice's "Add app"."""
    work, served = root / "work", root / "served" / "team" / "repo.git"
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    git(root, "init", "-q", "--bare", str(served))
    repo = SourceRepo(work, served)
    repo.commit("Add app", {"app.py": "a\nb\nc\n"}, author=ALICE, when=first)
    repo.commit("Fix b", {"app.py": "a\nB\nc\n"}, author=BOB, when=second)
    return repo


@dataclass
class GitHttpServer:
    url: str
    authorizations: list[str]


@contextmanager
def serve(directory: Path) -> Iterator[GitHttpServer]:
    authorizations: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            authorizations.append(self.headers.get("Authorization", ""))
            super().do_GET()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield GitHttpServer(f"http://127.0.0.1:{server.server_port}", authorizations)
    finally:
        server.shutdown()
        server.server_close()
