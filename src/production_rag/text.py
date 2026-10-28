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


def content_tokens(text: str) -> list[str]:
    """Tokens with stopwords removed; falls back to all tokens if nothing would remain."""
    tokens = tokenize(text)
    content = [t for t in tokens if t not in STOPWORDS]
    return content or tokens
