from types import SimpleNamespace

import pytest


def test_latest_release_version_uses_published_release_tag_not_repository_files():
    from app.services.release_status import read_latest_release_version

    class Session:
        headers = {}

        def get(self, url, **kwargs):
            assert url.endswith("/releases/latest")
            assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"tag_name": "v0.13.0", "draft": False, "prerelease": False},
            )

    result = read_latest_release_version(Session(), proxies={"https": "http://proxy.test"})

    assert result["version"] == "0.13.0"
    assert result["source"] == "github_release"


@pytest.mark.parametrize("tag_name", ["", "main", "vnext", "v0.13.0/unsafe"])
def test_latest_release_version_rejects_non_release_version_tags(tag_name):
    from app.services.release_status import read_latest_release_version

    class Session:
        headers = {}

        def get(self, _url, **_kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"tag_name": tag_name, "draft": False, "prerelease": False},
            )

    with pytest.raises(ValueError, match="发布标签"):
        read_latest_release_version(Session())
