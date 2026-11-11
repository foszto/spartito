"""Short text renderings shared by the layers."""

from __future__ import annotations


def kilo(tokens: int | None) -> str:
    if tokens is None:
        return "?"
    return f"{tokens / 1000:.0f}k" if tokens >= 1000 else str(tokens)
