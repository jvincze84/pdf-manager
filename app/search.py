"""Translate a user search string into an SQLite FTS5 MATCH expression.

Supported syntax:
    invoice 2024          -> documents containing ALL words (prefix match: "invoice" also finds "invoices")
    "exact phrase here"   -> documents containing exactly this phrase
    -draft                -> exclude documents containing this word
    -"some phrase"        -> exclude documents containing this phrase
    word1 OR word2        -> either word
"""
import re

_TOKEN_RE = re.compile(r'(-?)"([^"]*)"?|(\S+)')
# characters that unicode61 treats as separators anyway; stripping them avoids empty tokens
_WORD_CLEAN_RE = re.compile(r"[^\w]+", re.UNICODE)


def _quote(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def build_match(query: str) -> str | None:
    positives: list[str] = []
    negatives: list[str] = []
    pending_or = False

    for m in _TOKEN_RE.finditer(query or ""):
        neg, phrase, word = m.group(1), m.group(2), m.group(3)
        if phrase is not None:
            phrase = phrase.strip()
            if not _WORD_CLEAN_RE.sub("", phrase):
                continue
            term = _quote(phrase)
        else:
            if word == "OR":
                if positives:
                    pending_or = True
                continue
            neg = ""
            if word.startswith("-") and len(word) > 1:
                neg, word = "-", word[1:]
            parts = [p for p in _WORD_CLEAN_RE.split(word) if p]
            if not parts:
                continue
            # "e-mail" / "2024.01.05" -> phrase of its parts; last part prefix-matched
            term = _quote(" ".join(parts)) + "*"

        if neg:
            negatives.append(term)
            continue
        if pending_or and positives:
            positives[-1] = f"({positives[-1]} OR {term})"
            pending_or = False
        else:
            positives.append(term)

    if not positives:
        return None
    expr = " AND ".join(positives)
    if negatives:
        expr = f"({expr}) NOT ({' OR '.join(negatives)})"
    return expr
