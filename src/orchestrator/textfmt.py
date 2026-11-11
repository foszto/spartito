"""Short text renderings shared by the layers: token counts and one-line previews."""

from __future__ import annotations


# Flattens whitespace to single spaces; longer text is cut at `limit` characters and gets an
# ellipsis appended (the result may be `limit + 1` characters long).
def one_line(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


# Like one_line, but the result, ellipsis included, is at most `limit` characters long.
def fit_line(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def kilo(tokens: int | None) -> str:
    if tokens is None:
        return "?"
    return f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)
