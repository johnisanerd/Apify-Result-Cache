"""The `google-ai-overview` key contract. If this hash drifts, every cached AI
Overview becomes unreachable, so the canonical form is pinned literally."""

from __future__ import annotations

import hashlib

import pytest

from apify_result_cache import ResultCache, ai_overview_key_fields

NS = "google-ai-overview"
GOLDEN_CANONICAL = (
    '{"gl":"us","hl":"en","location":null,"ns":"google-ai-overview",'
    '"query":"what is a heat pump","v":1}'
)
GOLDEN = "86a48654b643c07c95ac4efd4456fb29524ff5185eeceb6bd575a5326a7bda7d"


@pytest.fixture
async def cache(actor, db, keys_env):
    return await ResultCache.start(namespace=NS, schema_version=1)


def ak(**over):
    base = dict(query="what is a heat pump", gl="us", hl="en", location=None)
    base.update(over)
    return ai_overview_key_fields(**base)


def test_golden_literal_matches_the_pinned_digest():
    assert hashlib.sha256(GOLDEN_CANONICAL.encode()).hexdigest() == GOLDEN


async def test_golden_hash(cache):
    assert cache.key(ak()) == GOLDEN


@pytest.mark.parametrize("query", [
    "what is a heat pump", "  what is a heat pump  ", "What Is A Heat Pump",
    "what  is\ta heat\npump", "WHAT IS A HEAT PUMP",
])
async def test_query_whitespace_and_case_share_one_key(cache, query):
    assert cache.key(ak(query=query)) == GOLDEN


@pytest.mark.parametrize("gl,hl", [(None, None), ("", ""), ("US", "EN"), (" us ", " en ")])
async def test_locale_defaults_and_case(cache, gl, hl):
    assert cache.key(ak(gl=gl, hl=hl)) == GOLDEN


@pytest.mark.parametrize("location", [None, "", "   "])
async def test_blank_location_is_none(cache, location):
    assert cache.key(ak(location=location)) == GOLDEN


async def test_each_identifying_field_changes_the_key(cache):
    assert cache.key(ak(query="what is a heat pump?")) != GOLDEN
    assert cache.key(ak(gl="gb")) != GOLDEN
    assert cache.key(ak(hl="de")) != GOLDEN
    assert cache.key(ak(location="Austin, Texas")) != GOLDEN


def test_location_keeps_case_and_collapses_whitespace():
    assert ak(location="  Austin,   Texas ")["location"] == "Austin, Texas"


@pytest.mark.parametrize("query", ["", "   ", None])
def test_empty_query_raises(query):
    with pytest.raises(ValueError):
        ai_overview_key_fields(query=query, gl="us", hl="en", location=None)


def test_fields_are_exactly_the_documented_set():
    assert set(ak()) == {"query", "gl", "hl", "location"}
