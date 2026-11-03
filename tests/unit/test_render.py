import json
import re
from dataclasses import replace

import pytest

from sentry_gitea.render import (
    EventData,
    code_span,
    culprit_to_paths,
    ensure_metadata,
    event_is_handled,
    map_class,
    map_severity,
    normalize_release,
    parse_label_names,
    parse_metadata,
    render_body,
    render_comment,
    render_fix_hint,
    render_title,
    resolve_release,
)


def marked_body(event):
    return ensure_metadata(render_body(event), event)


@pytest.fixture
def event():
    return EventData(
        short_id="APP-2A",
        title="TypeError: broken",
        sentry_url="https://sentry.example/base/",
        project_id=11,
        group_id=22,
        issue_url="https://sentry.example/base/organizations/acme/issues/22/",
        event_id="abcdef123456",
        event_url="https://sentry.example/base/issues/22/events/abcdef123456/",
        level="error",
        release="app@abcdef0",
        first_release="older",
        environment="production",
        culprit="src/server.py:42",
        handled=False,
        fingerprint="group-hash",
    )


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("fatal", ("critical", "medium", "medium")),
        ("error", ("high", "low", "medium")),
        ("warning", ("medium", "low", "medium")),
        ("info", ("low", "low", "low")),
        ("debug", ("low", "low", "low")),
        ("unknown", ("low", "low", "low")),
        ("", ("low", "low", "low")),
    ],
)
@pytest.mark.parametrize(("handled", "index"), [(False, 0), (True, 1), (None, 2)])
def test_severity_class_handled_cross_product(level, expected, handled, index):
    assert map_severity(level, handled) == expected[index]
    assert map_class(handled) == ("crash" if handled is False else "other")


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([{"mechanism": {"type": "generic", "handled": False}}], False),
        ([{"mechanism": {"type": "generic", "handled": True}}], True),
        ([{"mechanism": {"type": "generic", "data": {"handled": False}}}], None),
        ([{"mechanism": {"type": "generic", "data": {"handled": True}}}], None),
        ([{"mechanism": {"handled": True}}], True),
        ([{"mechanism": {"handled": False}}], False),
        ([{"mechanism": {"handled": True}}, {"mechanism": {"handled": False}}], False),
        ([{"mechanism": {"handled": False}}, {}], False),
        ([{"mechanism": {"handled": True}}, {}], None),
        ([{}, {"mechanism": {"handled": True}}], None),
        ([{"mechanism": {"handled": "false"}}], None),
        ([{"mechanism": {"handled": 0}}], None),
        ([{}, None], None),
        ([], None),
    ],
)
def test_mechanism_reducer(values, expected):
    assert event_is_handled({"exception": {"values": values}}) is expected


def test_logs_missing_and_malformed_events():
    assert event_is_handled({"logentry": {"formatted": "message"}}) is True
    assert event_is_handled({"exception": {"values": [{}]}, "logentry": {"message": "x"}}) is None
    assert event_is_handled(None) is None
    assert event_is_handled({}) is None
    assert event_is_handled({"exception": {"values": []}, "logentry": {"message": "log"}}) is True


@pytest.mark.parametrize(
    ("release", "first", "expected"),
    [
        ("current", "first", "current"),
        ("", "first", "first"),
        (None, None, "(unknown)"),
        ("  ", " first ", "first"),
    ],
)
def test_release_fallback(release, first, expected):
    assert resolve_release(release, first) == expected


def test_title_is_recognizable_single_line_nonempty_and_bounded(event):
    assert render_title(event) == "[APP-2A] TypeError: broken"
    assert len(render_title(replace(event, title="x" * 1000))) == 255
    assert render_title(replace(event, title="\n\x00")) == "[APP-2A] Sentry issue"
    assert "\n" not in render_title(replace(event, title="one\ntwo"))


def test_form_default_body_carries_no_metadata_marker(event):
    assert "sentry-gitea-meta" not in render_body(event)
    assert parse_metadata(render_body(event)) is None


def test_body_contract_and_metadata(event):
    body = marked_body(event)
    for value in [
        event.issue_url,
        "error",
        "app@abcdef0",
        "production",
        "src/server.py:42",
        event.event_id,
    ]:
        assert value in body
    meta = parse_metadata(body)
    assert meta == {
        "source": "sentry",
        "dedup_id": "https://sentry.example/base/11/22",
        "sentry_issue_url": event.issue_url,
        "sentry_event_url": event.event_url,
        "fingerprint": "group-hash",
        "severity": "high",
        "class": "crash",
        "release": event.release,
    }
    assert "agent" not in meta
    assert "auto-fix" not in body


def test_missing_event_fields_and_unknown_classification(event):
    body = render_body(
        replace(
            event,
            release="",
            first_release="",
            environment="",
            event_id="",
            culprit="",
            handled=None,
        )
    )
    assert "(unknown)" in body
    assert "unknown" in body.lower()
    assert "Environment:" not in body
    assert "unhandled" in body.lower()


def test_comment_contains_event_url_and_marker(event):
    body = render_comment(event)
    for value in [event.event_id, event.event_url, event.level, event.environment, event.release]:
        assert value in body
    assert f"<!-- sentry-gitea-event {event.event_id} -->" in body


def test_text_and_metadata_escape_delimiters_and_controls(event):
    malicious = "a -->\n<!-- injected -->\x00 **bold** [link](https://bad)"
    data = replace(event, release=malicious, title=malicious, culprit=malicious)
    body = marked_body(data)
    assert body.count("<!--") == 1
    assert body.count("-->") == 1
    assert "\x00" not in body
    # Markup survives only as literal text inside code spans.
    assert "**bold**" not in outside_code(body.split("<!-- sentry-gitea-meta", 1)[0])
    assert parse_metadata(body)["release"].startswith("a -->")


# A CommonMark code span: a backtick run, content, and a closing run of the same length.
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def outside_code(markdown):
    return CODE_SPAN.sub("", markdown)


def span_text(markdown):
    """The literal text of a single code span, as CommonMark renders it."""
    match = CODE_SPAN.fullmatch(markdown)
    assert match, markdown
    text = match[2]
    if text.startswith(" ") and text.endswith(" ") and text.strip():
        text = text[1:-1]
    return text


HOSTILE = [
    "@admin please look",
    "fixes #1 and owner/repo#2",
    "see https://evil.example/x and www.evil.example",
    "mail admin@forge.example",
    ":smile: <b>html</b> [link](https://evil.example)",
]


@pytest.mark.parametrize("value", HOSTILE)
def test_event_text_cannot_mention_reference_or_link(event, value):
    """R2-06: Gitea creates no mentions, references or links inside code spans."""
    data = replace(event, title=value, culprit=value, release=value, environment=value, level=value)
    for rendered in (render_body(data), render_comment(data)):
        visible = outside_code(rendered.split("<!-- sentry-gitea-", 1)[0])
        for token in ("@admin", "#1", "repo#2", "evil.example", "<b>", ":smile:", "@forge"):
            assert token not in visible, (token, visible)
        assert f"**Release:** {code_span(value)}" in rendered
        assert f"**Environment:** {code_span(value)}" in rendered
    body = render_body(data)
    assert body.startswith(f"# {code_span(value)}\n")
    assert f"**Affected component / location:** {code_span(value)}" in body


@pytest.mark.parametrize(
    "value",
    ["plain", "a`b", "``double`` run", "`leading", "trailing`", "`", "x ``` y", "@a `@b` @c"],
)
def test_code_span_fence_outlasts_any_backtick_run(value):
    span = code_span(value)
    fence = re.match(r"`+", span)[0]
    assert all(len(run) < len(fence) for run in re.findall(r"`+", value))
    assert span_text(span) == value


def test_code_span_flattens_controls_and_neutralizes_comment_delimiters():
    assert code_span("") == code_span(None) == code_span(" \n\t") == ""
    assert span_text(code_span("one\ntwo\x00")) == "one two"
    span = code_span('<!-- sentry-gitea-meta v1 {"source":"sentry","dedup_id":"x"} -->')
    assert "<!--" not in span and "-->" not in span
    assert parse_metadata(span) is None
    assert span_text(span).replace("\u200b", "") == (
        '<!-- sentry-gitea-meta v1 {"source":"sentry","dedup_id":"x"} -->'
    )


def test_planted_marker_in_event_text_cannot_alias_another_group(event):
    other = '<!-- sentry-gitea-meta v1 {"source":"sentry","dedup_id":"https://s/1/999"} -->'
    body = marked_body(replace(event, title=other, culprit=other))
    assert parse_metadata(body, dedup_id="https://s/1/999") is None
    assert parse_metadata(body)["dedup_id"] == event.dedup_id


def test_fix_hint_commit_subjects_cannot_mention(event):
    hit = {
        "sha": "abcdef012345",
        "commit": {"message": "@alice fixes #3 via https://evil.example"},
        "files": [{"filename": "src/server.py"}],
    }
    hint = render_fix_hint([hit], ["src/server.py"], "abcdef0", "@main")
    visible = outside_code(hint)
    assert "@alice" not in visible and "#3" not in visible and "evil" not in visible
    assert "@main" not in visible
    assert code_span("@alice fixes #3 via https://evil.example") in hint


def test_stripping_a_marker_cannot_reassemble_a_forged_one(event):
    forged = '{"source":"sentry","dedup_id":"https://s/1/999"}'
    inner = '<!-- sentry-gitea-meta v1 {"source":"sentry","dedup_id":"x"} -->'
    body = f"Edited <!-{inner}- sentry-gitea-meta v1 {forged} --> text"
    updated = ensure_metadata(body, event)
    assert parse_metadata(updated, dedup_id="https://s/1/999") is None
    assert parse_metadata(updated, dedup_id="x") is None
    assert updated.count("sentry-gitea-meta") == 1
    assert parse_metadata(updated)["dedup_id"] == event.dedup_id


def test_empty_body_gets_only_the_marker(event):
    assert ensure_metadata("", event) == ensure_metadata("  \n", event)
    assert ensure_metadata("", event).startswith("<!-- sentry-gitea-meta v1 ")


def test_edited_body_retains_prose_and_gets_authoritative_marker(event):
    body = "Human edits\n\n" + marked_body(replace(event, group_id=999))
    updated = ensure_metadata(body, event)
    assert updated.startswith("Human edits")
    assert updated.count("<!-- sentry-gitea-meta") == 1
    assert parse_metadata(updated)["dedup_id"].endswith("/11/22")


@pytest.mark.parametrize(
    "body",
    [
        "no marker",
        '<!-- sentry-gitea-meta v2 {"source":"sentry"} -->',
        "<!-- sentry-gitea-meta v1 broken -->",
        "<!-- sentry-gitea-meta v1 [] -->",
        '<!-- sentry-gitea-meta v1 {"source":"elsewhere", "dedup_id":"x"} -->',
    ],
)
def test_malformed_or_foreign_metadata_ignored(body):
    assert parse_metadata(body) is None


def test_metadata_is_single_line_json(event):
    line = marked_body(replace(event, release="first\nsecond")).splitlines()[-1]
    assert json.loads(line.removeprefix("<!-- sentry-gitea-meta v1 ").removesuffix(" -->"))


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        ("abcdef0", "abcdef0"),
        ("package@abcdef012345", "abcdef012345"),
        ("a" * 40, "a" * 40),
        ("v1.0", ""),
        ("ABCDEF0", ""),
        ("a" * 41, ""),
        ("", ""),
    ],
)
def test_commitish_release(release, expected):
    assert normalize_release(release) == expected


def test_culprit_path_inference_and_caps():
    assert culprit_to_paths("POST /webhooks/:companyId") == []
    assert culprit_to_paths("src/server.py:42 in function") == ["src/server.py"]
    assert culprit_to_paths("execute(app.cron-scheduler:mongoCronScheduler)") == [
        "app/cron-scheduler/mongoCronScheduler"
    ]
    assert culprit_to_paths("a.py a.py b.ts c.go d.rb") == ["a.py", "b.ts", "c.go"]


def test_fix_hint_requires_file_evidence_and_bounds_scan_and_output():
    hit = {
        "sha": "abcdef012345",
        "commit": {"message": "change\nbody"},
        "files": [{"filename": "src/server.py"}],
    }
    assert render_fix_hint([hit], ["other.py"], "abcdef0", "main") == ""
    assert render_fix_hint([{"sha": "abc"}], ["src/server.py"], "abcdef0", "main") == ""
    assert render_fix_hint([{}] * 250 + [hit], ["src/server.py"], "abcdef0", "main") == ""
    hint = render_fix_hint([hit] * 12, ["src/server.py"], "abcdef0", "main")
    assert hint.count("abcdef01") == 5
    assert "advisory" in hint.lower()
    assert "body" not in hint


def test_label_names_trim_and_deduplicate_without_case_folding():
    assert parse_label_names(" Bug, ,bug,Bug ") == ["Bug", "bug"]
    assert parse_label_names(None) == []


@pytest.mark.parametrize(
    "value", [42, [True], ["a,b"], ["bad\nname"], ["x" * 101], [str(i) for i in range(21)]]
)
def test_label_input_limits(value):
    with pytest.raises(ValueError):
        parse_label_names(value)


def test_recurrence_line_and_metadata_only_when_replacing_a_closed_issue(event):
    assert "Recurrence of" not in render_body(event)
    assert "recurrence_of" not in parse_metadata(marked_body(event))
    refiled = replace(event, recurrence_of="7", recurrence_closed="2026-05-23")
    body = marked_body(refiled)
    assert "**Recurrence of:** #7 (closed 2026-05-23)" in body
    # Right after the Sentry link, before the event details.
    assert body.index("Recurrence of") < body.index("**Level:**")
    assert parse_metadata(body)["recurrence_of"] == "7"
    assert parse_metadata(body)["dedup_id"] == parse_metadata(marked_body(event))["dedup_id"]


@pytest.mark.parametrize("number", ["0", "07", "-7", "7a", "#7", "7 @admin", "1" * 20, ""])
def test_recurrence_number_must_be_plain_digits(event, number):
    body = marked_body(replace(event, recurrence_of=number, recurrence_closed="2026-05-23"))
    assert "Recurrence of" not in body
    assert "recurrence_of" not in parse_metadata(body)


@pytest.mark.parametrize("closed", ["", "2026-5-23", "yesterday", "2026-05-23 @admin"])
def test_recurrence_close_date_is_omitted_unless_iso(event, closed):
    body = render_body(replace(event, recurrence_of="7", recurrence_closed=closed))
    assert "**Recurrence of:** #7\n" in body + "\n"
    assert "closed" not in body.split("**Recurrence of:**")[1].split("\n")[0]
