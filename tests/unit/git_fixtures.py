"""Real git repositories served over git's smart HTTP protocol, for mirror and blame tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        return self.commits[name]

    def tag(self, name: str, commit: str, *, annotated: bool = False) -> None:
        git(self.work, "tag", *(["-a", "-m", name] if annotated else []), name, commit)
        git(self.work, "push", "-q", str(self.served), f"refs/tags/{name}")

    def branch(self, name: str, commit: str) -> None:
        git(self.work, "push", "-q", "--force", str(self.served), f"{commit}:refs/heads/{name}")


def source_repo(root: Path, first: datetime, second: datetime) -> SourceRepo:
    """`app.py` with line 2 from Bob's "Fix b" and lines 1 and 3 from Alice's "Add app"."""
    work, served = root / "work", root / "served" / "team" / "repo.git"
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    git(root, "init", "-q", "--bare", "-b", "main", str(served))
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
    """git's smart HTTP protocol through `git http-backend`, as Gitea speaks it."""
    authorizations: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._backend()

        def do_POST(self) -> None:
            self._backend()

        def _backend(self) -> None:
            authorizations.append(self.headers.get("Authorization", ""))
            path, _, query = self.path.partition("?")
            body = self._body()
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_PROJECT_ROOT": str(directory),
                "GIT_HTTP_EXPORT_ALL": "1",
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                "REQUEST_METHOD": self.command,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "CONTENT_LENGTH": str(len(body)),
                "REMOTE_ADDR": "127.0.0.1",
            }
            output = subprocess.run(
                ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
            ).stdout
            head, _, payload = output.partition(b"\r\n\r\n")
            headers = [line.split(": ", 1) for line in head.decode().split("\r\n") if line]
            status = next((int(v.split()[0]) for k, v in headers if k == "Status"), 200)
            self.send_response(status)
            for key, value in headers:
                if key != "Status":
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() != "chunked":
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))
            chunks = []
            while size := int(self.rfile.readline().split(b";")[0], 16):
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            self.rfile.readline()
            return b"".join(chunks)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield GitHttpServer(f"http://127.0.0.1:{server.server_port}", authorizations)
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def redirect_to(target: str) -> Iterator[GitHttpServer]:
    """Answers every request with a 301 to the same path on `target`."""
    authorizations: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            authorizations.append(self.headers.get("Authorization", ""))
            self.send_response(301)
            self.send_header("Location", target + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield GitHttpServer(f"http://127.0.0.1:{server.server_port}", authorizations)
    finally:
        server.shutdown()
        server.server_close()
