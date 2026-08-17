"""本文画像の寸法予約テスト。

width/height が無い <img> は読み込み完了まで高さ 0 のままで、本文を読んでいる
最中に画像 1 枚ごとにレイアウトが数百 px 伸びる。WebKit はスクロールアンカリング
非対応なのでスクロール位置が補正されず、読んでいる行が飛ぶ。
抽出時に寸法を埋めることでこれを防ぐ。
"""

from __future__ import annotations

import asyncio
import struct
import zlib

import httpx

import pytest

from app.services import content_extractor
from app.services.content_extractor import (
    _apply_image_sizes,
    _declared_image_sizes,
    _is_probe_allowed,
    _is_public_ip,
    _parse_image_size,
    _probe_image_sizes,
    _unsized_image_srcs,
    _fix_html,
)


def _png(width: int, height: int) -> bytes:
    """最小構成の PNG ヘッダー (IHDR まで) を組み立てる。"""
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 8


def _jpeg(width: int, height: int) -> bytes:
    # APP0 を挟んでから SOF0 を置く (マーカー読み飛ばしを踏ませる)
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
    return b"\xff\xd8" + app0 + sof0 + b"\x01" * 6


def _webp_vp8x(width: int, height: int) -> bytes:
    body = b"WEBPVP8X" + struct.pack("<I", 10) + b"\x00" * 4
    body += struct.pack("<I", width - 1)[:3] + struct.pack("<I", height - 1)[:3]
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_parse_image_size_png() -> None:
    assert _parse_image_size(_png(800, 441)) == (800, 441)


def test_parse_image_size_gif() -> None:
    assert _parse_image_size(_gif(640, 480)) == (640, 480)


def test_parse_image_size_jpeg_skips_app_markers() -> None:
    assert _parse_image_size(_jpeg(1052, 299)) == (1052, 299)


def test_parse_image_size_webp_vp8x() -> None:
    assert _parse_image_size(_webp_vp8x(1200, 630)) == (1200, 630)


def test_parse_image_size_rejects_non_image() -> None:
    assert _parse_image_size(b"<html><body>not an image</body></html>") is None
    assert _parse_image_size(b"") is None


def test_declared_image_sizes_prefers_page_markup() -> None:
    """元ページの表示サイズ宣言を拾う。原寸で描画するとアイコンが巨大化する。"""
    html = (
        '<img class="icon" height="26" src="/img/topic.png" width="26">'
        '<img src="https://cdn.test/photo.jpg" width="700" height="400">'
        '<img src="https://cdn.test/nodim.png">'
    )
    sizes = _declared_image_sizes(html, "https://example.test/article")
    assert sizes == {
        "https://example.test/img/topic.png": (26, 26),
        "https://cdn.test/photo.jpg": (700, 400),
    }


def test_fix_html_restores_declared_size_on_graphic_tags() -> None:
    """trafilatura の <graphic> は寸法を落とすため、宣言値を復元する。"""
    extracted = '<p><graphic src="https://cdn.test/topic.png"/></p>'
    out = _fix_html(
        extracted,
        base_url="https://example.test/a",
        image_sizes={"https://cdn.test/topic.png": (26, 26)},
    )
    assert 'width="26"' in out
    assert 'height="26"' in out


def test_apply_image_sizes_keeps_existing_dimensions() -> None:
    html = '<img src="https://cdn.test/a.png" width="10" height="10">'
    out = _apply_image_sizes(html, {"https://cdn.test/a.png": (800, 600)})
    assert out == html


def test_unsized_image_srcs_skips_svg_and_duplicates() -> None:
    html = (
        '<img src="https://cdn.test/a.png">'
        '<img src="https://cdn.test/a.png">'
        '<img src="https://cdn.test/icon.svg">'
        '<img src="https://cdn.test/b.png?w=1">'
        '<img src="data:image/png;base64,AAAA">'
        '<img src="https://cdn.test/sized.png" width="4" height="4">'
    )
    assert _unsized_image_srcs(html) == [
        "https://cdn.test/a.png",
        "https://cdn.test/b.png?w=1",
    ]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("93.184.216.34", True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("127.0.0.1", False),
        ("::1", False),
        ("10.1.2.3", False),
        ("172.16.5.4", False),
        ("192.168.1.10", False),
        ("169.254.169.254", False),  # クラウドのメタデータエンドポイント
        ("100.77.68.10", False),     # Tailscale (100.64.0.0/10)
        ("fd00::1", False),
        ("0.0.0.0", False),
        ("not-an-ip", False),
    ],
)
def test_is_public_ip(raw: str, expected: bool) -> None:
    assert _is_public_ip(raw) is expected


def _fake_getaddrinfo(mapping: dict[str, list[str]]):
    """host -> 解決結果 IP 群 のマップで getaddrinfo を差し替えるヘルパ。"""
    async def getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise OSError(f"unknown host {host}")
        return [(None, None, None, "", (ip, port)) for ip in mapping[host]]
    return getaddrinfo


def test_is_probe_allowed_rejects_internal_and_odd_targets(monkeypatch) -> None:
    """本文の <img src> は攻撃者が書ける値なので、内部宛は実測しない (SSRF)。"""
    resolved = {
        "cdn.test": ["93.184.216.34"],
        "localhost": ["127.0.0.1"],
        "metadata.test": ["169.254.169.254"],
        # 公開 IP と内部 IP を混ぜて返すホスト (DNS 側での抜け道を塞ぐ)
        "mixed.test": ["93.184.216.34", "10.0.0.5"],
    }

    class _Loop:
        getaddrinfo = staticmethod(_fake_getaddrinfo(resolved))

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _Loop())

    async def allowed(url: str) -> bool:
        return await _is_probe_allowed(url)

    assert asyncio.run(allowed("https://cdn.test/a.png")) is True
    assert asyncio.run(allowed("http://cdn.test/a.png")) is True
    assert asyncio.run(allowed("http://localhost/a.png")) is False
    assert asyncio.run(allowed("http://metadata.test/latest/meta-data/")) is False
    assert asyncio.run(allowed("https://mixed.test/a.png")) is False
    # 未知のスキーム・ポート・ホスト
    assert asyncio.run(allowed("file:///etc/passwd")) is False
    assert asyncio.run(allowed("https://cdn.test:11434/a.png")) is False
    assert asyncio.run(allowed("https://unknown.test/a.png")) is False


def test_probe_image_sizes_skips_disallowed_targets(monkeypatch) -> None:
    """許可外の宛先には 1 リクエストも出さない。"""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=_png(10, 10))

    async def deny_all(url: str) -> bool:
        return False

    monkeypatch.setattr(content_extractor, "_is_probe_allowed", deny_all)

    async def run() -> dict[str, tuple[int, int]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _probe_image_sizes(client, ["http://127.0.0.1/a.png"])

    assert asyncio.run(run()) == {}
    assert requested == []


def test_probe_image_sizes_does_not_follow_redirects(monkeypatch) -> None:
    """リダイレクト先は再検証できないため追わない (内部宛への横流しを防ぐ)。"""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/redirect.png":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/a.png"})
        return httpx.Response(200, content=_png(10, 10))

    async def allow_all(url: str) -> bool:
        return True

    monkeypatch.setattr(content_extractor, "_is_probe_allowed", allow_all)

    async def run() -> dict[str, tuple[int, int]]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            return await _probe_image_sizes(client, ["https://cdn.test/redirect.png"])

    assert asyncio.run(run()) == {}
    assert requested == ["https://cdn.test/redirect.png"]


def test_probe_image_sizes_reads_only_the_header(monkeypatch) -> None:
    """先頭バイトだけ読んで寸法を得る。取得できない画像は結果に含めない。"""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/ok.png":
            return httpx.Response(206, content=_png(800, 441))
        if request.url.path == "/broken.png":
            return httpx.Response(404)
        return httpx.Response(200, content=b"not an image")

    async def allow_all(url: str) -> bool:
        return True

    monkeypatch.setattr(content_extractor, "_is_probe_allowed", allow_all)

    async def run() -> dict[str, tuple[int, int]]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await _probe_image_sizes(
                client,
                [
                    "https://cdn.test/ok.png",
                    "https://cdn.test/broken.png",
                    "https://cdn.test/text.png",
                ],
            )

    assert asyncio.run(run()) == {"https://cdn.test/ok.png": (800, 441)}
    assert len(requested) == 3
