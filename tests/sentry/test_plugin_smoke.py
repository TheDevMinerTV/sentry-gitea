import pytest
from sentry.plugins.base import plugins

from sentry_gitea import __version__
from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL
from sentry_gitea.plugins.alerts import GiteaAlertsPlugin
from sentry_gitea.plugins.issue import GiteaIssuePlugin


@pytest.mark.parametrize(
    ("slug", "plugin_class"), [("gitea", GiteaIssuePlugin), ("gitea_alerts", GiteaAlertsPlugin)]
)
def test_plugins_discovered_from_installed_entry_points(slug, plugin_class) -> None:
    plugin = plugins.get(slug)
    assert isinstance(plugin, plugin_class)
    assert plugin.version == __version__ == "0.2.0"
    assert (plugin.author, plugin.author_url) == (AUTHOR, PROJECT_URL)
    assert ("Source", PROJECT_URL) in plugin.resource_links
    assert ("Bug Tracker", ISSUES_URL) in plugin.resource_links
    assert plugin.is_configured(None) is False
    assert plugin.get_conf_key() == slug
