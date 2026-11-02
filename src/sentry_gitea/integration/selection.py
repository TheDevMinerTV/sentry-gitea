"""Which of the owner's repositories an installation syncs, stored on the organization config.

`config["repository_ids"]` is a sorted list of Gitea repository IDs, which survive renames. An
installation without the key predates the picker and syncs every repository; an empty list
syncs none.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sentry_gitea.api.client import normalize_repo

REPOSITORIES_KEY = "repository_ids"
# Repository listings stop at 20 pages of 50.
MAX_SELECTED = 1000

Selection = frozenset[int] | None


@dataclass(frozen=True)
class RepositoryChoice:
    id: int
    full_name: str
    description: str


def repository_choices(rows: Iterable[Any], owner_id: int) -> list[RepositoryChoice]:
    """The owner's repositories the user administers: only those can carry the Sentry hook."""
    choices = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        owner, permissions = row.get("owner"), row.get("permissions")
        if (
            not isinstance(owner, dict)
            or str(owner.get("id")) != str(owner_id)
            or not isinstance(permissions, dict)
            or permissions.get("admin") is not True
            or type(row.get("id")) is not int
            or row["id"] <= 0
        ):
            continue
        try:
            full_name = normalize_repo(row.get("full_name"))
        except ValueError:
            continue
        description = row.get("description")
        choices.append(
            RepositoryChoice(
                row["id"], full_name, description[:200] if isinstance(description, str) else ""
            )
        )
    return sorted(choices, key=lambda choice: choice.full_name.casefold())


def parse_repository_ids(value: Any) -> list[int]:
    if not isinstance(value, list | tuple):
        raise ValueError("Select repositories from the list.")
    ids = set()
    for item in value:
        if type(item) is str and item.isascii() and item.isdigit():
            item = int(item)
        if type(item) is not int or item <= 0:
            raise ValueError("Select repositories from the list.")
        ids.add(item)
    if len(ids) > MAX_SELECTED:
        raise ValueError(f"Select at most {MAX_SELECTED} repositories.")
    return sorted(ids)


def stored_selection(config: Mapping[str, Any] | None) -> Selection:
    """The selected IDs, or None (every repository) for installations without a selection."""
    if not config or REPOSITORIES_KEY not in config:
        return None
    try:
        return frozenset(parse_repository_ids(config[REPOSITORIES_KEY]))
    except ValueError:
        return frozenset()


def is_selected(selection: Selection, repo_id: Any) -> bool:
    if selection is None:
        return True
    value = str(repo_id)
    return value.isascii() and value.isdigit() and int(value) in selection
