from openagentsearch.fetch.robots import RobotsPolicy

FIXTURE = """
User-agent: openagentsearch
Disallow: /private/

User-agent: *
Disallow: /
"""


def test_named_agent_gets_its_own_rules():
    policy = RobotsPolicy(FIXTURE, user_agent="openagentsearch")
    assert policy.is_allowed("https://docs.python.org/3/") is True
    assert policy.is_allowed("https://docs.python.org/private/x") is False


def test_other_agents_fall_back_to_wildcard_rules():
    policy = RobotsPolicy(FIXTURE, user_agent="someone-else")
    assert policy.is_allowed("https://docs.python.org/3/") is False
    assert policy.is_allowed("https://docs.python.org/private/x") is False
