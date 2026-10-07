"""Key normalisation for the `crunchbase-company` and `crunchbase-lookup`
namespaces.

The one place that knows what identifies a Crunchbase company request, so
every Actor that reads or writes these namespaces hashes the same request the
same way. A request recorded one way must look up the same way, or cached
rows become unreachable.

Two namespaces, two keys:

- `crunchbase-company`: one organization page, identified by its slug (the
  last path segment of `https://www.crunchbase.com/organization/<slug>`).
  The input may be the full URL or the bare slug; the host, scheme, `www.`,
  trailing slash, query string and fragment are not part of the key.
- `crunchbase-lookup`: one name or website-domain lookup term, as the caller
  typed it, normalised for whitespace and case. The payload is the slug the
  term resolved to; the company page itself lives in the other namespace.

Not in the key, on purpose: anything derived at serve time (output formats,
the caller's own join fields such as `searchTerm`).
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")
_ORG_PATH = re.compile(r"/organization/([^/?#]*)", re.IGNORECASE)


def _norm(text: str | None) -> str:
    return _WHITESPACE.sub(" ", (text or "").strip()).casefold()


def crunchbase_company_key_fields(*, url_or_slug: str) -> dict[str, object]:
    """The identifying fields of one organization page, normalised.

    - A URL containing `/organization/<slug>` contributes only the slug; the
      rest of the URL is ignored.
    - A bare slug is used as is.
    - Whitespace is collapsed and the slug is casefolded, matching the site,
      which serves `/organization/Stripe` and `/organization/stripe` as one page.

    Raises ValueError on an empty input.
    """
    raw = (url_or_slug or "").strip()
    m = _ORG_PATH.search(raw)
    slug = _norm(m.group(1) if m else raw)
    if not slug:
        raise ValueError("url_or_slug is required")
    return {"slug": slug}


def crunchbase_lookup_key_fields(*, term: str) -> dict[str, object]:
    """The identifying fields of one name or domain lookup, normalised.

    - `term`: stripped, internal whitespace collapsed to one space, and
      casefolded. "OpenAI", " openai " and "OPENAI" share one cached
      resolution; "basecamp.com" and "Basecamp" do not (different lookups
      can resolve differently).

    Raises ValueError on an empty term.
    """
    text = _norm(term)
    if not text:
        raise ValueError("term is required")
    return {"term": text}
