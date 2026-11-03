"""Sentry's issue-tracker menu shows an installation's `domain_name` under the provider name."""

from unittest.mock import patch

from sentry.integrations.models.integration import Integration
from sentry.signals import post_upgrade
from sentry.silo.base import SiloMode
from sentry.testutils.silo import no_silo_test
from test_integration_provider import METADATA, IntegrationFixture


@no_silo_test
class TestDomainName(IntegrationFixture):
    def test_upgrade_gives_existing_installations_a_domain_name_once(self):
        assert "domain_name" not in self.integration.metadata
        plain = self.create_integration(
            organization=self.organization,
            provider="gitea",
            external_id="plain-http",
            name="other",
            metadata={**METADATA, "public_url": "http://gitea.internal:3000", "owner_login": "o"},
        )
        kept = self.create_integration(
            organization=self.organization,
            provider="gitea",
            external_id="kept",
            name="kept",
            metadata={**METADATA, "domain_name": "chosen.example/kept"},
        )
        github = self.create_integration(
            organization=self.organization, provider="github", external_id="gh", metadata={}
        )

        for _ in range(2):
            post_upgrade.send(sender=SiloMode.MONOLITH, interactive=False)

        def domain(integration):
            return Integration.objects.get(id=integration.id).metadata.get("domain_name")

        assert domain(self.integration) == "public-forge.example/git/team"
        assert domain(plain) == "http://gitea.internal:3000/o"
        assert domain(kept) == "chosen.example/kept"
        assert domain(github) is None
        assert (
            Integration.objects.get(id=self.integration.id).metadata["webhook_secret"]
            == (METADATA["webhook_secret"])
        )
        with self.feature("organizations:integrations-issue-basic"):
            menu = self.client.get(
                f"/api/0/organizations/{self.organization.slug}/issues/{self.group.id}/integrations/"
            )
        assert menu.status_code == 200, menu.data
        [row] = [row for row in menu.data if row["id"] == str(self.integration.id)]
        assert row["domainName"] == "public-forge.example/git/team"

    def test_a_failing_backfill_does_not_fail_the_upgrade(self):
        with (
            patch("sentry_gitea.integration.provider.owner_domain_name", side_effect=RuntimeError),
            self.assertLogs("sentry_gitea.integration.provider", level="WARNING") as logs,
        ):
            post_upgrade.send(sender=SiloMode.MONOLITH, interactive=False)
        assert "domain name backfill failed" in logs.output[0]
        assert "domain_name" not in Integration.objects.get(id=self.integration.id).metadata
