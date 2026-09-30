"""The key contract. This hash is the wire format between Phase 0 (recording
keys) and Phase 1 (serving by key): if it drifts, the measured hit rate means
nothing and every cached row becomes unreachable."""

from __future__ import annotations

import hashlib

import pytest

from apify_result_cache import ResultCache, youtube_key_fields

NS = "youtube-transcript"
GOLDEN_CANONICAL = (
    '{"languages":["en"],"ns":"youtube-transcript","preserve_formatting":false,'
    '"transcript_type":"any","translate_to":null,"v":1,"video_id":"dQw4w9WgXcQ"}'
)
GOLDEN = "d097217330f8268b66ec640f7c1a3523b88ccb7f2663f893bb6fcc7b5962fcb4"


@pytest.fixture
async def cache(actor, db, keys_env):
    return await ResultCache.start(namespace=NS, schema_version=1)


def yk(**over):
    base = dict(video_id="dQw4w9WgXcQ", languages=None, transcript_type="any",
                translate_to=None, preserve_formatting=False)
    base.update(over)
    return youtube_key_fields(**base)


def test_golden_literal_matches_the_pinned_digest():
    assert hashlib.sha256(GOLDEN_CANONICAL.encode()).hexdigest() == GOLDEN


async def test_golden_hash(cache):
    assert cache.key(yk()) == GOLDEN


async def test_field_order_is_irrelevant(cache):
    fields = yk()
    assert cache.key(dict(reversed(list(fields.items())))) == GOLDEN


async def test_instance_ns_and_v_win_over_the_caller(cache):
    assert cache.key({**yk(), "ns": "evil", "v": 99}) == GOLDEN


async def test_schema_version_changes_the_key(actor, db, keys_env):
    v2 = await ResultCache.start(namespace=NS, schema_version=2)
    assert v2.key(yk()) != GOLDEN


async def test_namespace_changes_the_key(actor, db, keys_env):
    other = await ResultCache.start(namespace="other-thing", schema_version=1)
    assert other.key(yk()) != GOLDEN


@pytest.mark.parametrize("langs", [None, [], [" "], ["", "  "], "", "en", ["en"], [" en "]])
async def test_default_languages_normalise_to_en(cache, langs):
    assert cache.key(yk(languages=langs)) == GOLDEN


async def test_language_order_matters(cache):
    assert cache.key(yk(languages=["en", "de"])) != cache.key(yk(languages=["de", "en"]))


async def test_language_case_is_preserved(cache):
    assert cache.key(yk(languages=["zh-Hans"])) != cache.key(yk(languages=["zh-hans"]))


async def test_comma_string_languages(cache):
    assert cache.key(yk(languages="en, de")) == cache.key(yk(languages=["en", "de"]))


async def test_preserve_formatting_changes_the_key(cache):
    assert cache.key(yk(preserve_formatting=True)) != GOLDEN


@pytest.mark.parametrize("blank", ["", "  ", None])
async def test_blank_translate_to_is_none(cache, blank):
    assert cache.key(yk(translate_to=blank)) == GOLDEN


async def test_translate_to_changes_the_key(cache):
    assert cache.key(yk(translate_to="es")) != GOLDEN


@pytest.mark.parametrize("kind, same_as", [("MANUAL", "manual"), (" Generated ", "generated"),
                                           ("bogus", "any"), (None, "any")])
async def test_transcript_type_normalisation(cache, kind, same_as):
    assert cache.key(yk(transcript_type=kind)) == cache.key(yk(transcript_type=same_as))


def test_output_formats_and_metadata_cannot_enter_the_key():
    with pytest.raises(TypeError):
        youtube_key_fields(video_id="x", languages=None, transcript_type="any", translate_to=None,
                           preserve_formatting=False, output_formats=["srt"])  # type: ignore[call-arg]


def test_video_id_is_required():
    with pytest.raises(ValueError):
        yk(video_id="  ")
