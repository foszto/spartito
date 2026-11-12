"""The board view's front end: its two static files are served, and the page loads them so
that board.js only defines `BoardView` before app.js, which uses it, runs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orchestrator.web.app import app

STATIC = Path(__file__).resolve().parent.parent / "src" / "orchestrator" / "web" / "static"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.mark.parametrize("name", ["board.js", "board.css"])
def test_the_board_files_are_served(client: TestClient, name: str) -> None:
    response = client.get(f"/static/{name}")

    assert response.status_code == 200
    assert response.content == (STATIC / name).read_bytes()


def test_the_page_loads_board_js_before_app_js_and_its_stylesheet(client: TestClient) -> None:
    page = client.get("/").text
    scripts = re.findall(r'<script src="([^"]+)"></script>', page)
    sheets = re.findall(r'<link rel="stylesheet" href="([^"]+)">', page)

    assert scripts == ["/static/board.js", "/static/app.js"]
    assert sheets == ["/static/app.css", "/static/board.css"]


def test_board_js_exports_only_the_board_view() -> None:
    """One IIFE: no top-level name of its own clashes with app.js (a classic script shares
    the page's global scope)."""
    source = (STATIC / "board.js").read_text()
    top_level = re.findall(r"^(?:const|let|var|function|class)\s", source, re.MULTILINE)

    assert top_level == []
    assert "window.BoardView = { ROUTE, owns, render, navEntry, messageAction };" in source
