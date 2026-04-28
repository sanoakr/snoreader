"""記事本文抽出時の文字エンコーディング処理テスト。

EUC-JP / Shift_JIS で配信されるページを trafilatura に直接バイト列で渡すと、
encoding 検出に失敗して全文が文字化けする。Content-Type の charset を尊重する
``_decoded_html`` を経由することで正しく抽出されることを確認する。
"""

from __future__ import annotations

import httpx

from app.services.content_extractor import _decoded_html, _extract_from_html

_EUC_JP_HTML = """\
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=euc-jp">
<title>テストタイトル</title>
</head>
<body>
<article>
<h1>【超画像】アトリエシリーズの新キャラ</h1>
<p>これは日本語の本文です。アトリエという単語が含まれます。</p>
<p>もう一段落。十分な文字量を確保するためにダミー本文を追加します。
日本語のニュース記事の抽出が正しく行えることを確認します。</p>
</article>
</body>
</html>
"""


def _make_response(body: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=body,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "http://example.test/article.html"),
    )


def test_decoded_html_uses_declared_charset() -> None:
    """charset=euc-jp を尊重してテキスト復号できる。"""
    body = _EUC_JP_HTML.encode("euc-jp")
    resp = _make_response(body, "text/html; charset=euc-jp")
    decoded = _decoded_html(resp)
    assert isinstance(decoded, str)
    assert "アトリエ" in decoded


def test_decoded_html_falls_back_to_bytes_when_no_charset() -> None:
    """charset 未宣言なら bytes のまま返す（trafilatura に検出を任せる）。"""
    body = b"<html><body><p>plain</p></body></html>"
    resp = _make_response(body, "text/html")
    assert _decoded_html(resp) is body


def test_extract_from_html_handles_euc_jp_text() -> None:
    """EUC-JP の HTML 文字列から日本語を文字化けせずに抽出できる。"""
    body = _EUC_JP_HTML.encode("euc-jp")
    resp = _make_response(body, "text/html; charset=euc-jp")
    html = _decoded_html(resp)
    extracted = _extract_from_html(html, "http://example.test/")
    assert extracted is not None
    assert "アトリエ" in extracted
