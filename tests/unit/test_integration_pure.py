import hashlib

import pytest

from sentry_gitea.integration.selection import (
    PickedRepositories,
    RepositoryChoice,
    initially_ticked,
    is_selected,
    parse_repository_ids,
    repository_choices,
    stored_selection,
)
from sentry_gitea.integration.utils import (
    OAUTH_SCOPES,
    external_id,
    issue_key,
    normalize_tokens,
    parse_issue_key,
    validate_scopes,
)


def test_external_identity_is_stable_bounded_and_namespace_specific():
    value = external_id("https://FORGE.example/git/", "org", 8)
    assert value == hashlib.sha256(b"v1|https://forge.example/git|org|8").hexdigest()
    assert len(value) == 64
    assert value != external_id("https://forge.example/git", "user", 8)
    assert value != external_id("https://forge.example", "org", 8)


def test_canonical_issue_key_uses_repo_id_and_local_number():
    assert issue_key(41, 7) == "41#7"
    assert parse_issue_key("41#7") == (41, 7)


@pytest.mark.parametrize("key", ["owner/repo#7", "0#1", "1#0", "01#7", "1#2#3", None])
def test_invalid_issue_keys_are_rejected(key):
    with pytest.raises(ValueError):
        parse_issue_key(key)


def test_token_normalizer_handles_scope_omission_and_rotated_refresh_token():
    tokens = normalize_tokens(
        {"access_token": "test-access", "refresh_token": "test-next", "expires_in": 3600}, now=100
    )
    assert tokens == {"access_token": "test-access", "refresh_token": "test-next", "expires": 3700}
    updated = normalize_tokens(
        {"access_token": "test-next-access", "expires_in": 20}, previous=tokens, now=200
    )
    assert updated["refresh_token"] == "test-next"
    assert updated["expires"] == 220
    assert validate_scopes(OAUTH_SCOPES) == list(OAUTH_SCOPES)


@pytest.mark.parametrize("scopes", [[], ["all"], ["read:user", "made:up"], "openid"])
def test_invalid_or_incomplete_requested_scopes_never_fall_back_to_all(scopes):
    with pytest.raises(ValueError):
        validate_scopes(scopes)


@pytest.mark.parametrize(
    "tokens",
    [
        {},
        {"access_token": "test"},
        {"access_token": "test", "refresh_token": "test", "expires_in": "oops"},
        {"access_token": "test", "refresh_token": "test", "expires_in": -1},
    ],
)
def test_malformed_token_responses_are_rejected_without_echoing_them(tokens):
    with pytest.raises(ValueError, match="Gitea returned invalid OAuth credentials"):
        normalize_tokens(tokens)


def test_repository_selection_is_sorted_unique_ids_and_missing_means_all():
    assert parse_repository_ids(["42", 41, 42]) == [41, 42]
    assert stored_selection({}) is None
    assert stored_selection({"repository_ids": []}) == frozenset()
    assert stored_selection({"repository_ids": [41]}) == {41}
    assert is_selected(None, "43") and is_selected(frozenset({41}), "41")
    assert not is_selected(frozenset({41}), "43") and not is_selected(frozenset({41}), "team/x")


@pytest.mark.parametrize("value", ["41", [0], [-1], ["4a"], [True], [4.0], ["٤١"], None])
def test_invalid_repository_selections_are_rejected(value):
    with pytest.raises(ValueError):
        parse_repository_ids(value)


def picked(offered, ticked, reinstall=True):
    return PickedRepositories(frozenset(offered), frozenset(ticked), reinstall)


@pytest.mark.parametrize(
    ("submit", "stored", "imported", "expected"),
    [
        (picked([41, 43], [43], reinstall=False), frozenset({41, 50}), {41, 50}, {43}),
        (picked([41, 43], [], reinstall=False), None, {41}, set()),
        (picked([41, 43], [41]), frozenset({41, 50}), {41, 50}, {41, 50}),
        (picked([41, 43], [41, 43]), frozenset({41, 50}), {41, 50}, {41, 43, 50}),
        (picked([41, 43], []), frozenset({41, 50}), {41, 50}, {50}),
        (picked([], []), frozenset({41, 50}), {41, 50}, {41, 50}),
        (picked([41, 43], [41, 43]), None, {41, 50}, None),
        (picked([41, 43], [41]), None, {41, 43, 50}, {41, 50}),
    ],
)
def test_a_reinstall_changes_only_the_repositories_its_picker_offered(
    submit, stored, imported, expected
):
    assert submit.applied_to(stored, imported) == expected


def test_the_install_picker_ticks_the_selection_of_a_reinstall_only():
    assert initially_ticked(False, None, [41, 43]) == frozenset()
    assert initially_ticked(True, None, [41, 43]) == {41, 43}
    assert initially_ticked(True, frozenset({41, 50}), [41, 43]) == {41}
    assert initially_ticked(True, frozenset(), [41, 43]) == frozenset()


def test_picked_repositories_survive_the_pipeline_state_and_reject_tampering():
    submit = picked([41, 43], [41])
    assert PickedRepositories.from_state(submit.to_state()) == submit
    for state in (
        [41],
        {"offered": [41], "ticked": [41]},
        {"offered": [41], "ticked": [43], "reinstall": True},
        {"offered": [41], "ticked": [41], "reinstall": "yes"},
    ):
        with pytest.raises(ValueError):
            PickedRepositories.from_state(state)


def test_repository_choices_keep_only_administered_repositories_of_the_owner():
    def row(repo_id, full_name, owner=8, admin=True, **changes):
        return {
            "id": repo_id,
            "full_name": full_name,
            "owner": {"id": owner},
            "permissions": {"admin": admin},
            **changes,
        }

    rows = [
        row(43, "team/Zeta", description="d" * 300),
        row(41, "team/alpha"),
        row(42, "team/read-only", admin=False),
        row(44, "other/repo", owner=9),
        row(45, "team/bad name"),
        row("46", "team/string-id"),
    ]
    assert repository_choices(rows, 8) == [
        RepositoryChoice(41, "team/alpha", ""),
        RepositoryChoice(43, "team/Zeta", "d" * 200),
    ]
