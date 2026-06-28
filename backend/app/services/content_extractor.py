"""Extract clean article content from URLs using trafilatura."""

from __future__ import annotations

import logging
import re
from typing import Literal
from urllib.parse import urljoin

import httpx
import trafilatura

ExtractStatus = Literal["not_found", "forbidden", "error"]

logger = logging.getLogger(__name__)

_GRAPHIC_RE = re.compile(r'<graphic\b([^>]*)/?>', re.IGNORECASE)
_ATTR_SRC = re.compile(r'\bsrc="([^"]*)"')
_ATTR_ALT = re.compile(r'\balt="([^"]*)"')
_NESTED_PRE = re.compile(r'<pre>\s*<pre>(.*?)</pre>\s*</pre>', re.DOTALL)

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


def _fix_html(html: str, base_url: str = "") -> str:
    """Convert trafilatura-specific tags to standard HTML and fix image URLs."""
    def _graphic_to_img(m: re.Match) -> str:
        attrs = m.group(1)
        src = _ATTR_SRC.search(attrs)
        alt = _ATTR_ALT.search(attrs)
        src_val = src.group(1) if src else ""
        alt_val = alt.group(1) if alt else ""
        if base_url and src_val and not src_val.startswith(('http', '//', 'data:')):
            src_val = urljoin(base_url, src_val)
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
            src = sm.group(1)
            if base_url and src and not src.startswith(('http', '//', 'data:')):
                src = urljoin(base_url, src)
            return f'src="{src}"'
        tag = re.sub(r'src="([^"]*)"', _abs_src, tag)
        # Drop author/commentator profile images by CDN host
        src_m = re.search(r'src="https?://([^/"]+)', tag)
        if src_m and src_m.group(1) in _PROFILE_IMG_HOSTS:
            return ""
        if 'referrerpolicy' not in tag:
            tag = tag.replace('<img', '<img referrerpolicy="no-referrer"', 1)
        return tag

    html = re.sub(r'<img\b[^>]*>', _fix_img_tag, html)

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
        result = _fix_html(result, base_url=url)
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

            return _extract_from_html(_decoded_html(resp), str(resp.url)), None

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
