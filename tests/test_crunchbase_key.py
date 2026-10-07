"""The `crunchbase-company` and `crunchbase-lookup` key contracts. If either
hash drifts, every cached company becomes unreachable, so the canonical forms
are pinned literally."""

from __future__ import annotations

import hashlib

import pytest

from apify_result_cache import (
    ResultCache,
    crunchbase_company_key_fields,
    crunchbase_lookup_key_fields,
)

COMPANY_NS = "crunchbase-company"
LOOKUP_NS = "crunchbase-lookup"
COMPANY_CANONICAL = '{"ns":"crunchbase-company","slug":"openai","v":1}'
COMPANY_GOLDEN = "a44f7c7ca3d62b2bb51882da59a8cd842f2b2cbcecab418d426b2afbca18ce86"
LOOKUP_CANONICAL = '{"ns":"crunchbase-lookup","term":"basecamp.com","v":1}'
LOOKUP_GOLDEN = "043c98f391984ef3ca3ab583cfac5ac14cbf3b92c765676fcad3dff7894e7b81"


@pytest.fixture
async def company_cache(actor, db, keys_env):
    return await ResultCache.start(namespace=COMPANY_NS, schema_version=1)


@pytest.fixture
async def lookup_cache(actor, db, keys_env):
    return await ResultCache.start(namespace=LOOKUP_NS, schema_version=1)


def test_golden_literals_match_the_pinned_digests():
    assert hashlib.sha256(COMPANY_CANONICAL.encode()).hexdigest() == COMPANY_GOLDEN
    assert hashlib.sha256(LOOKUP_CANONICAL.encode()).hexdigest() == LOOKUP_GOLDEN


async def test_company_golden_hash(company_cache):
    assert company_cache.key(crunchbase_company_key_fields(url_or_slug="openai")) == COMPANY_GOLDEN


async def test_lookup_golden_hash(lookup_cache):
    assert lookup_cache.key(crunchbase_lookup_key_fields(term="basecamp.com")) == LOOKUP_GOLDEN


@pytest.mark.parametrize("value", [
    "openai", "OpenAI", " openai ", "OPENAI",
    "https://www.crunchbase.com/organization/openai",
    "https://crunchbase.com/organization/openai",
    "http://www.crunchbase.com/organization/OpenAI/",
    "https://www.crunchbase.com/organization/openai?utm_source=x#people",
    "HTTPS://WWW.CRUNCHBASE.COM/ORGANIZATION/OPENAI",
])
async def test_url_slug_case_and_decoration_share_one_company_key(company_cache, value):
    assert company_cache.key(crunchbase_company_key_fields(url_or_slug=value)) == COMPANY_GOLDEN


@pytest.mark.parametrize("term", ["basecamp.com", " basecamp.com ", "Basecamp.COM", "BASECAMP.COM"])
async def test_term_whitespace_and_case_share_one_lookup_key(lookup_cache, term):
    assert lookup_cache.key(crunchbase_lookup_key_fields(term=term)) == LOOKUP_GOLDEN


async def test_different_slugs_and_terms_change_the_key(company_cache, lookup_cache):
    assert company_cache.key(crunchbase_company_key_fields(url_or_slug="openai-2")) != COMPANY_GOLDEN
    assert company_cache.key(crunchbase_company_key_fields(
        url_or_slug="https://www.crunchbase.com/organization/anthropic")) != COMPANY_GOLDEN
    assert lookup_cache.key(crunchbase_lookup_key_fields(term="basecamp")) != LOOKUP_GOLDEN
    assert lookup_cache.key(crunchbase_lookup_key_fields(term="Basecamp Inc.")) != LOOKUP_GOLDEN


async def test_namespaces_do_not_collide(company_cache, lookup_cache):
    # Same identifying text, different namespace: different key.
    assert company_cache.key({"slug": "openai"}) != lookup_cache.key({"term": "openai"})


def test_internal_whitespace_collapses_in_terms():
    assert crunchbase_lookup_key_fields(term="  Mercado   Libre\t")["term"] == "mercado libre"


def test_non_organization_url_is_treated_as_a_slug_string():
    # A person or investor URL has no /organization/ segment; the whole string
    # becomes the slug so it can never alias an organization page.
    fields = crunchbase_company_key_fields(url_or_slug="https://www.crunchbase.com/person/sam-altman")
    assert fields["slug"] == "https://www.crunchbase.com/person/sam-altman"


@pytest.mark.parametrize("value", ["", "   ", None, "https://www.crunchbase.com/organization/"])
def test_empty_company_input_raises(value):
    with pytest.raises(ValueError):
        crunchbase_company_key_fields(url_or_slug=value)


@pytest.mark.parametrize("term", ["", "   ", None])
def test_empty_term_raises(term):
    with pytest.raises(ValueError):
        crunchbase_lookup_key_fields(term=term)


def test_fields_are_exactly_the_documented_sets():
    assert set(crunchbase_company_key_fields(url_or_slug="openai")) == {"slug"}
    assert set(crunchbase_lookup_key_fields(term="openai")) == {"term"}
