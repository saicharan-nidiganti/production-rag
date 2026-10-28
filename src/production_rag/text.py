"""Shared tokenization used by the hashing embedder, lexical query building and evaluation."""

from __future__ import annotations

import re

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Small, conservative English stopword list: enough to keep function words from
# dominating bag-of-words similarity without removing domain terms.
_STOPWORD_TEXT = """
a an and are as at be been but by can could did do does for from had has have how i if in
into is it its may me my of on or our should so than that the their them then there these
they this those to was we were what when where which who whom why will with would you your
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _is_content(token: str) -> bool:
    # Single letters are fragments such as the "s" of "what's" or "customer's", which
    # matched unrelated text. Single digits ("5 minutes") carry meaning and are kept.
    return token not in STOPWORDS and (len(token) > 1 or token.isdigit())


def content_tokens(text: str) -> list[str]:
    """Tokens without stopwords or single letters; falls back to all tokens if none remain."""
    tokens = tokenize(text)
    content = [t for t in tokens if _is_content(t)]
    return content or tokens
