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

from app.services.content_extractor import (
    _apply_image_sizes,
    _declared_image_sizes,
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


def test_probe_image_sizes_reads_only_the_header() -> None:
    """先頭バイトだけ読んで寸法を得る。取得できない画像は結果に含めない。"""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/ok.png":
            return httpx.Response(206, content=_png(800, 441))
        if request.url.path == "/broken.png":
            return httpx.Response(404)
        return httpx.Response(200, content=b"not an image")

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
