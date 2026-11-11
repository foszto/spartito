"""The installable page (manifest, icons, head tags) and the server's identity (/api/ping)."""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orchestrator import __version__, config
from orchestrator.web.app import app

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "orchestrator" / "web" / "static"
ICNS = ROOT / "src" / "orchestrator" / "gui" / "icon.icns"
SCRIPT = ROOT / "scripts" / "make_icons.py"
ICON_FILES = ("icon.svg", "icon-192.png", "icon-512.png", "icon-maskable-512.png")
ICON_FILES += ("icon-apple-180.png",)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


def icns_elements(data: bytes) -> dict[str, bytes]:
    """The elements of an ICNS file by type; fails on a broken one."""
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data)
    pos, elements = 8, {}
    while pos < len(data):
        kind, (length,) = (
            data[pos : pos + 4].decode("ascii"),
            struct.unpack(">I", data[pos + 4 : pos + 8]),
        )
        elements[kind] = data[pos + 8 : pos + length]
        pos += length
    assert pos == len(data)
    return elements


def png_info(data: bytes) -> tuple[int, int, bytes]:
    """Width, height and the decompressed image data of a PNG; fails on a broken one."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert zlib.crc32(kind + body) == crc, kind
        chunks.append((kind, body))
        pos += 12 + length
    kinds = [kind for kind, _ in chunks]
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
    assert not {b"tEXt", b"zTXt", b"iTXt", b"tIME"} & set(kinds)  # nothing personal inside
    width, height, depth, colour = struct.unpack(">IIBB", chunks[0][1][:10])
    assert depth == 8 and colour in (2, 6)
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    assert len(raw) == height * (1 + width * (4 if colour == 6 else 3))
    return width, height, raw


class Head(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[dict[str, str]] = []
        self.inside = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.inside = self.inside or tag == "head"
        if self.inside and tag in ("link", "meta"):
            self.tags.append({"tag": tag, **{k: v or "" for k, v in attrs}})

    def handle_endtag(self, tag: str) -> None:
        self.inside = self.inside and tag != "head"


def head_tags(client: TestClient) -> list[dict[str, str]]:
    parser = Head()
    parser.feed(client.get("/").text)
    return parser.tags


def test_the_manifest_makes_the_page_installable(client: TestClient) -> None:
    response = client.get("/manifest.webmanifest")
    data = response.json()

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "application/manifest+json"
    assert data["name"] == data["short_name"] == config.PROJECT_NAME
    assert data["id"] == data["start_url"] == data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["theme_color"] == data["background_color"] == "#f4f5f8"
    assert "prefer_related_applications" not in data
    icons = {(icon["sizes"], icon["purpose"]): icon for icon in data["icons"]}
    assert set(icons) == {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")}


def test_every_icon_the_manifest_and_the_head_name_is_served_at_its_size(
    client: TestClient,
) -> None:
    named = [(i["src"], i["sizes"]) for i in client.get("/manifest.webmanifest").json()["icons"]]
    links = [t for t in head_tags(client) if t["tag"] == "link" and "icon" in t["rel"]]
    named += [(t["href"], t.get("sizes")) for t in links]

    assert {src.rsplit("/", 1)[1] for src, _ in named} == set(ICON_FILES)
    for src, sizes in named:
        response = client.get(src)
        assert response.status_code == 200, src
        assert response.content == (STATIC / src.rsplit("/", 1)[1]).read_bytes()
        if src.endswith(".svg"):
            assert response.headers["content-type"].startswith("image/svg+xml")
            assert response.text.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
            continue
        assert response.headers["content-type"] == "image/png"
        width, height, _ = png_info(response.content)
        assert f"{width}x{height}" == sizes, src


def test_the_head_links_the_manifest_icons_title_and_theme(client: TestClient) -> None:
    tags = head_tags(client)
    links = {t["rel"]: t["href"] for t in tags if t["tag"] == "link" and t["rel"] != "icon"}
    icons = {t["type"]: t["href"] for t in tags if t["tag"] == "link" and t["rel"] == "icon"}
    metas = [t for t in tags if t["tag"] == "meta" and "name" in t]
    named = {t["name"]: t["content"] for t in metas}
    themes = {t["media"]: t["content"] for t in metas if t["name"] == "theme-color"}

    assert links["manifest"] == "/manifest.webmanifest"
    assert links["apple-touch-icon"] == "/static/icon-apple-180.png"
    assert icons == {"image/svg+xml": "/static/icon.svg", "image/png": "/static/icon-192.png"}
    assert named["apple-mobile-web-app-title"] == config.PROJECT_NAME
    assert named["application-name"] == config.PROJECT_NAME
    assert themes == {
        "(prefers-color-scheme: light)": "#f4f5f8",
        "(prefers-color-scheme: dark)": "#0f1116",
    }
    assert f"<title>{config.PROJECT_NAME}</title>" in client.get("/").text


def test_ping_names_the_tool_and_a_digest_of_its_home(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = client.get("/api/ping")
    home = str(config.home().resolve())
    monkeypatch.setenv("ORCH_HOME", str(tmp_path / "other"))
    second = client.get("/api/ping").json()

    assert first.status_code == 200
    assert first.json() == {
        "tool": "orchestrator",
        "version": __version__,
        "home": hashlib.sha256(home.encode()).hexdigest()[:16],
    }
    assert second["home"] != first.json()["home"] and len(second["home"]) == 16
    assert home not in first.text and str(tmp_path) not in json.dumps(second)
    assert not (Path(home) / "orch.db").exists()  # no store opened


def test_ping_reports_the_package_version() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    assert f'\nversion = "{__version__}"\n' in project


@pytest.mark.parametrize(
    "path", ["/manifest.webmanifest", "/api/ping", "/static/icon.svg", "/static/icon-192.png"]
)
def test_the_new_routes_refuse_a_foreign_host_and_send_no_cors_headers(
    client: TestClient, path: str
) -> None:
    refused = client.get(path, headers={"host": "evil.example"})
    allowed = client.get(path, headers={"origin": "http://evil.example"})

    assert (
        refused.status_code
        == client.get("/api/state", headers={"host": "evil.example"}).status_code
    )
    assert refused.status_code == 403 and refused.json() == {"error": "unexpected Host header"}
    assert allowed.status_code == 200
    assert not [h for h in allowed.headers if h.lower().startswith("access-control-")]
    assert client.post(path, json={}, headers={"origin": "http://evil.example"}).status_code == 403


def test_mac_app_icon_holds_every_size_as_png() -> None:
    sizes = {kind: png_info(data)[:2] for kind, data in icns_elements(ICNS.read_bytes()).items()}
    assert sizes == {
        "icp4": (16, 16),
        "icp5": (32, 32),
        "ic11": (32, 32),
        "ic12": (64, 64),
        "ic07": (128, 128),
        "ic13": (256, 256),
        "ic08": (256, 256),
        "ic14": (512, 512),
        "ic09": (512, 512),
        "ic10": (1024, 1024),
    }


def test_make_icons_reproduces_the_committed_files(tmp_path: Path) -> None:
    """Two runs give the same bytes; their images are the committed ones. (Bytes against the
    committed files only where zlib compresses as it did here: another zlib build may pack the
    same pixels differently.)"""
    outputs = []
    for run in ("one", "two"):
        out = tmp_path / run
        subprocess.run([sys.executable, str(SCRIPT), str(out)], check=True, capture_output=True)
        outputs.append({path.name: path.read_bytes() for path in out.iterdir()})

    assert outputs[0] == outputs[1]
    assert set(outputs[0]) == {*ICON_FILES, ICNS.name}
    for name, data in outputs[0].items():
        committed = (ICNS if name == ICNS.name else STATIC / name).read_bytes()
        if name.endswith(".icns"):
            mine, theirs = icns_elements(data), icns_elements(committed)
            assert list(mine) == list(theirs), name
            assert [png_info(d) for d in mine.values()] == [png_info(d) for d in theirs.values()]
        elif name.endswith(".svg"):
            assert data == committed
        else:
            assert png_info(data) == png_info(committed), name
            _, _, raw = png_info(committed)
            if zlib.compress(raw, 9) in committed:
                assert data == committed, name
