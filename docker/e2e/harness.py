"""Local development harness. All Sentry implementation stays in the host image."""

import argparse
import base64
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from redaction import Credential, exposure_message

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".e2e"
BEGIN = "# BEGIN sentry-gitea"
END = "# END sentry-gitea"
GITEA_PLUGIN_SCOPES = ("read:user", "read:repository", "write:issue")


def read_env(path: Path) -> dict[str, str]:
    """Read scalar env assignments without executing shell code."""
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key.strip()):
            raise ValueError(f"Invalid env assignment in {path.name}")
        parts = shlex.split(value, comments=True)
        if len(parts) > 1:
            raise ValueError(f"Quote values containing spaces in {path.name}")
        values[key.strip()] = parts[0] if parts else ""
    return values


def write_private(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            os.chmod(temporary_path, mode)
            temporary.write(content)
            temporary.flush()
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def write_env(path: Path, values: dict[str, str]) -> None:
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Env values must fit on a single line")
    content = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    write_private(path, content)


def marked_block(text: str, body: str | None) -> str:
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(starts) != len(ends) or len(starts) > 1 or (starts and starts[0] >= ends[0]):
        raise ValueError("Malformed sentry-gitea markers; repair them before rerunning")
    block = f"{BEGIN}\n{body.rstrip()}\n{END}\n" if body is not None else ""
    if starts:
        return "".join(lines[: starts[0]]) + block + "".join(lines[ends[0] + 1 :])
    if not block:
        return text
    return text + ("\n" if text and not text.endswith("\n") else "") + block


def run(
    command: list[str],
    *,
    label: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    capture: bool = True,
    timeout: int = 120,
) -> str:
    # In particular, never print captured CLI output containing generated tokens.
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            input=input_text,
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f"{label} could not run or timed out") from None
    if result.returncode:
        raise RuntimeError(f"{label} failed (exit {result.returncode}); command output withheld")
    return result.stdout if capture else ""


def self_hosted_dir() -> Path:
    return Path(
        os.environ.get("SELF_HOSTED_DIR", str(ROOT.parent / "self-hosted-26.3.1"))
    ).resolve()


def self_hosted_compose() -> list[str]:
    directory = self_hosted_dir()
    command = ["docker", "compose", "--project-directory", str(directory)]
    command += ["--env-file", str(directory / ".env")]
    if (directory / ".env.custom").exists():
        command += ["--env-file", str(directory / ".env.custom")]
    command += ["-f", str(directory / "docker-compose.yml")]
    override = directory / "docker-compose.override.yml"
    if override.exists():
        command += ["-f", str(override)]
    return command


def recent_plugin_logs(
    since: str, *, credentials: tuple[Credential, ...] = (), timeout: float = 10
) -> str:
    """Capture, never print, taskworker plugin messages since an RFC3339 timestamp.

    In 26.3.1 errors-only, post_process_group schedules workflow actions on
    taskworker, which runs legacy plugin notification callbacks. Both stdout and
    stderr from Docker are captured: container stderr must not bypass filtering.
    """
    command = self_hosted_compose() + [
        "logs",
        "--no-color",
        "--timestamps",
        "--since",
        since,
        "--tail",
        "2000",
        "taskworker",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Read taskworker logs failed or timed out; output withheld") from None
    if result.returncode:
        raise RuntimeError("Read taskworker logs failed; output withheld")
    if message := exposure_message(result.stdout, credentials, service="taskworker"):
        raise RuntimeError(message)
    return "\n".join(line for line in result.stdout.splitlines() if "sentry-gitea:" in line)


def compose_config() -> dict:
    return json.loads(
        run(
            self_hosted_compose() + ["config", "--format", "json"],
            label="Read Sentry Compose config",
        )
    )


def active_profiles() -> set[str]:
    directory = self_hosted_dir()
    values = read_env(directory / ".env") | read_env(directory / ".env.custom") | dict(os.environ)
    return {item.strip() for item in values.get("COMPOSE_PROFILES", "").split(",") if item.strip()}


def sentry_services(services: dict, profiles: set[str]) -> list[str]:
    return [
        name
        for name, config in services.items()
        if config.get("image") in {"sentry-self-hosted-local", "sentry-cleanup-self-hosted-local"}
        and (not config.get("profiles") or profiles.intersection(config["profiles"]))
    ]


def gitea_compose() -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "-p",
        os.environ.get("E2E_COMPOSE_PROJECT", "sentry-gitea-e2e"),
        "-f",
        str(ROOT / "docker/e2e/gitea.compose.yml"),
    ]


def checked_subnets(values: list[str]) -> str:
    """Exact non-public subnets for Gitea's [security] ALLOWED_HOST_LIST, comma-separated."""
    subnets = []
    for value in values:
        try:
            subnet = ipaddress.ip_network(value.strip(), strict=True)
        except ValueError:
            raise ValueError("Subnets must be exact CIDRs (host bits zero)") from None
        # Gitea already reaches public targets in lax mode; a public or catch-all entry
        # here could only widen what any Gitea user's webhooks can reach.
        if subnet.prefixlen == 0 or subnet.is_global:
            raise ValueError("Gitea webhook subnets must be private, not catch-all")
        subnets.append(str(subnet))
    if not subnets:
        raise ValueError("No subnet to allow for Gitea webhooks")
    return ",".join(dict.fromkeys(subnets))


def network_subnets(network: str) -> str:
    """The IPAM subnet(s) of Sentry's Docker network (where Gitea delivers webhooks)."""
    output = run(
        ["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", network],
        label="Inspect Sentry's Docker network",
    )
    try:
        configs = json.loads(output)
    except ValueError:
        raise RuntimeError("Docker network inspection did not return JSON") from None
    values = [
        config["Subnet"]
        for config in configs or []
        if isinstance(config, dict) and isinstance(config.get("Subnet"), str) and config["Subnet"]
    ]
    if not values:
        raise RuntimeError("Sentry's Docker network has no IPAM subnet; set SELF_HOSTED_SUBNETS")
    return checked_subnets(values)


def network_environment(*, subnets: bool = True) -> dict[str, str]:
    environment = dict(os.environ)
    if not environment.get("SELF_HOSTED_NETWORK"):
        environment["SELF_HOSTED_NETWORK"] = compose_config()["networks"]["default"]["name"]
    if environment.get("SELF_HOSTED_SUBNETS"):
        environment["SELF_HOSTED_SUBNETS"] = checked_subnets(
            environment["SELF_HOSTED_SUBNETS"].split(",")
        )
    elif subnets:
        environment["SELF_HOSTED_SUBNETS"] = network_subnets(environment["SELF_HOSTED_NETWORK"])
    else:
        # Only satisfies Compose interpolation for commands that never start Gitea (down).
        environment["SELF_HOSTED_SUBNETS"] = "unused"
    return environment


# Gitea's image copies GITEA__* variables into the persistent app.ini and never deletes
# keys, so a [webhook] ALLOWED_HOST_LIST written by an older harness (e.g. "*", which
# Gitea 28 drops) keeps overriding [security] ALLOWED_HOST_LIST. Remove only that key, in
# place (same owner/mode), and print nothing else: app.ini holds Gitea's secrets.
INI_SCRUBBED = "SENTRY_GITEA_E2E_INI_SCRUBBED"
INI_SCRUB = rf"""set -eu
ini=/data/gitea/conf/app.ini
[ -f "$ini" ] || exit 0
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
set +e
awk '
/^[[:space:]]*\[/ {{
    section = tolower($0)
    sub(/^[[:space:]]*\[[[:space:]]*/, "", section)
    sub(/[[:space:]]*\].*$/, "", section)
}}
section == "webhook" && toupper($0) ~ /^[[:space:]]*ALLOWED_HOST_LIST[[:space:]]*=/ {{
    found = 1
    next
}}
{{ print }}
END {{ exit found ? 3 : 0 }}
' "$ini" > "$tmp"
status=$?
set -e
case "$status" in
    0) ;;
    3) cat "$tmp" > "$ini"; echo {INI_SCRUBBED} ;;
    *) exit "$status" ;;
esac
"""


def gitea_up() -> None:
    environment = network_environment()
    compose = gitea_compose()
    up = compose + ["up", "-d", "--wait", "--wait-timeout", "180"]
    run(up, label="gitea-up", env=environment, capture=False, timeout=240)
    output = run(
        compose + ["exec", "-T", "--user", "git", "gitea", "sh", "-c", INI_SCRUB],
        label="Remove the deprecated Gitea [webhook] ALLOWED_HOST_LIST",
        env=environment,
    )
    if INI_SCRUBBED in output.split():
        print("Removed a stale [webhook] ALLOWED_HOST_LIST from Gitea's app.ini; restarting.")
        run(compose + ["restart", "gitea"], label="Restart Gitea", env=environment, capture=False)
        run(up, label="gitea-up", env=environment, capture=False, timeout=240)
    print(f"Gitea webhooks may reach non-public {environment['SELF_HOSTED_SUBNETS']} only.")


def check_compose() -> None:
    version = run(["docker", "compose", "version", "--short"], label="Check Compose version")
    numbers = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not numbers or tuple(map(int, numbers.groups())) < (2, 32, 2):
        raise RuntimeError("Docker Compose >= 2.32.2 is required; set DOCKER_CONFIG if needed")


def build_wheel(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    for previous in destination.glob("sentry_gitea-*.whl"):
        previous.unlink()
    run(
        [os.environ.get("UV", "uv"), "build", "--wheel", "--out-dir", str(destination)],
        label="Build sentry-gitea wheel",
        capture=False,
        timeout=300,
    )
    wheels = list(destination.glob("sentry_gitea-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one sentry-gitea wheel")
    return wheels[0]


def integration_settings(values: dict[str, str]) -> str:
    """Keep plugin-only installs possible; configure OAuth after Gitea bootstrap."""
    if not any(values.get(key) for key in ("GITEA_CLIENT_ID", "GITEA_CLIENT_SECRET")):
        return ""
    mapping = {
        "GITEA_BASE_URL": "GITEA_INTERNAL_URL",
        "GITEA_PUBLIC_URL": "GITEA_URL",
        "GITEA_CLIENT_ID": "GITEA_CLIENT_ID",
        "GITEA_CLIENT_SECRET": "GITEA_CLIENT_SECRET",
    }
    if any(not values.get(key) for key in mapping.values()):
        raise RuntimeError("Incomplete Gitea OAuth settings; run make e2e-bootstrap first")
    # repr produces Python literals, including for quotes/backslashes in secrets.
    return "\n".join(f"{setting} = {values[key]!r}" for setting, key in mapping.items())


# Gitea reaches Sentry's web service directly on self-hosted's Docker network.
WEBHOOK_URL_PREFIX = "http://web:9000"
TASKS_MODULE = "sentry_gitea.integration.tasks"
# Blame suspect commits run git inside Sentry's containers; the Sentry image has none.
INSTALL_GIT = (
    "apt-get update && apt-get install -y --no-install-recommends git "
    "&& rm -rf /var/lib/apt/lists/*"
)
# The plugins' base_url in the E2E is Gitea's internal origin, a private address that
# only an allowlisted origin may reach (every other base_url gets Sentry's SSRF guard).
PLUGIN_ALLOWED_BASE_URLS = ("http://gitea:3000",)


def sync_settings(snippet_body: str) -> str:
    """Non-secret harness settings, unless the shipped snippet already enables them.

    Webhook deliveries need a URL Gitea can reach, the push backfill task must be
    registered with Sentry's taskworker, the plugins must be allowed to call the
    internal Gitea origin, and blame suspect commits are on. Comment lines in the
    snippet do not count.
    """
    active = [line for line in snippet_body.splitlines() if not line.lstrip().startswith("#")]
    lines = []
    if not any(re.match(r"\s*GITEA_WEBHOOK_URL_PREFIX\s*=", line) for line in active):
        prefix = os.environ.get("GITEA_WEBHOOK_URL_PREFIX") or WEBHOOK_URL_PREFIX
        if not re.fullmatch(r"https?://[^\s/'\"\\]+", prefix.rstrip("/")):
            raise ValueError("GITEA_WEBHOOK_URL_PREFIX must be an http(s) origin")
        lines.append(f"GITEA_WEBHOOK_URL_PREFIX = {prefix.rstrip('/')!r}")
    if not any("TASKWORKER_IMPORTS" in line and TASKS_MODULE in line for line in active):
        lines.append(f"TASKWORKER_IMPORTS += ({TASKS_MODULE!r},)")
    if not any(re.match(r"\s*GITEA_PLUGIN_ALLOWED_BASE_URLS\s*=", line) for line in active):
        lines.append(f"GITEA_PLUGIN_ALLOWED_BASE_URLS = {PLUGIN_ALLOWED_BASE_URLS!r}")
    if not any(re.match(r"\s*GITEA_BLAME_ENABLED\s*=", line) for line in active):
        lines.append("GITEA_BLAME_ENABLED = True")
    return "\n".join(lines)


def install(uninstall: bool = False) -> None:
    compose = self_hosted_compose()
    config = compose_config()
    existing = set(
        run(compose + ["ps", "--services", "--all"], label="Find existing Sentry services").split()
    )
    running = set(
        run(
            compose + ["ps", "--services", "--status", "running"],
            label="Find running Sentry services",
        ).split()
    )
    services = [
        name for name in sentry_services(config["services"], active_profiles()) if name in existing
    ]
    if "web" not in services:
        raise RuntimeError("Create the self-hosted Sentry stack before installing this extension")
    directory = self_hosted_dir() / "sentry"
    enhance = directory / "enhance-image.sh"
    settings = directory / "sentry.conf.py"
    if not settings.exists():
        raise RuntimeError("Self-hosted sentry/sentry.conf.py is missing; finish install.sh first")
    original_enhance = (
        enhance.read_text() if enhance.exists() else "#!/usr/bin/env bash\nset -euo pipefail\n"
    )
    original_settings = settings.read_text()
    # Validate both files before a build or any writes.
    marked_block(original_enhance, None)
    marked_block(original_settings, None)
    install_body = settings_body = build_settings_body = None
    if not uninstall:
        oauth_settings = integration_settings(read_env(STATE / "gitea.env"))
        with tempfile.TemporaryDirectory(prefix="sentry-gitea-wheel-") as temporary:
            wheel = build_wheel(Path(temporary))
            shutil.copy2(wheel, directory / wheel.name)
            install_body = (
                f"{INSTALL_GIT}\n"
                "python -m pip install --no-deps --no-index --force-reinstall "
                + shlex.quote(f"/usr/src/sentry/{wheel.name}")
            )
        snippet = (ROOT / "docker/sentry.conf.py.snippet").read_text().splitlines()
        settings_body = build_settings_body = "\n".join(snippet[1:-1])
        # Non-secret, so they belong in the image build context and the runtime block.
        if extra := sync_settings(build_settings_body):
            settings_body = build_settings_body = build_settings_body + "\n" + extra
        if oauth_settings:
            settings_body += "\n" + oauth_settings
        else:
            print("OAuth is not bootstrapped; rerun e2e-install after make e2e-bootstrap.")
    write_private(
        enhance,
        marked_block(original_enhance, install_body),
        enhance.stat().st_mode | 0o100 if enhance.exists() else 0o755,
    )
    # Preserve readability for Sentry's uid 999 through the self-hosted bind mount.
    # Only the harness credential files have a mandatory mode of 0600.
    settings_mode = settings.stat().st_mode
    print("Rebuilding Sentry, then its cleanup image; this can take several minutes.", flush=True)
    # Self-hosted's Dockerfile copies this whole directory. Keep the managed OAuth
    # settings out of image layers, including on reinstall; the runtime bind mount
    # receives the full block before any service recreation (also restore on failure).
    # The cleanup image builds FROM the freshly built local Sentry image, which a
    # docker-container buildx builder cannot see; self-hosted's installer pins this too.
    build_environment = dict(os.environ, BUILDX_BUILDER="default")
    try:
        write_private(settings, marked_block(original_settings, build_settings_body), settings_mode)
        run(
            compose + ["build", "web"],
            label="Build Sentry image",
            env=build_environment,
            capture=False,
            timeout=1800,
        )
        run(
            compose + ["build", "sentry-cleanup"],
            label="Build cleanup image",
            env=build_environment,
            capture=False,
            timeout=1800,
        )
    finally:
        write_private(settings, marked_block(original_settings, settings_body), settings_mode)
    run(
        compose
        + [
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "300",
            *services,
        ],
        label="Recreate affected Sentry services",
        capture=False,
        timeout=360,
    )
    if "nginx" in running:
        run(compose + ["restart", "nginx"], label="Refresh nginx upstream", capture=False)
    print(
        "Extension removed."
        if uninstall
        else "Extension installed; run make e2e-test after bootstrap."
    )


class APIError(RuntimeError):
    def __init__(self, method: str, path: str, status: int) -> None:
        self.status = status
        super().__init__(f"{method} {path}: HTTP {status}")


class API:
    def __init__(self, url: str, token: str = "", scheme: str = "Bearer") -> None:
        self.url = url.rstrip("/")
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"{scheme} {token}"

    def request(self, method: str, path: str, *, expected=(200,), **kwargs):
        try:
            response = self.session.request(method, self.url + path, timeout=15, **kwargs)
        except requests.RequestException:
            raise RuntimeError(f"{method} {path} could not connect") from None
        if response.status_code not in expected:
            raise APIError(method, path, response.status_code)
        if response.status_code == 404 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise RuntimeError(f"{method} {path} did not return JSON") from None


def wait_for_gitea(url: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            response = requests.get(url.rstrip("/") + "/api/healthz", timeout=5)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError("Gitea was not healthy within 180 seconds")


def ensure_gitea_repo(api: API, owner: str, repo: str, username: str) -> None:
    user_owned = owner == username
    if not user_owned and api.request("GET", f"/api/v1/orgs/{owner}", expected=(200, 404)) is None:
        api.request(
            "POST",
            "/api/v1/orgs",
            expected=(201,),
            json={"username": owner, "visibility": "private"},
        )
    existing = api.request("GET", f"/api/v1/repos/{owner}/{repo}", expected=(200, 404))
    if existing is None:
        path = "/api/v1/user/repos" if user_owned else f"/api/v1/orgs/{owner}/repos"
        api.request(
            "POST",
            path,
            expected=(201,),
            json={"name": repo, "auto_init": True, "default_branch": "main", "private": True},
        )
    elif existing.get("empty"):
        api.request(
            "POST",
            f"/api/v1/repos/{owner}/{repo}/contents/README.md",
            expected=(201,),
            json={
                "content": base64.b64encode(b"# E2E demo\n").decode(),
                "message": "Initialize E2E demo",
            },
        )


# Read-only membership for the "member but not owner" org. Gitea 28 needs units_map;
# `units` + `permission` alone leaves the member with no read access.
MEMBER_TEAM = {
    "name": "e2e-members",
    "permission": "read",
    "units_map": {"repo.code": "read", "repo.issues": "read"},
    "includes_all_repositories": True,
}


def bootstrap_gitea() -> None:
    environment = network_environment()
    compose = gitea_compose()
    url = os.environ.get(
        "GITEA_URL", f"http://127.0.0.1:{os.environ.get('GITEA_HTTP_PORT', '13000')}"
    )
    wait_for_gitea(url)
    previous = read_env(STATE / "gitea.env")
    username = os.environ.get("GITEA_ADMIN_USERNAME", "e2e-admin")
    token = os.environ.get("GITEA_TOKEN", previous.get("GITEA_TOKEN", ""))
    if token:
        try:
            identity = API(url, token, "token").request("GET", "/api/v1/user")
            if identity["login"] != username or not identity["is_admin"]:
                raise RuntimeError(
                    "Gitea token must belong to GITEA_ADMIN_USERNAME and be an admin"
                )
        except APIError as error:
            # Invalid stored tokens can be recovered through the local admin CLI.
            if error.status != 401 or os.environ.get("GITEA_TOKEN"):
                raise
            token = ""
    if not token:
        users = run(
            compose
            + [
                "exec",
                "-T",
                "--user",
                "git",
                "gitea",
                "gitea",
                "--config",
                "/data/gitea/conf/app.ini",
                "admin",
                "user",
                "list",
            ],
            label="List Gitea users",
            env=environment,
        )
        exists = any(
            len(parts := line.split()) >= 2 and parts[1] == username for line in users.splitlines()
        )
        token_name = f"sentry-gitea-e2e-{secrets.token_hex(6)}"
        if exists:
            output = run(
                compose
                + [
                    "exec",
                    "-T",
                    "--user",
                    "git",
                    "gitea",
                    "gitea",
                    "--config",
                    "/data/gitea/conf/app.ini",
                    "admin",
                    "user",
                    "generate-access-token",
                    "--username",
                    username,
                    "--token-name",
                    token_name,
                    "--scopes",
                    "all",
                    "--raw",
                ],
                label="Generate Gitea token",
                env=environment,
            )
        else:
            environment["GITEA_E2E_PASSWORD"] = os.environ.get(
                "GITEA_ADMIN_PASSWORD"
            ) or secrets.token_urlsafe(32)
            script = (
                'exec gitea --config /data/gitea/conf/app.ini admin user create --username "$1" '
                '--password "$GITEA_E2E_PASSWORD" --email "$2" --admin '
                '--must-change-password=false --access-token --access-token-name "$3" '
                "--access-token-scopes all"
            )
            output = run(
                compose
                + [
                    "exec",
                    "-T",
                    "--user",
                    "git",
                    "-e",
                    "GITEA_E2E_PASSWORD",
                    "gitea",
                    "sh",
                    "-c",
                    script,
                    "bootstrap",
                    username,
                    os.environ.get("GITEA_ADMIN_EMAIL", "e2e-admin@example.invalid"),
                    token_name,
                ],
                label="Create Gitea admin",
                env=environment,
            )
        tokens = [word for word in output.split() if re.fullmatch(r"[a-fA-F0-9]{40}", word)]
        if len(tokens) != 1:
            raise RuntimeError("Gitea CLI did not return one token; output withheld")
        token = tokens[0]
    owner = os.environ.get("GITEA_OWNER", "e2e")
    repo = os.environ.get("GITEA_REPO", "demo")
    for value in (owner, repo):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("Gitea owner/repo must be simple slugs")
    # Persist immediately so a later repo-creation failure does not lose the token.
    write_env(
        STATE / "gitea.env",
        previous
        | {
            "GITEA_URL": url,
            "GITEA_INTERNAL_URL": "http://gitea:3000",
            "GITEA_TOKEN": token,
            # The scoped-token step below validates/reuses this separately from
            # the administrator credential used to create the private repo.
            "GITEA_PLUGIN_TOKEN": previous.get("GITEA_PLUGIN_TOKEN", ""),
            "GITEA_OWNER": owner,
            "GITEA_REPO": repo,
        },
    )
    ensure_gitea_repo(API(url, token, "token"), owner, repo, username)
    print("Gitea ready; credentials saved privately to .e2e/gitea.env.")


def gitea_rows(api: API, path: str) -> list[dict]:
    rows = []
    for page in range(1, 101):
        batch = api.request("GET", path, params={"page": page, "limit": 50})
        if not isinstance(batch, list):
            raise RuntimeError("Gitea bootstrap expected a list; response withheld")
        rows.extend(batch)
        if len(batch) < 50:
            return rows
    raise RuntimeError("Gitea bootstrap exceeded its pagination limit")


def ensure_gitea_oauth_app(api: API, values: dict[str, str]) -> None:
    path = "/api/v1/user/applications/oauth2"
    config = {
        "name": "sentry-gitea-e2e",
        "confidential_client": True,
        "redirect_uris": ["http://127.0.0.1:9000/extensions/gitea/setup/"],
        "skip_secondary_authorization": False,
    }
    matches = [app for app in gitea_rows(api, path) if app["name"] == config["name"]]
    if len(matches) > 1:
        raise RuntimeError("More than one managed Gitea OAuth app exists; resolve duplicates")
    app = matches[0] if matches else None
    if app is None:
        app = api.request("POST", path, expected=(201,), json=config)
    elif (
        app["client_id"] != values.get("GITEA_CLIENT_ID")
        or not values.get("GITEA_CLIENT_SECRET")
        or any(app.get(key) != value for key, value in config.items())
    ):
        # GET cannot recover the secret. Updating this one managed app rotates it.
        app = api.request("PATCH", f"{path}/{app['id']}", json=config)
    values["GITEA_OAUTH_APP_ID"] = str(app["id"])
    values["GITEA_CLIENT_ID"] = app["client_id"]
    if app.get("client_secret"):
        values["GITEA_CLIENT_SECRET"] = app["client_secret"]
    if not values.get("GITEA_CLIENT_SECRET"):
        raise RuntimeError("Gitea did not return the managed OAuth secret; response withheld")
    write_env(STATE / "gitea.env", values)


def bootstrap_gitea_integration() -> None:
    """Add isolated, non-admin OAuth fixtures without changing the plugin's repo/token."""
    values = read_env(STATE / "gitea.env")
    admin = API(values["GITEA_URL"], values["GITEA_TOKEN"], "token")
    username = values.get("GITEA_E2E_USERNAME", "e2e-user")
    user_path = f"/api/v1/admin/users/{username}"
    user = admin.request("GET", f"/api/v1/users/{username}", expected=(200, 404))
    if user and user.get("is_admin"):
        raise RuntimeError("The integration E2E user must be a dedicated non-admin account")
    values.update(
        GITEA_E2E_USERNAME=username,
        GITEA_E2E_PASSWORD=values.get("GITEA_E2E_PASSWORD") or "Aa1!" + secrets.token_urlsafe(32),
        GITEA_E2E_REPO="integration-demo",
        GITEA_E2E_DEFAULT_REPO="integration-z-default",
        GITEA_E2E_ORG="e2e-org",
        GITEA_E2E_MEMBER_ORG="e2e-member-org",
    )
    # Persist before mutations so interrupted bootstrap can recover the password.
    write_env(STATE / "gitea.env", values)
    if user is None:
        admin.request(
            "POST",
            "/api/v1/admin/users",
            expected=(201,),
            json={
                "username": username,
                "email": f"{username}@example.invalid",
                "password": values["GITEA_E2E_PASSWORD"],
                "must_change_password": False,
            },
        )
    admin.request(
        "PATCH",
        user_path,
        json={
            "password": values["GITEA_E2E_PASSWORD"],
            "must_change_password": False,
            "admin": False,
            "active": True,
            "prohibit_login": False,
            "allow_create_organization": True,
        },
    )
    user_api = API(values["GITEA_URL"])
    user_api.session.auth = (username, values["GITEA_E2E_PASSWORD"])
    try:
        identity = user_api.request("GET", "/api/v1/user")
        if identity["login"] != username or identity["is_admin"]:
            raise RuntimeError("Gitea E2E login is not the expected non-admin account")
        for repo in (values["GITEA_E2E_REPO"], values["GITEA_E2E_DEFAULT_REPO"]):
            ensure_gitea_repo(user_api, username, repo, username)
        owner_org = values["GITEA_E2E_ORG"]
        if admin.request("GET", f"/api/v1/orgs/{owner_org}", expected=(200, 404)) is None:
            admin.request(
                "POST",
                f"{user_path}/orgs",
                expected=(201,),
                json={"username": owner_org, "visibility": "private"},
            )
        owners = [
            team
            for team in gitea_rows(admin, f"/api/v1/orgs/{owner_org}/teams")
            if team["name"] == "Owners"
        ]
        if len(owners) != 1:
            raise RuntimeError("The E2E organization must have exactly one Owners team")
        admin.request("PUT", f"/api/v1/teams/{owners[0]['id']}/members/{username}", expected=(204,))
        ensure_gitea_repo(user_api, owner_org, values["GITEA_E2E_REPO"], username)
        member_org = values["GITEA_E2E_MEMBER_ORG"]
        admin_name = admin.request("GET", "/api/v1/user")["login"]
        ensure_gitea_repo(admin, member_org, values["GITEA_E2E_REPO"], admin_name)
        teams = gitea_rows(admin, f"/api/v1/orgs/{member_org}/teams")
        team = next((team for team in teams if team["name"] == "e2e-members"), None)
        if team is None:
            team = admin.request(
                "POST",
                f"/api/v1/orgs/{member_org}/teams",
                expected=(201,),
                json=MEMBER_TEAM,
            )
        else:
            # Gitea 28 grants unit access via units_map; heal teams created without it.
            admin.request("PATCH", f"/api/v1/teams/{team['id']}", expected=(200,), json=MEMBER_TEAM)
        admin.request("PUT", f"/api/v1/teams/{team['id']}/members/{username}", expected=(204,))
        for org, owns in ((owner_org, True), (member_org, False)):
            user_api.request("GET", f"/api/v1/orgs/{org}/members/{username}", expected=(204,))
            permissions = user_api.request(
                "GET", f"/api/v1/users/{username}/orgs/{org}/permissions"
            )
            # Membership is proven by the 204 above. Gitea derives org-level can_read
            # from the team's legacy access mode, which is "none" for units_map teams,
            # so only ownership is a meaningful fixture invariant here.
            if permissions["is_owner"] is not owns:
                raise RuntimeError("E2E organization membership does not match the fixtures")
        ensure_gitea_oauth_app(user_api, values)
    finally:
        admin.session.close()
        user_api.session.close()
    print("Non-admin Gitea OAuth fixtures ready; saved privately to .e2e/gitea.env.")


def bootstrap_gitea_plugin_token() -> None:
    """Complete CLI bootstrap with a separate least-privilege plugin credential."""
    values = read_env(STATE / "gitea.env")
    username = os.environ.get("GITEA_ADMIN_USERNAME", "e2e-admin")
    token = os.environ.get("GITEA_PLUGIN_TOKEN", values.get("GITEA_PLUGIN_TOKEN", ""))

    def valid(candidate: str) -> bool:
        metadata = API(values["GITEA_URL"], candidate, "token").request("GET", "/api/v1/token")
        return metadata["user"]["login"] == username and set(metadata["scopes"]) == set(
            GITEA_PLUGIN_SCOPES
        )

    if token:
        try:
            reusable = valid(token)
        except APIError as error:
            # A transient/server error is not grounds to mint another credential.
            if error.status != 401 or os.environ.get("GITEA_PLUGIN_TOKEN"):
                raise
            reusable = False
        if not reusable:
            if os.environ.get("GITEA_PLUGIN_TOKEN"):
                raise RuntimeError("GITEA_PLUGIN_TOKEN must have the exact plugin scopes and owner")
            token = ""
    if not token:
        output = run(
            gitea_compose()
            + [
                "exec",
                "-T",
                "--user",
                "git",
                "gitea",
                "gitea",
                "--config",
                "/data/gitea/conf/app.ini",
                "admin",
                "user",
                "generate-access-token",
                "--username",
                username,
                "--token-name",
                f"sentry-gitea-plugin-e2e-{secrets.token_hex(6)}",
                "--scopes",
                ",".join(GITEA_PLUGIN_SCOPES),
                "--raw",
            ],
            label="Generate scoped Gitea plugin token",
            env=network_environment(),
        )
        token = output.strip()
        if not re.fullmatch(r"[a-fA-F0-9]{40}", token):
            raise RuntimeError("Gitea CLI did not return one plugin token; output withheld")
        # Persist before the validation request, so a transient HTTP failure on
        # this last check does not lose a successfully minted token.
        values["GITEA_PLUGIN_TOKEN"] = token
        write_env(STATE / "gitea.env", values)
        if not valid(token):
            raise RuntimeError("Minted Gitea token has unexpected scopes or owner; output withheld")
    values["GITEA_PLUGIN_TOKEN"] = token
    write_env(STATE / "gitea.env", values)
    print("Scoped Gitea plugin token ready; saved privately to .e2e/gitea.env.")


def bootstrap_sentry() -> None:
    compose = self_hosted_compose()
    environment = dict(os.environ)
    environment["SENTRY_E2E_EMAIL"] = os.environ.get(
        "SENTRY_ADMIN_EMAIL", "e2e-admin@example.invalid"
    )
    # Self-hosted runs with SENTRY_SINGLE_ORGANIZATION = True: its HTML views (login
    # landing, integration setup) only serve the default "sentry" organization to
    # non-superusers. Use it, as real self-hosted installs do.
    environment["SENTRY_E2E_ORG"] = os.environ.get("SENTRY_ORG", "sentry")
    environment["SENTRY_E2E_PROJECT"] = os.environ.get("SENTRY_PROJECT", "demo")
    environment["SENTRY_E2E_URL"] = os.environ.get("SENTRY_URL", "http://127.0.0.1:9000")
    previous = read_env(STATE / "sentry.env")
    environment["SENTRY_E2E_TOKEN"] = previous.get("SENTRY_TOKEN", "")
    environment["SENTRY_E2E_PASSWORD"] = previous.get("SENTRY_ADMIN_PASSWORD") or (
        "Aa1!" + secrets.token_urlsafe(32)
    )
    environment["SENTRY_E2E_OWNER_PASSWORD"] = previous.get("SENTRY_OWNER_PASSWORD") or (
        "Aa1!" + secrets.token_urlsafe(32)
    )
    write_env(
        STATE / "sentry.env",
        previous
        | {
            "SENTRY_ADMIN_PASSWORD": environment["SENTRY_E2E_PASSWORD"],
            "SENTRY_OWNER_USERNAME": "e2e-owner",
            "SENTRY_OWNER_PASSWORD": environment["SENTRY_E2E_OWNER_PASSWORD"],
        },
    )
    output_path = f"/tmp/sentry-gitea-{secrets.token_hex(12)}.json"
    environment["SENTRY_E2E_OUTPUT"] = output_path
    run(
        compose
        + [
            "exec",
            "-T",
            "web",
            "sentry",
            "createuser",
            "--no-input",
            "--superuser",
            "--no-password",
            "--email",
            environment["SENTRY_E2E_EMAIL"],
        ],
        label="Create Sentry superuser",
        env=environment,
        timeout=180,
    )
    command = compose + ["exec", "-T"]
    for name in ("EMAIL", "ORG", "PROJECT", "URL", "TOKEN", "OUTPUT", "PASSWORD", "OWNER_PASSWORD"):
        command += ["-e", f"SENTRY_E2E_{name}"]
    try:
        run(
            command + ["web", "sentry", "exec"],
            label="Seed Sentry org/project/token",
            env=environment,
            input_text=(ROOT / "docker/e2e/seed-sentry.py").read_text(),
            timeout=180,
        )
        with tempfile.TemporaryDirectory(dir=STATE) as temporary:
            result_path = Path(temporary) / "sentry.json"
            run(
                compose + ["cp", f"web:{output_path}", str(result_path)],
                label="Read private Sentry bootstrap result",
            )
            values = json.loads(result_path.read_text())
            write_env(STATE / "sentry.env", values)
    finally:
        run(
            compose + ["exec", "-T", "web", "rm", "-f", output_path],
            label="Remove temporary bootstrap result",
        )
    print("Sentry ready; credentials saved privately to .e2e/sentry.env.")


def sentry_tests() -> None:
    build_wheel(STATE / "sentry-tests-wheel")
    compose = [
        "docker",
        "compose",
        "-p",
        "sentry-gitea-tests",
        "-f",
        str(ROOT / "docker/test-sentry.compose.yml"),
    ]
    try:
        run(
            compose + ["up", "-d", "--wait", "postgres", "redis"],
            label="Start isolated test services",
            capture=False,
            timeout=180,
        )
        run(
            compose + ["run", "--rm", "tests"],
            label="Sentry smoke tests",
            capture=False,
            timeout=1800,
        )
    finally:
        run(compose + ["down"], label="Stop isolated test services", capture=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "install",
            "uninstall",
            "gitea-up",
            "gitea-down",
            "bootstrap-gitea",
            "bootstrap-sentry",
            "sentry-tests",
        ],
    )
    action = parser.parse_args().action
    STATE.mkdir(mode=0o700, exist_ok=True)
    check_compose()
    if action in {"install", "uninstall"}:
        install(uninstall=action == "uninstall")
    elif action == "bootstrap-gitea":
        bootstrap_gitea()
        bootstrap_gitea_plugin_token()
        bootstrap_gitea_integration()
    elif action == "bootstrap-sentry":
        bootstrap_sentry()
    elif action == "sentry-tests":
        sentry_tests()
    elif action == "gitea-up":
        gitea_up()
    else:
        run(
            gitea_compose() + ["down"],
            label=action,
            env=network_environment(subnets=False),
            capture=False,
            timeout=240,
        )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
