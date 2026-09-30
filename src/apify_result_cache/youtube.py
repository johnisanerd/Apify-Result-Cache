"""Key normalisation for the `youtube-transcript` namespace.

The core library is namespace-agnostic; this module is the one place that
knows what identifies a YouTube transcript request. It exists so Phase 0
(recording keys) and Phase 1 (serving by key) cannot drift: a request that
was recorded one way must look up the same way, or the measured hit rate is
fiction.

Not in the key, on purpose: `output_formats` (srt/vtt/text are derived from
the stored snippets at serve time), `include_metadata` and
`include_extended_metadata` (metadata is joined at serve time).
"""

from __future__ import annotations

from typing import Iterable

DEFAULT_LANGUAGES = ("en",)
TRANSCRIPT_TYPES = ("any", "manual", "generated")


def youtube_key_fields(
    *,
    video_id: str,
    languages: Iterable[str] | str | None,
    transcript_type: str | None,
    translate_to: str | None,
    preserve_formatting: bool,
) -> dict[str, object]:
    """The identifying fields of one transcript request, normalised.

    - `languages`: None, empty, or blank entries -> `["en"]` (the fetch
      default). Order and case are preserved: order is the fallback
      preference and codes like `zh-Hans` are case-significant.
    - `transcript_type`: lower-cased; anything not any|manual|generated -> `any`.
    - `translate_to`: stripped; blank -> None.
    - `preserve_formatting`: coerced to bool (it changes the snippet text).
    """
    vid = (video_id or "").strip()
    if not vid:
        raise ValueError("video_id is required")

    if isinstance(languages, str):
        raw_langs: Iterable[str] = languages.split(",")
    else:
        raw_langs = languages or ()
    langs = [str(code).strip() for code in raw_langs if str(code).strip()]
    if not langs:
        langs = list(DEFAULT_LANGUAGES)

    kind = (transcript_type or "any").strip().lower()
    if kind not in TRANSCRIPT_TYPES:
        kind = "any"

    target = (translate_to or "").strip() or None

    return {
        "video_id": vid,
        "languages": langs,
        "transcript_type": kind,
        "translate_to": target,
        "preserve_formatting": bool(preserve_formatting),
    }
