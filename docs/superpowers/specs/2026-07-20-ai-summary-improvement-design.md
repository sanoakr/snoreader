# AIサマリー改善 設計書

日付: 2026-07-20

## 背景・課題

現在の AI サマリーは常に「日本語箇条書き3項目固定」で生成されており、以下の問題がある。

- タイトルに含まれる情報がそのまま箇条書きに繰り返され、要約として新しい情報を提供できていない
- 項目数が常に3固定のため、内容によっては冗長、または逆に記事の結論・結果が省かれることがある

## ゴール

- AI サマリーの箇条書きに、タイトルの情報を繰り返さず、記事の結論・結果を含めた新しい情報のみを書かせる
- 項目数は 1〜9 個の範囲で、必要最小限に絞る（多くても少なくてもよいが、なるべく少なく）
- 既存記事の AI サマリーも新ルールで作り直す

## 対象コードの現状

AI サマリー生成には2つの経路があり、どちらも「日本語箇条書き ・ 始まり、EXACTLY 3項目」という同一ルールを別々のシステムプロンプトに重複して持っている。

- `backend/app/ai/summarizer.py` の `summarize_article()` — 要約のみを (再)生成する単体呼び出し。`POST /api/articles/{id}/summarize` から使用
- `backend/app/ai/processor.py` の `summarize_and_tag()` — 要約 + タグ提案を1回の LLM 呼び出しで行う。SUMMARY セクションに続けてでないとタグを安定生成できないための構成。`background_processor.py` の Phase 1/2、`POST /api/articles/{id}/extract` から使用

> **注記**: `processor.py` / `background_processor.py` のコード内コメントおよび `CLAUDE.md` には "Ternary-Bonsai-8B" と記載されていたが、実際に `com.ccxa.mlx-lm-server` launchd サービスで起動中のモデルは `gemma-4-e4b-it-4bit`（Gemma 4）であることを確認した（`mlx_lm.server --model /Users/sano/models/gemma-4-e4b-it-4bit`、`com.ccxa.snoreader.plist` の `SNOREADER_LLM_MODEL` も同じパス）。本設計のスコープ外だが、ユーザー承認のもと該当箇所（`CLAUDE.md` 2箇所、`processor.py` / `background_processor.py` のコメント）を Gemma 4 表記に修正済み。

## 設計

### 1. 共通ルールモジュールの新設

`backend/app/ai/summary_rules.py` を新規作成し、以下を集約する。

- `MIN_SUMMARY_BULLETS = 1`, `MAX_SUMMARY_BULLETS = 9` の定数
- 共有ルール文言（英語のプロンプト内に埋め込む Rules 断片）:
  - 1〜9個の日本語箇条書き。できるだけ少なく（新情報がある場合のみ項目を追加する）
  - タイトルに書かれている情報を繰り返さない。各箇条書きはタイトルにない情報を追加すること
  - 記事の結論・結果を必ず含め、タイトル + 要約で記事全体が把握できるようにする
  - 意見・推測を書かない（既存ルール継続）
- 共通関数 `finalize_bullets(lines: list[str]) -> str | None`
  - `_TAG_IN_BULLET`（`word|word` 形式の混入行）を除外
  - `MAX_SUMMARY_BULLETS` 件で打ち切り
  - 0件なら `None`（LLM 出力が空/フォーマット違反として扱う。既存の失敗時挙動と同じ）

### 2. `summarizer.py` の変更

- `_SYSTEM_PROMPT` を `summary_rules` のルール文言を使って書き換え。「EXACTLY 3」の文言を除去し、1〜9個・タイトル重複禁止・結論必須の指示に差し替え
- `_clean_summary()` は `summary_rules.finalize_bullets()` を呼ぶだけに簡略化

### 3. `processor.py` の変更

- `_SYSTEM_PROMPT` の SUMMARY ルール部分を同様に書き換え（出力フォーマット例の箇条書きは1個のみ例示に変更し、「これがちょうど3個」という誤解を避ける）
- `_parse_output()` で SUMMARY セクションから集めた `summary_lines` を、最後に `summary_rules.finalize_bullets()` で仕上げる（TAGS セクションの分離ロジック自体は processor.py 固有のまま維持）

### 4. 既存記事の一括再生成

`backend/app/routers/articles.py` に `POST /api/articles/regenerate-summaries` を追加する。既存の `POST /api/articles/regenerate-tag-suggestions`（`articles.py:807`付近）と全く同じパターン:

```python
@router.post("/articles/regenerate-summaries", response_model=dict)
async def regenerate_summaries(session: AsyncSession = Depends(get_session)):
    """既存の ai_summary を NULL に戻し、background processor の Phase 1 に再生成させる。"""
    result = await session.execute(
        update(Article)
        .where(Article.ai_summary.isnot(None))
        .values(ai_summary=None)
    )
    await session.commit()
    return {"cleared": result.rowcount or 0}
```

- フロントエンドの変更は行わない。`regenerate-tag-suggestions` 同様、デプロイ後に手動で `curl -X POST` して叩く管理用エンドポイントとして扱う
- 副作用として、`ai_summary` が NULL に戻った記事は background processor の Phase 1 (`summarize_and_tag`) で要約とタグ提案が両方再生成される。既存の `tag_suggestions` も上書きされるが、これは許容する（`regenerate-tag-suggestions` と対称的な既存アーキテクチャの自然な帰結であり、ユーザーの承認済み）
- 再生成は background processor が1記事ずつ順番に処理するため、記事数によっては数分〜数時間かかる（既存の `regenerate-tag-suggestions` と同じ制約であり、新しいバッチ処理の仕組みは追加しない）

## 既知のリスク

過去のコードコメントから、現行モデル（Gemma 4 / `gemma-4-e4b-it-4bit`）は "EXACTLY 3" のような厳格な指示でないと出力フォーマットを守らない傾向があったことが伺える。「1〜9個、できるだけ少なく」という曖昧な指示にどこまで従うかは実装後の実地確認が必要。パース側は 9 個で打ち切り・0個なら失敗扱いで防御するが、モデルが指示に従わない場合はプロンプトの追加チューニングが必要になる可能性がある。

## テスト方針

- `summary_rules.finalize_bullets()` の単体テストを新規追加（`backend/tests/`）:
  - 通常の1〜9個の ・ 箇条書きがそのまま通ること
  - 10個以上入力した場合に9個で打ち切られること
  - `word|word` 形式の混入行が除外されること
  - 空リスト/該当行なしの場合に `None` を返すこと
- `summarizer.py` / `processor.py` の既存の呼び出し側テストがあればそのまま通ることを確認（現状 `backend/tests/` にこの2ファイル向けの既存テストはない）
- `regenerate-summaries` エンドポイントは `regenerate-tag-suggestions` に既存テストがあればそれに倣ったテストを追加、なければ手動確認（curl で `ai_summary` が NULL になることを確認）で代替

## スコープ外

- LLM モデル自体の変更・ファインチューニング
- フロントエンドの UI 変更（再生成トリガーボタンなど）
- タグ提案ロジック自体の変更（今回はサマリーのルール変更が主目的で、タグ側は既存のまま）
