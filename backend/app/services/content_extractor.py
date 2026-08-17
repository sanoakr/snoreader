"""Extract clean article content from URLs using trafilatura."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

ExtractStatus = Literal["not_found", "forbidden", "error"]

logger = logging.getLogger(__name__)

_GRAPHIC_RE = re.compile(r'<graphic\b([^>]*)/?>', re.IGNORECASE)
_ATTR_SRC = re.compile(r'\bsrc="([^"]*)"')
_ATTR_ALT = re.compile(r'\balt="([^"]*)"')
_NESTED_PRE = re.compile(r'<pre>\s*<pre>(.*?)</pre>\s*</pre>', re.DOTALL)

_IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_IMG_SRC_ANY_QUOTE_RE = re.compile(r'\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
_IMG_WIDTH_RE = re.compile(r'\bwidth=["\']?(\d+)', re.IGNORECASE)
_IMG_HEIGHT_RE = re.compile(r'\bheight=["\']?(\d+)', re.IGNORECASE)
_HAS_DIMENSION_RE = re.compile(r'\b(?:width|height)=', re.IGNORECASE)

# 画像の寸法をリーダー側で予約するため、抽出時に width/height を埋める。
# 属性が無いと読み込み完了まで高さ 0 のままで、本文を読んでいる最中に
# 画像 1 枚ごとに数百 px 伸びてスクロール位置がずれる (WebKit はスクロール
# アンカリング非対応なので補正されない)。
# 先頭だけ読めば寸法は判るので Range 付きで取る。JPEG は SOF マーカーまで
# 進む必要があるため多めに確保する。
_IMAGE_HEAD_BYTES = 16384
_IMAGE_PROBE_CONCURRENCY = 5
_IMAGE_PROBE_TIMEOUT = 8.0
# 1 記事あたりの実測本数上限 (画像を大量に貼るページで抽出が長引くのを防ぐ)
_IMAGE_PROBE_LIMIT = 40
# 寸法を読めない・読む意味がない拡張子
_UNSIZED_IMAGE_SUFFIXES = ('.svg', '.svgz')
# 実測リクエストを許可するポート (画像 CDN は 80/443 のみ)
_ALLOWED_PROBE_PORTS = frozenset({80, 443})
# この px 以下の画像は計測用ビーコンや遅延読み込みのスペーサーで、本文ではない。
# Togetter は実 URL を JS で差し込むため、静的 HTML には 1x1 の p.gif が
# ユーザーアイコンの数だけ並ぶ。寸法を埋めた後なら確実に判別できる
_SPACER_MAX_PX = 2

# trafilatura が出力する <row>/<cell> を標準 HTML テーブルタグへ変換するパターン
_ROW_RE = re.compile(r'<row\b([^>]*)>', re.IGNORECASE)
_CELL_RE = re.compile(r'<cell\b([^>]*)>', re.IGNORECASE)

# Zenn / 数式サイトが使う数式要素を抽出・保護するためのパターン
# display=true → ブロック数式、それ以外 → インライン数式
_EMBED_KATEX_RE = re.compile(
    r'<embed-katex([^>]*)>\s*<eq[^>]*>([\s\S]*?)</eq>\s*</embed-katex>',
    re.IGNORECASE,
)
# Qiita/はてな等の Markdown レンダラが生成する数式 span の開きタグ
# - math-inline / math-block: Qiita 等
# - katex-display: KaTeX レンダラ
# - mathjax / MathJax_Display: MathJax
# (入れ子になる <span> のため、閉じタグは別途カウンタで探す)
_MATH_SPAN_OPEN_RE = re.compile(
    r'<span\b([^>]*\bclass="[^"]*\b'
    r'(?:math-inline|math-block|katex-display|MathJax(?:_Display)?|mathjax)'
    r'\b[^"]*"[^>]*)>',
    re.IGNORECASE,
)
_SPAN_TOKEN_RE = re.compile(r'<(/?)span\b[^>]*>', re.IGNORECASE)
_TAG_STRIP_RE = re.compile(r'<[^>]+>')
_MATH_DOLLAR_RE = re.compile(r'^\s*(\$\$?)([\s\S]+?)\1\s*$')
_ANNOTATION_RE = re.compile(
    r'<annotation\b[^>]*encoding="application/x-tex"[^>]*>([\s\S]*?)</annotation>',
    re.IGNORECASE,
)
# Qiita / note 等が記事本文に埋める生のドル記法を捕まえるパターン。
# - $$...$$ : 段落単独なら display、文中混在なら inline (note.com は本文中でも $$ を使う)
# - $...$  : 常に inline
# - <pre>/<code> 内は対象外にするため事前に剥がす
_BLOCK_DOLLAR_RE = re.compile(r'\$\$([\s\S]+?)\$\$')
# $ の前後が ASCII 英数字/もう一つの $ でないことを要求する。
# Python の \w は Unicode で日本語にもマッチするため [A-Za-z0-9_] で明示する。
# 値段 "$30" や正規表現末尾の $$ などはここで除外される。
_INLINE_DOLLAR_RE = re.compile(
    r'(?<![\\A-Za-z0-9_$])\$(?!\s)([^$\n<>]{1,200}?)(?<!\s)\$(?![A-Za-z0-9_$])'
)
# LaTeX 標準のデリミタ \(...\) / \[...\] (KaTeX 系サイトが採用)。
# - \[...\] は display、\(...\) は inline
# - 注: \[\] は <code class="math-tex"> プレースホルダーに先に置換するため、
#   その内側の \(...\) が二重変換されることはない
_BRACKET_BLOCK_RE = re.compile(r'\\\[([\s\S]+?)\\\]')
_BRACKET_INLINE_RE = re.compile(r'\\\(([\s\S]+?)\\\)')
_PRE_OR_CODE_RE = re.compile(r'<(pre|code)\b[^>]*>[\s\S]*?</\1>', re.IGNORECASE)
_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
# <b>/<strong> が <a> 1 件だけを包んでいるパターン。Gigazine 等が
# 文中リンクを太字で強調するときに使うが、trafilatura はこの形を
# 段落から切り出してリンクを次の段落より後ろにずらしてしまうため、
# 抽出前に外側の強調タグだけ剥がす。
_BOLD_WRAPPING_ANCHOR_RE = re.compile(
    r'<(b|strong)\b[^>]*>(\s*<a\b[^>]*>[\s\S]*?</a>\s*)</\1>',
    re.IGNORECASE,
)
# 段落の境目: ブロック要素タグの開き・閉じ、または <br>
_BLOCK_BOUNDARY_RE = re.compile(
    r'<(?:/?(?:p|div|li|h[1-6]|blockquote|td|th|figcaption|article|section)\b[^>]*'
    r'|br\s*/?)>',
    re.IGNORECASE,
)

# 著者・コメンテーターのプロフィール画像として知られている CDN ホスト
_PROFILE_IMG_HOSTS = {
    "byline-pctr.c.yimg.jp",  # Yahoo! ニュース エキスパート著者アイコン
}

_YAHOO_PICKUP_RE = re.compile(r'https?://news\.yahoo\.co\.jp/pickup/')
# Match canonical article URLs only — exclude sub-paths like /articles/HASH/images/000
_YAHOO_ARTICLE_RE = re.compile(r'^https?://news\.yahoo\.co\.jp/articles/[^/?#]+/?(?:[?#].*)?$')
# Matches URLs that should NOT be treated as the target article
_YAHOO_IGNORE_RE = re.compile(
    r'yimg\.jp|x\.com|twitter\.com|facebook\.com|instagram\.com|lycorp\.co\.jp|privacy',
    re.IGNORECASE,
)

# 47news.jp の記事ページは要約のみ表示し、本文へは news.jp の URL に飛ばす。
_47NEWS_RE = re.compile(r'https?://(?:www\.)?47news\.jp/\d+\.html')
# news.jp の記事 URL (ID 部分は数字)
_NEWS_JP_ARTICLE_RE = re.compile(r'^https?://news\.jp/i/\d+(?:\?[^\s"\']*)?$')

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# <meta charset="..."> / <meta http-equiv="Content-Type" content="text/html; charset=..."> の宣言を拾う
_META_CHARSET_RE = re.compile(
    rb'<meta[^>]+?charset=["\']?\s*([A-Za-z0-9_\-:]+)',
    re.IGNORECASE,
)


def _absolute_src(src: str, base_url: str) -> str:
    """相対 src を絶対 URL にする。base_url が無い場合はそのまま返す。"""
    if base_url and src and not src.startswith(('http', '//', 'data:')):
        return urljoin(base_url, src)
    return src


def _declared_image_sizes(source_html: str, base_url: str) -> dict[str, tuple[int, int]]:
    """元ページの <img> が宣言している表示サイズを ``src -> (w, h)`` で返す。

    trafilatura の ``<graphic>`` は src と alt しか持たないため、抽出後の HTML では
    元ページが指定していた width/height が失われる。実ファイルの原寸で描画すると
    26px のアイコンが 200px になるので、宣言値があればそれを最優先で使う。
    """
    sizes: dict[str, tuple[int, int]] = {}
    for tag in _IMG_TAG_RE.findall(source_html):
        src_m = _IMG_SRC_ANY_QUOTE_RE.search(tag)
        w_m = _IMG_WIDTH_RE.search(tag)
        h_m = _IMG_HEIGHT_RE.search(tag)
        if not (src_m and w_m and h_m):
            continue
        width, height = int(w_m.group(1)), int(h_m.group(1))
        if width <= 0 or height <= 0:
            continue
        sizes[_absolute_src(src_m.group(1), base_url)] = (width, height)
    return sizes


def _parse_image_size(head: bytes) -> tuple[int, int] | None:
    """画像バイト列の先頭からピクセル寸法を読む (PNG / GIF / JPEG / WebP)。

    ヘッダーだけで判るフォーマットのみ対応する。判定できなければ None。
    """
    if len(head) < 16:
        return None

    # PNG: IHDR チャンクに 32bit big-endian で幅・高さが入る
    if head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR':
        return (int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big'))

    # GIF: 論理画面記述子に 16bit little-endian
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return (int.from_bytes(head[6:8], 'little'), int.from_bytes(head[8:10], 'little'))

    # WebP: VP8 / VP8L / VP8X の 3 系統でヘッダー構造が違う
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        chunk = head[12:16]
        if chunk == b'VP8X' and len(head) >= 30:
            width = int.from_bytes(head[24:27], 'little') + 1
            height = int.from_bytes(head[27:30], 'little') + 1
            return (width, height)
        if chunk == b'VP8 ' and len(head) >= 30:
            width = int.from_bytes(head[26:28], 'little') & 0x3FFF
            height = int.from_bytes(head[28:30], 'little') & 0x3FFF
            return (width, height) if width and height else None
        if chunk == b'VP8L' and len(head) >= 25:
            bits = int.from_bytes(head[21:25], 'little')
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        return None

    # JPEG: SOF マーカーを探す。マーカー間は長さフィールドで読み飛ばす
    if head[:2] == b'\xff\xd8':
        i = 2
        end = len(head)
        while i + 9 < end:
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            # スタンドアロンマーカー (パディング / RSTn) は長さを持たない
            if marker in (0xFF, 0x01) or 0xD0 <= marker <= 0xD9:
                i += 2
                continue
            length = int.from_bytes(head[i + 2:i + 4], 'big')
            if length < 2:
                return None
            # SOF0-SOF15 (0xC4 DHT / 0xC8 JPG / 0xCC DAC は除く) が寸法を持つ
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(head[i + 5:i + 7], 'big')
                width = int.from_bytes(head[i + 7:i + 9], 'big')
                return (width, height) if width and height else None
            i += 2 + length
        return None

    return None


def _unsized_image_srcs(html: str) -> list[str]:
    """width/height が無く、実測すれば埋められる <img> の src を重複なしで返す。"""
    srcs: list[str] = []
    seen: set[str] = set()
    for tag in _IMG_TAG_RE.findall(html):
        if _HAS_DIMENSION_RE.search(tag):
            continue
        src_m = _ATTR_SRC.search(tag)
        if not src_m:
            continue
        src = src_m.group(1)
        if not src.startswith(('http://', 'https://')):
            continue
        if src.split('?')[0].lower().endswith(_UNSIZED_IMAGE_SUFFIXES):
            continue
        if src in seen:
            continue
        seen.add(src)
        srcs.append(src)
    return srcs


def _is_public_ip(raw: str) -> bool:
    """名前解決結果が公開アドレスかどうか。ループバック・LAN・リンクローカルは除く。"""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    # is_global が loopback / private / link-local / reserved / unspecified を、
    # Tailscale が使う 100.64.0.0/10 (shared address space) までまとめて弾く
    return ip.is_global and not ip.is_multicast


async def _is_probe_allowed(url: str) -> bool:
    """画像実測の宛先として許可するか判定する。

    本文の ``<img src>`` はページ側が自由に書けるため、そのまま取りに行くと
    ループバックや LAN 内サービスへの GET に使われる (SSRF)。名前解決した
    アドレスが**すべて**公開アドレスである http/https の 80/443 のみ許可する。
    弾いた場合は寸法が入らないだけで、画像そのものはブラウザ側で表示される。

    名前解決の結果は httpx が接続時に再度引くため DNS rebinding は残るが、
    ここで取得するのはピクセル寸法のみでレスポンス本文は保存も表示もしない。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:  # ポート部が数値でない URL
        return False
    if port not in _ALLOWED_PROBE_PORTS:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except (OSError, UnicodeError) as e:
        logger.debug("画像サイズ取得: 名前解決失敗 %s: %s", host, e)
        return False
    if not infos:
        return False
    return all(_is_public_ip(info[4][0]) for info in infos)


async def _probe_image_sizes(
    client: httpx.AsyncClient, srcs: list[str]
) -> dict[str, tuple[int, int]]:
    """画像の先頭バイトだけ取得して ``src -> (w, h)`` を返す。失敗分は含めない。"""
    sem = asyncio.Semaphore(_IMAGE_PROBE_CONCURRENCY)
    headers = {
        "User-Agent": _BROWSER_HEADERS["User-Agent"],
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        "Range": f"bytes=0-{_IMAGE_HEAD_BYTES - 1}",
    }

    async def probe(src: str) -> tuple[str, tuple[int, int] | None]:
        async with sem:
            if not await _is_probe_allowed(src):
                logger.debug("画像サイズ取得をスキップ (許可外の宛先) %s", src)
                return src, None
            try:
                # リダイレクト先は再検証できないので追わない。3xx は寸法なし扱い
                async with client.stream(
                    "GET",
                    src,
                    headers=headers,
                    timeout=_IMAGE_PROBE_TIMEOUT,
                    follow_redirects=False,
                ) as resp:
                    if resp.status_code >= 300:
                        return src, None
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) >= _IMAGE_HEAD_BYTES:
                            break
                return src, _parse_image_size(bytes(buf))
            except Exception as e:  # ネットワーク失敗は寸法なしで諦める
                logger.debug("画像サイズ取得失敗 %s: %s", src, e)
                return src, None

    results = await asyncio.gather(*(probe(s) for s in srcs))
    return {src: size for src, size in results if size}


def _apply_image_sizes(html: str, sizes: dict[str, tuple[int, int]]) -> str:
    """寸法未指定の <img> に width/height を付ける。既に付いているタグは触らない。"""
    if not sizes:
        return html

    def _fix(m: re.Match) -> str:
        tag = m.group(0)
        if _HAS_DIMENSION_RE.search(tag):
            return tag
        src_m = _ATTR_SRC.search(tag)
        if not src_m:
            return tag
        size = sizes.get(src_m.group(1))
        if not size:
            return tag
        return tag.replace('<img', f'<img width="{size[0]}" height="{size[1]}"', 1)

    return _IMG_TAG_RE.sub(_fix, html)


def _drop_spacer_images(html: str) -> str:
    """1x1 のビーコン / スペーサー画像を落とす。寸法が判っているタグだけ見る。"""
    def _drop(m: re.Match) -> str:
        tag = m.group(0)
        w_m = _IMG_WIDTH_RE.search(tag)
        h_m = _IMG_HEIGHT_RE.search(tag)
        if not (w_m and h_m):
            return tag
        if int(w_m.group(1)) <= _SPACER_MAX_PX and int(h_m.group(1)) <= _SPACER_MAX_PX:
            return ""
        return tag

    return _IMG_TAG_RE.sub(_drop, html)


async def _reserve_image_space(client: httpx.AsyncClient, html: str) -> str:
    """本文画像の高さを予約するため、寸法未指定の <img> を実測して埋める。"""
    srcs = _unsized_image_srcs(html)
    if not srcs:
        return _drop_spacer_images(html)
    if len(srcs) > _IMAGE_PROBE_LIMIT:
        logger.info("画像が多いため寸法実測を %d 件に制限", _IMAGE_PROBE_LIMIT)
        srcs = srcs[:_IMAGE_PROBE_LIMIT]
    html = _apply_image_sizes(html, await _probe_image_sizes(client, srcs))
    return _drop_spacer_images(html)


def _fix_html(
    html: str, base_url: str = "", image_sizes: dict[str, tuple[int, int]] | None = None
) -> str:
    """Convert trafilatura-specific tags to standard HTML and fix image URLs."""
    def _graphic_to_img(m: re.Match) -> str:
        attrs = m.group(1)
        src = _ATTR_SRC.search(attrs)
        alt = _ATTR_ALT.search(attrs)
        src_val = src.group(1) if src else ""
        alt_val = alt.group(1) if alt else ""
        src_val = _absolute_src(src_val, base_url)
        return f'<img src="{src_val}" alt="{alt_val}" loading="lazy">'

    html = _GRAPHIC_RE.sub(_graphic_to_img, html)
    html = _NESTED_PRE.sub(r'<pre>\1</pre>', html)

    # trafilatura が出力する <row>/<cell> を標準 <tr>/<td> に変換する
    def _row_to_tr(m: re.Match) -> str:
        attrs = m.group(1)
        # span 属性は colspan として引き継ぐ
        colspan = re.search(r'\bspan="(\d+)"', attrs)
        if colspan:
            return f'<tr colspan="{colspan.group(1)}">'
        return '<tr>'

    # <cell role="head"> → <th>...</th>、それ以外 → <td>...</td>
    # </cell> が常に </td> になる問題を避けるため、開きタグと対になる閉じタグを選択する
    cell_stack: list[str] = []

    def _cell_to_td(m: re.Match) -> str:
        attrs = m.group(1)
        tag = 'th' if re.search(r'\brole="head"', attrs) else 'td'
        cell_stack.append(tag)
        return f'<{tag}>'

    def _close_cell(_m: re.Match) -> str:
        # セルは入れ子にならないため FIFO で先頭から取り出す
        tag = cell_stack.pop(0) if cell_stack else 'td'
        return f'</{tag}>'

    html = _ROW_RE.sub(_row_to_tr, html)
    html = re.sub(r'</row>', '</tr>', html, flags=re.IGNORECASE)
    html = _CELL_RE.sub(_cell_to_td, html)
    html = re.sub(r'</cell>', _close_cell, html, flags=re.IGNORECASE)

    # Absolutize relative img src, add referrerpolicy, and strip known profile-image hosts
    def _fix_img_tag(m: re.Match) -> str:
        tag = m.group(0)
        def _abs_src(sm: re.Match) -> str:
            return f'src="{_absolute_src(sm.group(1), base_url)}"'
        tag = re.sub(r'src="([^"]*)"', _abs_src, tag)
        # Drop author/commentator profile images by CDN host
        src_m = re.search(r'src="https?://([^/"]+)', tag)
        if src_m and src_m.group(1) in _PROFILE_IMG_HOSTS:
            return ""
        if 'referrerpolicy' not in tag:
            tag = tag.replace('<img', '<img referrerpolicy="no-referrer"', 1)
        return tag

    html = re.sub(r'<img\b[^>]*>', _fix_img_tag, html)

    # 元ページが宣言していた表示サイズを復元する (実ファイルの原寸より優先)
    html = _apply_image_sizes(html, image_sizes or {})
    html = _drop_spacer_images(html)

    # Qiita / note / KaTeX サイトが本文に埋める生の数式記法を
    # <code class="math-tex"> プレースホルダーへ変換する
    html = _convert_math(html)
    return html


def _convert_math(html: str) -> str:
    """抽出後 HTML 内の数式記法 ($$, $, \\[\\], \\(\\)) を <code class="math-tex"> へ変換する。

    <pre>/<code> 内は対象外にするため事前に退避する。処理順は外側のブロックから:
    1) \\[...\\]  → display
    2) \\(...\\)  → inline
    3) $$...$$    → display または inline (前後文脈で判定)
    4) $...$      → inline
    変換済みの <code class="math-tex"></code> は他段階の正規表現に巻き込まれない
    ようプレースホルダーに退避してから戻す。
    """
    import html as html_mod

    # pre/code ブロックを退避
    code_blocks: list[str] = []

    def _stash_code(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f'\x00CODE{len(code_blocks) - 1}\x00'

    stashed = _PRE_OR_CODE_RE.sub(_stash_code, html)

    def _has_visible_text(segment: str) -> bool:
        """HTML タグを除去した残りに空白以外の文字があるか。"""
        return bool(_TAG_STRIP_RE.sub('', segment).strip())

    def _is_block_context(src: str, start: int, end: int) -> bool:
        """前後がブロック境界だけならブロック扱い。"""
        prev_boundary_end = 0
        for bm in _BLOCK_BOUNDARY_RE.finditer(src, 0, start):
            prev_boundary_end = bm.end()
        if _has_visible_text(src[prev_boundary_end:start]):
            return False
        next_boundary = _BLOCK_BOUNDARY_RE.search(src, end)
        next_boundary_start = next_boundary.start() if next_boundary else len(src)
        if _has_visible_text(src[end:next_boundary_start]):
            return False
        return True

    # 変換結果(math-tex)を退避するための領域。後段の正規表現で巻き込まれないよう
    # 一旦プレースホルダーに置き換え、最後にまとめて戻す。
    math_blocks: list[str] = []

    def _emit(inner: str, display: bool) -> str:
        latex = _BR_RE.sub(' ', inner).strip()
        latex = _TAG_STRIP_RE.sub(' ', latex).strip()
        latex = html_mod.unescape(latex)
        if not latex:
            return ''
        mode = 'display' if display else 'inline'
        tag = f'<code class="math-tex" data-display="{mode}" data-latex="{html_mod.escape(latex)}"></code>'
        math_blocks.append(tag)
        return f'\x00MATH{len(math_blocks) - 1}\x00'

    def _replace_simple(pattern: re.Pattern, src: str, display: bool) -> str:
        def _sub(m: re.Match) -> str:
            return _emit(m.group(1), display) or m.group(0)
        return pattern.sub(_sub, src)

    def _replace_with_context(pattern: re.Pattern, src: str) -> str:
        """前後の文脈で display / inline を判定する置換 ($$...$$ 用)。"""
        out: list[str] = []
        pos = 0
        for m in pattern.finditer(src):
            display = _is_block_context(src, m.start(), m.end())
            repl = _emit(m.group(1), display)
            if not repl:
                continue
            out.append(src[pos:m.start()])
            out.append(repl)
            pos = m.end()
        out.append(src[pos:])
        return ''.join(out)

    # 1) \[...\] (KaTeX/MathJax 標準のブロック数式)
    stashed = _replace_simple(_BRACKET_BLOCK_RE, stashed, display=True)
    # 2) \(...\) (KaTeX/MathJax 標準のインライン数式)
    stashed = _replace_simple(_BRACKET_INLINE_RE, stashed, display=False)
    # 3) $$...$$ (note.com は文中混在もあるので文脈判定)
    stashed = _replace_with_context(_BLOCK_DOLLAR_RE, stashed)
    # 4) $...$ (常にインライン)
    stashed = _replace_simple(_INLINE_DOLLAR_RE, stashed, display=False)

    # pre/code を戻す
    def _restore_code(m: re.Match) -> str:
        return code_blocks[int(m.group(1))]

    stashed = re.sub(r'\x00CODE(\d+)\x00', _restore_code, stashed)

    # math-tex を戻す
    def _restore_math(m: re.Match) -> str:
        return math_blocks[int(m.group(1))]

    return re.sub(r'\x00MATH(\d+)\x00', _restore_math, stashed)


def _find_yahoo_next_url(html_bytes: bytes) -> str | None:
    """Find the next URL to follow from a Yahoo pickup or articles page.

    Priority:
    1. Yahoo article page (/articles/) — pickup pages link here
    2. External source URL — articles pages may link to the original publisher
    """
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html_bytes)
        hrefs = [a.get('href', '') for a in tree.xpath('//a[@href]')]
        for href in hrefs:
            if _YAHOO_ARTICLE_RE.match(href):
                return href
        for href in hrefs:
            if href.startswith('http') and not _YAHOO_IGNORE_RE.search(href):
                return href
    except Exception:
        pass
    return None


def _find_47news_full_url(html_bytes: bytes) -> str | None:
    """47news.jp の記事ページから「記事全文を読む」リンク先 (news.jp) を取り出す。"""
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html_bytes)
        # アンカーのテキストが "記事全文を読む" を含むものを優先
        for a in tree.xpath('//a[@href]'):
            text = ''.join(a.itertext()).strip()
            href = a.get('href', '')
            if '記事全文を読む' in text and _NEWS_JP_ARTICLE_RE.match(href):
                return href
        # フォールバック: news.jp/i/ に向くアンカーを 1 件
        for a in tree.xpath('//a[@href]'):
            href = a.get('href', '')
            if _NEWS_JP_ARTICLE_RE.match(href):
                return href
    except Exception:
        pass
    return None


def _is_matome_blog(html_bytes: bytes) -> bool:
    """Livedoor Blog まとめブログパターン (スレッド書き込み列挙) を検出する。"""
    return (b'id="comments-list"' in html_bytes
            and b'class="comment-set"' in html_bytes)


def _extract_matome_posts(html_bytes: bytes, base_url: str) -> str | None:
    """Livedoor Blog まとめ記事の導入文 + スレッド書き込みを HTML で返す。"""
    try:
        from lxml import html as lxml_html
        from lxml.html import tostring as lxml_tostring

        tree = lxml_html.fromstring(html_bytes)
        tree.make_links_absolute(base_url)
        parts: list[str] = []

        # 導入文
        intro_nodes = tree.xpath('//div[contains(@class,"article-body-inner")]')
        if intro_nodes:
            intro_html = lxml_tostring(intro_nodes[0], encoding="unicode", method="html")
            intro_html = re.sub(r"^<div[^>]*>", '<div class="matome-intro">', intro_html, count=1)
            parts.append(intro_html)

        # スレッド書き込み
        post_items: list[str] = []
        for div in tree.xpath('//div[starts-with(@id,"com_")]'):
            div_id = div.get("id", "")
            post_num = div_id[4:] if div_id.startswith("com_") else ""
            for cs in div.xpath('.//li[contains(@class,"comment-body")]'):
                body_html = lxml_tostring(cs, encoding="unicode", method="html")
                body_inner = re.sub(r"^<li[^>]*>|</li>\s*$", "", body_html.strip())
                post_items.append(
                    f'<div class="thread-post">'
                    f'<span class="post-num">{post_num}</span>'
                    f'<div class="post-body">{body_inner}</div>'
                    f'</div>'
                )

        if post_items:
            parts.append('<div class="thread-posts">' + "".join(post_items) + "</div>")

        if not parts:
            return None
        return _fix_html("\n".join(parts), base_url=base_url)

    except Exception as e:
        logger.warning("まとめブログ抽出失敗 %s: %s", base_url, e)
        return None


def _protect_math(html: str) -> tuple[str, dict[str, str]]:
    """数式要素を一時プレースホルダーに置換してtrafilaturaの削除を防ぐ。

    Zenn の <embed-katex> や Qiita/はてな等の <span class="math-…"> は
    trafilatura に未知タグとして削除されるため、プレースホルダーへ差し替えて
    抽出後に復元する。
    Returns (modified_html, placeholder_map).
    """
    import html as html_mod
    placeholder_map: dict[str, str] = {}
    counter = [0]

    def _make_placeholder(latex: str, display: bool) -> str:
        key = f'MATHPLACEHOLDER{counter[0]:04d}END'
        counter[0] += 1
        tag = 'display' if display else 'inline'
        escaped = html_mod.escape(latex)
        placeholder_map[key] = (
            f'<code class="math-tex" data-display="{tag}" data-latex="{escaped}"></code>'
        )
        # trafilatura がテキストノードとして拾えるよう中身はキーを埋め込む
        return f'<span>{key}</span>'

    def _embed_katex_replace(m: re.Match) -> str:
        attrs = m.group(1)
        latex = m.group(2)
        display = 'display="true"' in attrs or 'display=true' in attrs
        return _make_placeholder(latex, display)

    def _replace_math_spans(src: str) -> str:
        """入れ子になる <span> を考慮して数式 span をプレースホルダーへ置換する。

        外側でマッチした span の内部を再走査しないよう、消費位置を毎回スキップする。
        """
        out: list[str] = []
        pos = 0
        while True:
            m = _MATH_SPAN_OPEN_RE.search(src, pos)
            if not m:
                break
            start = m.start()
            attrs = m.group(1)
            cls_match = re.search(r'class="([^"]*)"', attrs, re.IGNORECASE)
            cls = cls_match.group(1).lower() if cls_match else ''
            display = ('math-block' in cls) or ('katex-display' in cls)

            # 対応する閉じ </span> を入れ子カウンタで探す
            depth = 1
            end = -1
            inner_end = -1
            for tok in _SPAN_TOKEN_RE.finditer(src, m.end()):
                if tok.group(1) == '':
                    depth += 1
                else:
                    depth -= 1
                    if depth == 0:
                        inner_end = tok.start()
                        end = tok.end()
                        break
            if end == -1:
                # 閉じが見つからない場合は無視して先へ進める
                out.append(src[pos:m.end()])
                pos = m.end()
                continue
            inner = src[m.end():inner_end]

            # annotation encoding="application/x-tex" があれば LaTeX 原文を優先
            annot = _ANNOTATION_RE.search(inner)
            if annot:
                latex = html_mod.unescape(annot.group(1)).strip()
            else:
                text = _TAG_STRIP_RE.sub('', inner).strip()
                text = html_mod.unescape(text)
                dollar = _MATH_DOLLAR_RE.match(text)
                latex = dollar.group(2).strip() if dollar else text
            if not latex:
                out.append(src[pos:end])
                pos = end
                continue
            out.append(src[pos:start])
            out.append(_make_placeholder(latex, display))
            pos = end
        out.append(src[pos:])
        return ''.join(out)

    html = _EMBED_KATEX_RE.sub(_embed_katex_replace, html)
    html = _replace_math_spans(html)
    return html, placeholder_map


def _restore_math(html: str, placeholder_map: dict[str, str]) -> str:
    """_protect_math で置換したプレースホルダーを復元する。"""
    for key, replacement in placeholder_map.items():
        html = html.replace(key, replacement)
    return html


def _extract_from_html(html: str | bytes, url: str) -> str | None:
    """Parse HTML (text or bytes) and extract main content as HTML string."""
    html_bytes = html if isinstance(html, bytes) else html.encode()

    # まとめブログパターン → カスタム抽出
    if _is_matome_blog(html_bytes):
        result = _extract_matome_posts(html_bytes, url)
        if result:
            return result

    # 数式タグを一時保護してから trafilatura に渡す
    html_str = html if isinstance(html, str) else html.decode(errors="replace")
    # <b><a>...</a></b> のように強調タグが <a> だけを包んでいると
    # trafilatura が段落から切り出してしまい、リンクが本来の位置より
    # 後ろの段落の下に表示される。外側の強調だけ事前に剥がす。
    html_str = _BOLD_WRAPPING_ANCHOR_RE.sub(r'\2', html_str)
    html_str, math_map = _protect_math(html_str)

    tree = trafilatura.load_html(html_str)
    if tree is None:
        return None
    result = trafilatura.extract(
        tree,
        url=url,
        include_comments=False,
        include_tables=True,
        include_images=True,
        include_links=True,
        include_formatting=True,
        output_format="html",
    )
    # Retry with favor_recall for aggregator/bulletin-board sites where standard extraction fails
    if not result:
        result = trafilatura.extract(
            tree,
            url=url,
            include_comments=False,
            include_tables=True,
            include_images=True,
            include_links=True,
            include_formatting=True,
            favor_recall=True,
            output_format="html",
        )
    if result:
        result = _fix_html(
            result, base_url=url, image_sizes=_declared_image_sizes(html_str, url)
        )
        if math_map:
            result = _restore_math(result, math_map)
    return result


def _decoded_html(resp: httpx.Response) -> str | bytes:
    """Return HTML decoded with the response's declared encoding when available.

    trafilatura/lxml の自動検出は EUC-JP / Shift_JIS のページで誤判定して文字化け
    することがある。決定順は以下:

    1. HTTP レスポンスヘッダに charset がある → httpx の ``.text`` を使う
    2. HTML 内の ``<meta charset>`` 宣言を読み取り、その encoding で decode
    3. どちらも無ければ生バイトを返し、trafilatura 側の自動検出に任せる
    """
    if resp.charset_encoding:
        return resp.text
    m = _META_CHARSET_RE.search(resp.content[:4096])
    if m:
        declared = m.group(1).decode("ascii", errors="ignore").strip().lower()
        # Python は "shift_jis" も "sjis" も解決できるが、一部の別名はそうではない
        alias = {"shift-jis": "shift_jis", "x-sjis": "shift_jis"}
        encoding = alias.get(declared, declared)
        try:
            return resp.content.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    return resp.content


async def extract_content(url: str) -> tuple[str | None, ExtractStatus | None]:
    """Fetch a URL and extract the main article text as HTML.

    Returns ``(html, None)`` on success, ``(None, status)`` on failure where
    status classifies the failure so callers can decide whether to retry:

    - ``"not_found"``  : HTTP 404 (permanent — the resource is gone)
    - ``"forbidden"``  : HTTP 403 (permanent — bot-detection / paywall)
    - ``"error"``      : 5xx / timeout / network error (transient — retry ok)

    Yahoo pickup pages link to /articles/<hash>, where the full body lives.
    We follow that single hop, then extract directly.
    """
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            http2=False,
            headers=_BROWSER_HEADERS,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            # One hop: pickup → articles (or external source as legacy fallback)
            if _YAHOO_PICKUP_RE.search(str(resp.url)):
                next_url = _find_yahoo_next_url(resp.content)
                if next_url and next_url != str(resp.url):
                    logger.info("Yahoo: following %s → %s", resp.url, next_url)
                    try:
                        resp = await client.get(next_url)
                        resp.raise_for_status()
                    except Exception as e:
                        logger.warning("Failed to fetch %s: %s", next_url, e)

            # 47news → news.jp: 47news の記事ページは要約のみで、本文は
            # 「記事全文を読む」ボタンから news.jp に飛ぶ。1 ホップだけ追う。
            if _47NEWS_RE.match(str(resp.url)):
                next_url = _find_47news_full_url(resp.content)
                if next_url and next_url != str(resp.url):
                    logger.info("47news: following %s → %s", resp.url, next_url)
                    try:
                        resp = await client.get(next_url)
                        resp.raise_for_status()
                    except Exception as e:
                        logger.warning("Failed to fetch %s: %s", next_url, e)

            html = _extract_from_html(_decoded_html(resp), str(resp.url))
            if html:
                html = await _reserve_image_space(client, html)
            return html, None

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        logger.warning("HTTP %s fetching %s", status_code, url)
        if status_code == 404:
            return None, "not_found"
        if status_code == 403:
            return None, "forbidden"
        return None, "error"
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return None, "error"
