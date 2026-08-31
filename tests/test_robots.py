import pytest
from openagentsearch.fetch.robots import RobotsPolicy


@pytest.fixture
def robots_txt():
    return """User-agent: *
Disallow: /private/
Allow: /
"""


def test_is_allowed_true(robots_txt):
    policy = RobotsPolicy(robots_txt)
    assert policy.is_allowed("https://docs.python.org/3/") is True


def test_is_allowed_false(robots_txt):
    policy = RobotsPolicy(robots_txt)
    assert policy.is_allowed("https://docs.python.org/private/x") is False