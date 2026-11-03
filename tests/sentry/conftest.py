"""Load the host image's test support only for the Sentry test suite.

The autouse fixtures below are adapted from Sentry 24.10.0 `tests/conftest.py`
(Apache-2.0 since 2026-10-17; see NOTICE). Sentry ships them in its repository's
test tree, not in the image, but its test case classes depend on them: without
`simulate_on_commit`, creating an organization makes a hybrid-cloud RPC that the
test transaction guard rejects.
"""

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import responses

pytest_plugins = ["sentry.testutils.pytest"]
# The Sentry image ships a top-level `tests` package, so share git_fixtures by path.
sys.path.append(str(Path(__file__).parents[1] / "unit"))

# Sentry 26.10 removed the legacy plugin API (`sentry.plugins.bases`), so the plugins cannot load.
LEGACY_PLUGIN_TESTS = ["test_alerts_plugin.py", "test_issue_plugin.py", "test_plugin_smoke.py"]
collect_ignore = [] if importlib.util.find_spec("sentry.plugins.bases") else LEGACY_PLUGIN_TESTS


@pytest.hookimpl(tryfirst=True)
def pytest_configure() -> None:
    # Leave Django uninitialized until Sentry's own pytest_configure runs.
    os.environ.setdefault("_SENTRY_SKIP_CONFIGURATION", "1")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sentry.conf.server")
    from django.conf import settings

    settings.SENTRY_DEFAULT_INTEGRATIONS += (
        "sentry_gitea.integration.provider.GiteaIntegrationProvider",
    )
    # Sentry allowlists its own SCM search endpoints (GitLab, GitHub, Jira...) from
    # the DEBUG-only pagination check; ours returns the same short choice list.
    settings.SENTRY_API_PAGINATION_ALLOWLIST = set(settings.SENTRY_API_PAGINATION_ALLOWLIST) | {
        "GiteaSearchEndpoint"
    }


# Adapted from sentry 24.10.0: tests/conftest.py (validate_silo_mode)
@pytest.fixture(autouse=True)
def validate_silo_mode() -> Iterator[None]:
    from sentry.silo.base import SiloMode
    from sentry.testutils.pytest.sentry import get_default_silo_mode_for_test_cases

    expected = get_default_silo_mode_for_test_cases()
    message = f"SiloMode leaked between tests; expected {expected}."
    if SiloMode.get_current_mode() != expected:
        raise AssertionError(message)
    yield
    if SiloMode.get_current_mode() != expected:
        raise AssertionError(message)


# Adapted from sentry 24.10.0: tests/conftest.py (setup_simulate_on_commit)
@pytest.fixture(autouse=True)
def setup_simulate_on_commit(request: pytest.FixtureRequest) -> Iterator[None]:
    from sentry.testutils.hybrid_cloud import simulate_on_commit

    with simulate_on_commit(request):
        yield


# Adapted from sentry 24.10.0: tests/conftest.py (setup_enforce_monotonic_transactions)
@pytest.fixture(autouse=True)
def setup_enforce_monotonic_transactions() -> Iterator[None]:
    from sentry.testutils.hybrid_cloud import enforce_no_cross_transaction_interactions

    with enforce_no_cross_transaction_interactions():
        yield


# Adapted from sentry 24.10.0: tests/conftest.py (clear_caches)
@pytest.fixture(autouse=True)
def clear_caches() -> Iterator[None]:
    yield
    from django.core.cache import cache

    cache.clear()


# Adapted from sentry 24.10.0: tests/conftest.py (check_leaked_responses_mocks)
@pytest.fixture(autouse=True)
def check_leaked_responses_mocks() -> Iterator[None]:
    yield
    leaked = responses.registered()
    if leaked:
        responses.reset()
        listing = "".join(f"- {item}\n" for item in leaked)
        raise AssertionError(f"`responses` mocks leaked outside their test:\n{listing}")
