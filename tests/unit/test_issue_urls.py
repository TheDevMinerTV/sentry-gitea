import pytest

from sentry_gitea.api.client import browser_issue_url, owner_domain_name


class TestBrowserIssueUrl:
    @pytest.mark.parametrize("public_url", [None, "", "http://gitea:3000/"])
    def test_defaults_to_base_url(self, public_url):
        assert (
            browser_issue_url("http://gitea:3000/", "owner/repo", 7, public_url=public_url)
            == "http://gitea:3000/owner/repo/issues/7"
        )

    def test_public_url_replaces_api_host_and_subpath(self):
        assert (
            browser_issue_url(
                "http://gitea:3000/internal",
                "owner/repo",
                "7",
                public_url=" https://PUBLIC.example/forge/ ",
            )
            == "https://public.example/forge/owner/repo/issues/7"
        )

    def test_quotes_repository_segments_once(self):
        assert (
            browser_issue_url("https://forge.example/git/", "équipe/répo", 7)
            == "https://forge.example/git/%C3%A9quipe/r%C3%A9po/issues/7"
        )

    @pytest.mark.parametrize(
        "public_url",
        [
            "ftp://forge.example",
            "https://user:pass@forge.example",
            "https://forge.example?x=1",
            "https://forge.example#frag",
            "https:///missing",
            "https://forge.example/%2e%2e/api",
            "https://forge.example\\@elsewhere",
            "https://forge.example\n",
            "   ",
            False,
        ],
    )
    def test_public_url_uses_base_url_validation(self, public_url):
        with pytest.raises(ValueError):
            browser_issue_url("http://gitea:3000", "owner/repo", 7, public_url=public_url)

    @pytest.mark.parametrize(
        ("repo", "number"), [("owner/repo/extra", 7), ("owner/..", 7), ("owner/repo", "7/other")]
    )
    def test_rejects_invalid_issue_paths(self, repo, number):
        with pytest.raises(ValueError):
            browser_issue_url("https://forge.example", repo, number)


def test_owner_domain_name_drops_only_the_https_scheme():
    assert owner_domain_name("https://gitea.example/", "team") == "gitea.example/team"
    assert owner_domain_name("https://Gitea.Example:8443/git/", "team") == (
        "gitea.example:8443/git/team"
    )
    # Sentry would link a scheme-less name over HTTPS, which a plain-HTTP Gitea does not serve.
    assert owner_domain_name("http://gitea:3000", "team") == "http://gitea:3000/team"
    for login in ["", "a/b", "../x"]:
        with pytest.raises(ValueError):
            owner_domain_name("https://gitea.example", login)
