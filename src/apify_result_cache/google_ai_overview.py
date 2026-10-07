"""Key normalisation for the `google-ai-overview` namespace.

The one place that knows what identifies a Google AI Overview request, so
every Actor that reads or writes this namespace hashes the same request the
same way. A request recorded one way must look up the same way, or cached
rows become unreachable.

Not in the key, on purpose: anything derived at serve time from the stored
overview (for example a caller's own domains, used to classify citations).
"""

from __future__ import annotations

import re

DEFAULT_GL = "us"
DEFAULT_HL = "en"
_WHITESPACE = re.compile(r"\s+")


def ai_overview_key_fields(
    *,
    query: str,
    gl: str | None,
    hl: str | None,
    location: str | None,
) -> dict[str, object]:
    """The identifying fields of one AI Overview request, normalised.

    - `query`: stripped, internal whitespace collapsed to one space, and
      casefolded. Google answers a query the same way regardless of case, so
      "What is RAG" and "what is rag" share one cached copy.
    - `gl` / `hl`: stripped and lower-cased; blank -> "us" / "en", the
      defaults the Actor applies before fetching.
    - `location`: stripped with whitespace collapsed; blank -> None. Case is
      kept: it is passed to the source verbatim.

    Raises ValueError on an empty query.
    """
    text = _WHITESPACE.sub(" ", (query or "").strip()).casefold()
    if not text:
        raise ValueError("query is required")

    country = (gl or "").strip().lower() or DEFAULT_GL
    language = (hl or "").strip().lower() or DEFAULT_HL
    place = _WHITESPACE.sub(" ", (location or "").strip()) or None

    return {
        "query": text,
        "gl": country,
        "hl": language,
        "location": place,
    }
