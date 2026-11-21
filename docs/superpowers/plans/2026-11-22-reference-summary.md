# 参照用要約と要約入力の切り詰め修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 要約入力の 3,000 字切り詰めを直し（v0.14.5）、kioku 向けの `reference_summary` を Saved 記事に生成して export に載せる（v0.15.0）。

**Architecture:** 切り詰めは `summary_rules.clip_for_llm` に集約し、先頭 2/3 と末尾 1/3 を残す。参照用要約は新モジュール `app/ai/reference_summarizer.py` の専用 LLM 呼び出しで作り、`background_processor` の新しい Phase 4 ループ（`reserved` レーン）が Saved 記事に 1 件ずつ書く。`exports.py` は 5 欄を足すだけ。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy async + SQLite、pytest + pytest-asyncio、uv。

**Spec:** `docs/superpowers/specs/2026-11-22-reference-summary-design.md`

## Global Constraints

- テストは LLM を必ずモックする。実サーバーに依存させない。
- `app.ai.*` と兄弟 `app.services.*` は関数内で遅延 import する（`app.models` / `app.database` / `app.config` は先頭で可）。
- 記事 1 件につき UPDATE は 1 回。一括 UPDATE をしない（FTS トリガーが本文を再インデックスするため）。
- 新しい列は `app/main.py` の lifespan にある手動 `ALTER TABLE` で足す。
- コメントは日本語、識別子は英語。マジックナンバーは定数にする。
- テスト実行: `cd backend && uv run pytest`。フロントの変更はない。
- リリースは AGENTS.md の作業フロー（ブランチ → テスト → `--no-ff` マージ → バージョン更新 → `uv lock` / `npm install --package-lock-only` → タグ → README 英日 → push → LaunchAgent 再読込）。
- バージョン: Part 1 は `0.14.5`、Part 2 は `0.15.0`（`backend/pyproject.toml` と `frontend/package.json` を揃える）。
- LaunchAgent の再読込は `launchctl bootout` → `bootstrap`。`kickstart -k` は plist を読み直さない。

## Review Focus

1. **LLM が `<think>…</think>` を本文に混ぜて返す**（`reasoning_effort=none` で Ollama の thinking 解析が止まる既知の現象）。見出しが揃っていても `<think>` を含む出力は保存しない → Task 4 のテスト。
2. **生成中に記事が Saved から外された／dedup で消された**。消えていたら何もせず、Saved でなくなっていたら書かない → Task 5 のテスト。
3. **本文も抜粋も空白だけの記事**。選ばれ続けて Phase 4 が空回りしない（対象外にする）→ Task 5 のテスト。
4. **LLM が None を返す・例外を投げる**。書かずに `_reference_skip_until` で 300 秒飛ばし、他フェーズの `_llm_skip_until` には触れない → Task 5 のテスト。
5. **ちょうど上限の長さの本文**。`clip_for_llm` は `len == limit` を無加工で返す（境界のずれで結論の 1 文字目が欠けない）→ Task 1 のテスト。

---

## Part 1 — `fix/summary-input-truncation`（v0.14.5）

### Task 0: lethe の context の確認（コードなし）

- [ ] **Step 1: ロード中の context を確かめる**

Run: `curl -s http://100.123.63.64:11434/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"qwen3.8:27b","reasoning_effort":"none","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}' >/dev/null; curl -s http://100.123.63.64:11434/api/ps`
Expected: `qwen3.8:27b` の `context_length` が `32768`。8192 ならここで止めてユーザーに報告する（入力上限の前提が崩れる）。

- [ ] **Step 2: 長文の末尾が読まれるかを 1 件で試す**

本番 DB（`data/snoreader.db`）から `length(content) > 12000` の記事を 1 件選び、本文の末尾 500 字にだけ出る固有名詞を 1 つ控える。Task 1 の実装後、その記事の本文を `clip_for_llm(content, SUMMARY_INPUT_CHARS)` に通して `summarize_and_tag` 相当のリクエストを lethe に直接送り、要約にその固有名詞が出るかを見る。
Expected: 出る。出なければユーザーに報告する。

### Task 1: `clip_for_llm` と要約経路への適用

**Files:**
- Modify: `backend/app/ai/summary_rules.py`
- Modify: `backend/app/ai/processor.py:95`
- Modify: `backend/app/ai/summarizer.py:36`
- Test: `backend/tests/test_summary_rules.py`、`backend/tests/test_processor.py`、`backend/tests/test_summarizer.py`

**Interfaces:**
- Produces: `summary_rules.SUMMARY_INPUT_CHARS: int = 12_000`、`summary_rules.CLIP_SEPARATOR: str = "\n[…中略…]\n"`、`summary_rules.clip_for_llm(text: str, limit: int) -> str`（Task 4 も使う）

- [ ] **Step 1: 失敗するテストを書く**（`tests/test_summary_rules.py` に追加）

```python
from app.ai.summary_rules import CLIP_SEPARATOR, clip_for_llm


def test_clip_returns_text_unchanged_at_exact_limit():
    text = "あ" * 100
    assert clip_for_llm(text, 100) == text


def test_clip_keeps_head_and_tail_when_over_limit():
    text = "H" * 200 + "M" * 100 + "T" * 200  # 500 字
    out = clip_for_llm(text, 300)
    assert out.startswith("H" * 200)          # 先頭 limit*2//3 = 200
    assert out.endswith("T" * 100)            # 末尾 limit//3 = 100
    assert CLIP_SEPARATOR in out
    assert "M" not in out
    assert len(out) == 300 + len(CLIP_SEPARATOR)
```

`tests/test_processor.py` に、`chat_completion` をモックして受け取った user メッセージを捕まえ、末尾にだけ置いた語（`"結論マーカー"`）が 20,000 字の本文から渡ることを確かめるテスト `test_summarize_and_tag_passes_article_tail` を足す。`tests/test_summarizer.py` に `summarize_article` 版の `test_summarize_article_passes_article_tail` を同じ形で足す。

- [ ] **Step 2: 失敗を確認する**

Run: `uv run pytest tests/test_summary_rules.py tests/test_processor.py tests/test_summarizer.py -v`
Expected: `clip_for_llm` の ImportError、tail のテストは `結論マーカー` が見つからず FAIL。

- [ ] **Step 3: `clip_for_llm(text: str, limit: int) -> str` を `summary_rules.py` に実装する**

`len(text) <= limit` はそのまま返し、超えたら `text[: limit * 2 // 3] + CLIP_SEPARATOR + text[-(limit // 3):]`。`processor.py` と `summarizer.py` の `text[:3000]` を `summary_rules.clip_for_llm(text, summary_rules.SUMMARY_INPUT_CHARS)` に置き換える。

- [ ] **Step 4: 通ることを確認する**

Run: `uv run pytest -q`
Expected: 全件 PASS。

- [ ] **Step 5: コミット**

```bash
git add backend/app/ai/summary_rules.py backend/app/ai/processor.py backend/app/ai/summarizer.py backend/tests/test_summary_rules.py backend/tests/test_processor.py backend/tests/test_summarizer.py
git commit -m "fix: keep article tail in LLM summary input"
```

### Task 2: Part 1 のリリース

- [ ] **Step 1:** Task 0 Step 2 の実地確認を行う（Task 1 のコードで）。
- [ ] **Step 2:** バージョンを `0.14.5` に上げ、`uv lock` と `npm install --package-lock-only` で lockfile を更新する。README（英・日）の変更履歴に「要約入力が先頭 3,000 字で切れて結論が落ちていた問題を修正（上限 12,000 字、長文は先頭と末尾を残す）」を足す。
- [ ] **Step 3:** `uv run pytest -q` が全件 PASS。
- [ ] **Step 4:** コミット `fix: keep article tail in LLM summary input (v0.14.5)`、`main` へ `--no-ff` マージ、タグ `v0.14.5`、push。
- [ ] **Step 5:** `make deploy` → `launchctl bootout` / `bootstrap` → `launchctl list | grep snoreader` で新 PID、`curl -s localhost:8000/api/ai/status` が `available: true`。

---

## Part 2 — `feat/reference-summary`（v0.15.0）

`main`（v0.14.5）から `feat/reference-summary` を切る。

### Task 3: 列の追加

**Files:**
- Modify: `backend/app/models.py`（`Article`）
- Modify: `backend/app/main.py`（lifespan の `ALTER TABLE` 群の末尾）
- Test: `backend/tests/test_reference_columns.py`（新規）

**Interfaces:**
- Produces: `Article.reference_summary: str | None`（Text）、`Article.reference_summary_at: str | None`、`Article.reference_summary_version: str | None`、`Article.reference_basis: str | None`（String。値は `"content"` / `"excerpt"`）

- [ ] **Step 1: 失敗するテストを書く**

`test_lifespan_adds_reference_columns_to_existing_db`: 4 列の無い旧スキーマの `articles` を持つ DB を `tmp_path` に作り、`SNOREADER_DATABASE_URL` を向けて `TestClient(app)` で lifespan を回し、`PRAGMA table_info(articles)` に 4 列が揃うことを確かめる。旧スキーマは「今の `Base.metadata.create_all` で作ってから 4 列を `ALTER TABLE … DROP COLUMN` で落とす」で作る。

- [ ] **Step 2: 失敗を確認する** — Run: `uv run pytest tests/test_reference_columns.py -v` / Expected: 列が無く FAIL。
- [ ] **Step 3: 実装** — `Article` に 4 列（コメントで用途を書く）、`main.py` に `if "<col>" not in existing_article_cols: ALTER TABLE articles ADD COLUMN <col> TEXT` を 4 つ。
- [ ] **Step 4: 通ることを確認する** — Run: `uv run pytest -q` / Expected: 全件 PASS。
- [ ] **Step 5: コミット** — `feat: add reference summary columns`

### Task 4: 参照用要約の生成器

**Files:**
- Create: `backend/app/ai/reference_summarizer.py`
- Test: `backend/tests/test_reference_summarizer.py`（新規）

**Interfaces:**
- Consumes: `summary_rules.clip_for_llm`、`llm_client.chat_completion(messages, *, max_tokens, temperature, priority, lane) -> str | None`、`app.config.settings.llm_model`
- Produces:
  - 定数 `REFERENCE_PROMPT_VERSION = 1`、`REFERENCE_INPUT_CHARS = 16_000`、`REFERENCE_MAX_TOKENS = 1536`、`REFERENCE_MIN_CHARS = 300`、`REFERENCE_MAX_CHARS = 1500`、`REFERENCE_HEADINGS = ("【要旨】", "【事実】", "【主張と出どころ】", "【時点】")`
  - `reference_version(model: str) -> str` — `f"{model}/ref-v{REFERENCE_PROMPT_VERSION}"`
  - `current_version_suffix() -> str` — `f"/ref-v{REFERENCE_PROMPT_VERSION}"`（Task 5 の古さ判定に使う）
  - `validate_reference_summary(raw: str | None) -> str | None` — 前後の空白を落とし、4 見出しがすべて含まれ、`<think>` を含まず、長さが `REFERENCE_MIN_CHARS`〜`REFERENCE_MAX_CHARS` なら返す。それ以外は None。
  - `async generate_reference_summary(title: str, text: str, published_at: str | None, *, priority: int | None = None, lane: str = "reserved") -> str | None`

- [ ] **Step 1: 失敗するテストを書く**

```python
VALID = (
    "【要旨】SnoReader の記事。結論は X。\n"
    "【事実】\n" + "".join(f"・事実{i} Qwen3.8 27B 2026-11-22\n" for i in range(8)) +
    "【主張と出どころ】記事の主張: …。公式の発表: …。推測: …。\n"
    "【時点】2026-11-22 公開。\n"
)  # テスト内で 300 字以上になるよう事実行を伸ばしておく

def test_validate_accepts_four_headings_within_length(): assert validate_reference_summary(VALID) == VALID.strip()
def test_validate_rejects_missing_heading(): assert validate_reference_summary(VALID.replace("【時点】", "")) is None
def test_validate_rejects_too_short(): assert validate_reference_summary("【要旨】a【事実】b【主張と出どころ】c【時点】d") is None
def test_validate_rejects_too_long(): assert validate_reference_summary(VALID + "あ" * 1500) is None
def test_validate_rejects_think_leak(): assert validate_reference_summary("<think>x</think>\n" + VALID) is None
def test_validate_rejects_none(): assert validate_reference_summary(None) is None
def test_reference_version_format(): assert reference_version("qwen3.8:27b") == "qwen3.8:27b/ref-v1"
```

非同期テスト `test_generate_passes_tail_and_published_at`: `chat_completion` をモックして messages を捕まえ、30,000 字本文の末尾の語と `published_at` の文字列が user メッセージに入り、`lane="reserved"`・`max_tokens=REFERENCE_MAX_TOKENS` で呼ばれ、モックが `VALID` を返すと `VALID.strip()` が返ることを確かめる。`test_generate_returns_none_on_invalid_output`: モックが見出し欠けを返すと None。

- [ ] **Step 2: 失敗を確認する** — Run: `uv run pytest tests/test_reference_summarizer.py -v` / Expected: ImportError。
- [ ] **Step 3: 実装**

システムプロンプトは日本語で、設計書「生成」節の 4 部構成をそのまま指示する: 引くための要約であること、`【事実】` は箇条 5〜10 個で固有名詞・版・型番・数値・日付を原文の表記のまま（英語名は英語のまま）、`【主張と出どころ】` は「記事の主張／公式の発表／推測」を区別、`【時点】` は公開日か「〜の版で」、全体 600〜1,000 字（最大 1,200 字）、見出し以外の前置きを書かない。user メッセージは `processor.py` と同じく md5 由来の `[uid]` 前置き（`f"ref:{title}"`）、`Title:`、`Published:`（None なら `unknown`）、`clip_for_llm(text, REFERENCE_INPUT_CHARS)`。`temperature=0.2`。戻り値は `validate_reference_summary` に通す。

- [ ] **Step 4: 通ることを確認する** — Run: `uv run pytest -q` / Expected: 全件 PASS。
- [ ] **Step 5: コミット** — `feat: add reference summary generator`

### Task 5: Phase 4 ループ

**Files:**
- Modify: `backend/app/services/background_processor.py`
- Test: `backend/tests/test_background_phase4.py`（新規。`db` フィクスチャと `_seed` は `tests/test_background_phase3.py` と同じ形で、`_reference_skip_until.clear()` を足す）

**Interfaces:**
- Consumes: Task 3 の 4 列、Task 4 の `generate_reference_summary`、`reference_version`、`current_version_suffix`、`task_queue.PRIORITY_BACKGROUND`
- Produces: `_reference_skip_until: dict[int, float]`、`async _process_phase4_one() -> bool`（処理対象があれば True）、`async _phase4_loop() -> None`、`start()` での `background-processor-phase4` タスク登録

選択の順（1 件ずつ、すべて `is_saved` かつ `_reference_skip_until` 中の id を除く。入力が無い記事は除く＝「`extract_status IS NULL` かつ `trim(content) != ''`」または「`trim(summary) != ''`」）:
1. `reference_summary IS NULL`、`saved_at DESC`
2. `reference_basis = 'excerpt'` かつ本文が使える（`extract_status IS NULL` かつ `trim(content) != ''`）、`saved_at DESC`
3. `reference_summary_version NOT LIKE '%' || current_version_suffix()`、`reference_summary_at ASC`

basis の決め方: `extract_status IS NULL` かつ `content.strip()` があれば `"content"`、なければ `"excerpt"`（`summary` を使う）。

- [ ] **Step 1: 失敗するテストを書く**（すべて `generate_reference_summary` をモック）

- `test_phase4_writes_all_four_columns`: 本文ありの Saved 記事 → 4 列が入り、`reference_basis == "content"`、`reference_summary_version == "<settings.llm_model>/ref-v1"`、`reference_summary_at` が ISO 文字列。
- `test_phase4_skips_unsaved_articles`: 未 Saved だけなら `_process_phase4_one()` が False、モック未呼び出し。
- `test_phase4_newest_saved_first`: `saved_at` の違う 2 件で、新しい方が先に処理される。
- `test_phase4_uses_excerpt_when_extraction_failed`: `extract_status="not_found"` と `summary` あり → `reference_basis == "excerpt"`、モックに渡る text が `summary`。
- `test_phase4_redoes_excerpt_once_content_is_available`: 生成済み `excerpt` の記事に本文が入り `extract_status=None` → 再生成で `"content"` に変わる。
- `test_phase4_regenerates_older_prompt_version`: `reference_summary_version="m/ref-v0"` → 作り直される。`"other-model/ref-v1"` は作り直されない（`_process_phase4_one()` が False）。
- `test_phase4_ignores_whitespace_only_input`（Review Focus 3）: `content="   "`・`summary="  "` の Saved 記事 → False、モック未呼び出し。
- `test_phase4_backs_off_on_none_without_touching_llm_skip`（Review Focus 4）: モックが None → 列は NULL のまま、`_reference_skip_until` に id が入り、`_llm_skip_until` は空。例外を投げる版も同様。
- `test_phase4_does_not_write_if_unsaved_meanwhile`（Review Focus 2）: モックの中で記事の `is_saved` を False にする → 列は NULL のまま、例外なし。
- `test_phase4_handles_article_deleted_meanwhile`（Review Focus 2）: モックの中で記事を削除 → 例外なく True。

- [ ] **Step 2: 失敗を確認する** — Run: `uv run pytest tests/test_background_phase4.py -v` / Expected: `_process_phase4_one` の AttributeError。
- [ ] **Step 3: 実装** — `_process_phase3_one` / `_phase3_loop` と同じ形で書く。LLM 呼び出しはセッションの外。書き込みは `session.get` で取り直し、無い・Saved でない場合は書かずに True。4 列は 1 回の commit。`priority=PRIORITY_BACKGROUND`、`lane="reserved"`。失敗時は `_reference_skip_until[id] = time.monotonic() + _SKIP_DURATION`。モジュール docstring の Phase 一覧に Phase 4 を足す。
- [ ] **Step 4: 通ることを確認する** — Run: `uv run pytest -q` / Expected: 全件 PASS。
- [ ] **Step 5: コミット** — `feat: generate reference summaries for saved articles`

### Task 6: export の 5 欄

**Files:**
- Modify: `backend/app/routers/exports.py`
- Test: `backend/tests/test_export_saved.py`（新規）

**Interfaces:**
- Consumes: Task 3 の 4 列、既存の `Article.genre`

- [ ] **Step 1: 失敗するテストを書く**

- `test_export_includes_reference_fields`: 生成済みの Saved 記事 → 要素に `reference_summary`・`reference_summary_at`・`reference_summary_version`・`reference_basis`・`genre` が値どおり入る。
- `test_export_returns_null_reference_fields_when_missing`: 未生成 → 4 つの reference 欄が `None`（キーは存在する）。
- `test_export_keeps_existing_fields`: 既存の 10 キー（`id, title, url, author, published_at, saved_at, feed, summary, ai_summary, tags`）が残る。
- `test_export_excludes_unsaved`: 未 Saved の記事は出ない。

- [ ] **Step 2: 失敗を確認する** — Run: `uv run pytest tests/test_export_saved.py -v` / Expected: KeyError で FAIL。
- [ ] **Step 3: 実装** — 辞書に 5 キーを足す。並び・ファイル名・ヘッダーは変えない。
- [ ] **Step 4: 通ることを確認する** — Run: `uv run pytest -q` / Expected: 全件 PASS。
- [ ] **Step 5: コミット** — `feat: export reference summary fields`

### Task 7: Part 2 のリリースと実地確認

- [ ] **Step 1:** README（英・日）に参照用要約（目的、Saved のみ、Phase 4、export の 5 欄と null の意味）を書く。CLAUDE.md（ローカル専用・コミットしない）の Background processor 節に Phase 4、AI module 節に `reference_summarizer.py`、Routers 節の `exports.py` に 5 欄を書き足す。
- [ ] **Step 2:** バージョン `0.15.0`、lockfile 更新、`uv run pytest -q` 全件 PASS。
- [ ] **Step 3:** コミット `feat: add reference summaries for kioku (v0.15.0)`、`--no-ff` マージ、タグ `v0.15.0`、push。
- [ ] **Step 4:** `make deploy` → `bootout` / `bootstrap` → 新 PID と `available: true` を確認。
- [ ] **Step 5:** 数分待ち、`sqlite3 data/snoreader.db "select id, length(reference_summary), reference_basis, reference_summary_version from articles where reference_summary is not null order by reference_summary_at desc limit 5"` で生成されていることを確かめ、1 件を本文と見比べて `【事実】` の固有名詞・数値が一致するかを目視する（設計書「検証」）。`curl -s localhost:8000/api/export/saved-articles | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d), sum(a['reference_summary'] is not None for a in d))"` で export に出ていることを確かめる。
