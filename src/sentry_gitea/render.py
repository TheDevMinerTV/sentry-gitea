"""Pure issue content with conservative classification.

Derived from the author's earlier tooling. No raw event payloads, agent policy, database
access, or Sentry imports belong here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

MAX_PATHS = 3
MAX_COMMITS_SCANNED = 250
MAX_COMMITS_RENDERED = 5
MAX_LABELS = 20
_META = re.compile(r"<!--\s*sentry-gitea-meta v1\s+(.*?)\s*-->", re.DOTALL)


@dataclass(frozen=True)
class EventData:
    short_id: str
    title: str
    sentry_url: str
    project_id: int
    group_id: int
    issue_url: str
    event_id: str = ""
    event_url: str = ""
    level: str = "error"
    release: str = ""
    first_release: str = ""
    environment: str = ""
    culprit: str = ""
    handled: bool | None = None
    fingerprint: str = ""
    # Set when this issue replaces a closed one the group was linked to (0.2.0 re-filing).
    recurrence_of: str = ""
    recurrence_closed: str = ""

    @property
    def dedup_id(self) -> str:
        return f"{self.sentry_url.rstrip('/')}/{self.project_id}/{self.group_id}"


def event_is_handled(data: Mapping[str, Any] | None) -> bool | None:
    if not data:
        return None
    exceptions = data.get("exception")
    values = exceptions.get("values") if isinstance(exceptions, Mapping) else None
    if isinstance(values, Sequence) and not isinstance(values, str) and values:
        verdicts = []
        for value in values:
            mechanism = value.get("mechanism") if isinstance(value, Mapping) else None
            verdict = mechanism.get("handled") if isinstance(mechanism, Mapping) else None
            verdicts.append(verdict if type(verdict) is bool else None)
        if any(value is False for value in verdicts):
            return False
        return True if all(value is True for value in verdicts) else None
    no_exceptions = not exceptions or (isinstance(values, list) and not values)
    return True if data.get("logentry") and no_exceptions else None


def map_class(handled: bool | None) -> str:
    return "crash" if handled is False else "other"


def map_severity(level: str, handled: bool | None) -> str:
    if handled is True:
        return "medium" if level == "fatal" else "low"
    severity = {"fatal": "critical", "error": "high", "warning": "medium"}.get(level, "low")
    return "medium" if handled is None and severity in {"critical", "high"} else severity


def resolve_release(release: str | None, first_release: str | None = None) -> str:
    return (release or "").strip() or (first_release or "").strip() or "(unknown)"


def clean_text(value: Any) -> str:
    return " ".join(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "")).split())


def code_span(value: Any) -> str:
    """Inline code for untrusted text: Gitea creates no @mentions, references or links in it.

    The fence is longer than any backtick run in the value, and a space pads values that start
    or end with a backtick (CommonMark strips one). HTML comment delimiters get a zero-width
    space so the raw body never contains a forged metadata marker.
    """
    text = clean_text(value).replace("<!--", "<!\u200b--").replace("-->", "--\u200b>")
    if not text:
        return ""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _field(value: Any, missing: str = "(unknown)") -> str:
    return code_span(value) or missing


def _url(value: str) -> str:
    return quote(value, safe=":/?=&%+#@;,")


def render_title(event: EventData) -> str:
    identifier = clean_text(event.short_id)[:80] or str(event.group_id)
    return f"[{identifier}] {clean_text(event.title) or 'Sentry issue'}"[:255]


def _recurrence_number(event: EventData) -> str:
    value = str(event.recurrence_of or "")
    return value if re.fullmatch(r"[1-9][0-9]{0,18}", value) else ""


def render_metadata(event: EventData) -> str:
    metadata: dict[str, Any] = {
        "source": "sentry",
        "dedup_id": event.dedup_id,
        "sentry_issue_url": event.issue_url,
        "sentry_event_url": event.event_url,
        "fingerprint": event.fingerprint,
        "severity": map_severity(event.level, event.handled),
        "class": map_class(event.handled),
        "release": resolve_release(event.release, event.first_release),
    }
    if recurrence := _recurrence_number(event):
        metadata["recurrence_of"] = recurrence
    # Keep valid JSON while preventing user text from terminating the HTML comment.
    encoded = (
        json.dumps(metadata, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e")
    )
    return f"<!-- sentry-gitea-meta v1 {encoded} -->"


def parse_metadata(body: str, *, dedup_id: str | None = None) -> dict[str, Any] | None:
    if not isinstance(body, str):
        return None
    for match in _META.finditer(body):
        try:
            value = json.loads(match.group(1))
        except (ValueError, RecursionError):
            continue
        if (
            isinstance(value, dict)
            and value.get("source") == "sentry"
            and isinstance(value.get("dedup_id"), str)
            and (dedup_id is None or value["dedup_id"] == dedup_id)
        ):
            return value
    return None


def strip_metadata(body: str) -> str:
    # Repeat until stable: removing one marker can join the text around it into another.
    while (stripped := _META.sub("", body)) != body:
        body = stripped
    return body


def ensure_metadata(body: str, event: EventData) -> str:
    text = strip_metadata(body).rstrip()
    return f"{text}\n\n{render_metadata(event)}" if text else render_metadata(event)


def _release(event: EventData) -> str:
    return (event.release or "").strip() or (event.first_release or "").strip()


def render_body(event: EventData) -> str:
    # Event-derived values are untrusted (any DSN holder can send them): code spans only.
    lines = [
        f"# {_field(event.title, 'Sentry issue')}",
        f"**Sentry issue:** <{_url(event.issue_url)}>",
    ]
    if recurrence := _recurrence_number(event):
        # Digits only, so Gitea turns it into a cross-reference on the closed issue.
        closed = event.recurrence_closed
        when = f" (closed {closed})" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", closed or "") else ""
        lines.append(f"**Recurrence of:** #{recurrence}{when}")
    lines.extend(
        [
            f"**Level:** {_field(event.level)}",
            f"**Release:** {_field(_release(event))}",
        ]
    )
    if clean_text(event.environment):
        lines.append(f"**Environment:** {_field(event.environment)}")
    lines.extend(
        [
            f"**Affected component / location:** {_field(event.culprit)}",
            f"**Last-seen event ID:** {_field(event.event_id)}",
        ]
    )
    if event.handled is None:
        lines.append(
            "Classification is unknown: there is no complete handled/unhandled evidence. "
            "Class is other; severity is capped at medium."
        )
    lines.append(
        "**Reproduction:** Inspect the event and its context in "
        f"[Sentry](<{_url(event.issue_url)}>)."
    )
    return "\n\n".join(lines)


def render_comment(event: EventData) -> str:
    lines = [
        "Another Sentry event triggered an alert.",
        f"**Event ID:** {_field(event.event_id)}",
        f"**Level:** {_field(event.level)}",
        f"**Environment:** {_field(event.environment)}",
        f"**Release:** {_field(_release(event))}",
        f"**Sentry event:** <{_url(event.event_url or event.issue_url)}>",
    ]
    marker_id = re.sub(r"[^a-zA-Z0-9_-]", "", event.event_id)[:128]
    if marker_id:
        lines.append(f"<!-- sentry-gitea-event {marker_id} -->")
    return "\n\n".join(lines)


def parse_label_names(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    parts = value.split(",") if isinstance(value, str) else value
    if not isinstance(parts, Sequence):
        raise ValueError("Labels must be a comma-separated list of names.")
    result = []
    for part in parts:
        if not isinstance(part, str) or any(ord(c) < 32 for c in part):
            raise ValueError("Label names must be text without control characters.")
        name = part.strip()
        if len(name) > 100 or "," in name:
            raise ValueError("Each label name must be at most 100 characters without commas.")
        if name and name not in result:
            result.append(name)
    if len(result) > MAX_LABELS:
        raise ValueError(f"At most {MAX_LABELS} labels may be requested.")
    return result


def normalize_release(release: str) -> str:
    candidate = release.rsplit("@", 1)[-1].strip()
    return candidate if re.fullmatch(r"[0-9a-f]{7,40}", candidate) else ""


def culprit_to_paths(culprit: str) -> list[str]:
    paths = re.findall(
        r"[\w./@-]*[\w-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rb|java|kt|php|rs|cs)\b", culprit
    )
    for module, symbol in re.findall(
        r"(?<![\w./-])([A-Za-z_][\w-]*(?:\.[\w-]+)+):([A-Za-z_][\w-]*)(?![\w.-])", culprit
    ):
        paths.append(f"{module.replace('.', '/')}/{symbol}")
    return list(dict.fromkeys(path.strip("/") for path in paths))[:MAX_PATHS]


def render_fix_hint(commits: list[Any], paths: list[str], base: str, head: str) -> str:
    relevant = []
    for commit in commits[:MAX_COMMITS_SCANNED]:
        if not isinstance(commit, dict) or not isinstance(commit.get("files"), list):
            continue
        filenames = [f.get("filename") if isinstance(f, dict) else f for f in commit["files"]]
        if any(isinstance(f, str) and any(p in f for p in paths[:MAX_PATHS]) for f in filenames):
            relevant.append(commit)
    if not relevant:
        return ""
    lines = [
        f"**Where to look (advisory):** Changes from {_field(base)} to {_field(head)} "
        "touch files suggested by the Sentry location:"
    ]
    for commit in relevant[:MAX_COMMITS_RENDERED]:
        details = commit.get("commit") or {}
        subject = (
            str(details.get("message") or "").splitlines() if isinstance(details, dict) else []
        )
        # Commit subjects are repository text and may @mention people: code spans too.
        lines.append(
            f"- {_field(str(commit.get('sha') or '')[:8])} "
            f"{code_span(subject[0] if subject else '')}".rstrip()
        )
    lines.append(
        "This heuristic is not blame or proof of a fix. "
        "Verify before closing; labels are unchanged."
    )
    return "\n\n".join(lines)
