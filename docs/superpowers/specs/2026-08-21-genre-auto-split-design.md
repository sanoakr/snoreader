# ジャンル自動分割（未読 50 超の検知と分割提案）

- 日付: 2026-08-21
- 状態: 設計合意済み
- 関連: `2026-08-17-genre-subdivision-design.md`（サブジャンル導入）、`2026-08-08-unread-genre-triage-design.md`（ジャンル triage）

## 目的

葉ジャンルの未読が増えすぎると、サイドバーからの一括 triage（mark-all-read / dismiss）が
「多すぎて確認できない束」に対する操作になり、機能として使えなくなる。
未読 50 件を上限として、超えたジャンルに対して**分割案を自動で作り、
ユーザーが 1 クリックで適用できる**ようにする。

## 現状の測定（2026-08-21、未読 427 件）

```
ai_misc 53 | ai_llm 46 | other 46 | economy 39 | politics 37
entertainment 34 | culture 32 | life 28 | security 27 | science 23
dev_prog 19 | sports 18 | incident 12 | dev_tools 7 | dev_infra 2
```

50 超は `ai_misc`（53）のみ。ここから設計上重要な事実が 3 つ出た。

### 1. 受け皿ジャンルは兄弟追加では割れない

`ai_misc` の担当タグは `ai` 1 つだけ。53 件の共起タグは
`security 7 / tools 4 / hardware 4 / government 4 / education 3` と
**すでに他ジャンルにルールがあるタグ**が中心で、未ルールのものは
`waymo 2` `google 2` のような 2 件級しかない。担当タグを機械的に兄弟へ
分配する戦略はここでは空振りし、未ルールタグを昇格させる戦略は
下限を設けないと 2 件のジャンルを量産する。

### 2. `ai` タグが priority で全ジャンルに勝っている

`ai_misc` が最大バケットなのは、親 `ai` の priority が 1 で他の全ジャンルに
勝つため。`ai` + `security` の記事は security ではなく `ai_misc` に落ちる。
`ai` を `is_generic`（通常ルールが 1 つも当たらないときだけ使う）に降格すると、
実データで次のようになる。

| | 現状 | `ai` を汎用に |
|---|---|---|
| 最大バケット | ai_misc **53** | ai_llm **46** |
| 50 超のジャンル | 1 個 | **0 個** |
| ai_misc | 53 | 17 |

つまり現在の超過は、ジャンルを増やさず**ルール 1 行の属性変更**で解消できる。
分割戦略には「増やす」以外にこの手を必ず含める。

### 3. `other` は子を持てない

`other` は `genres` に行を持たない予約キーなので、構造上ぶら下げ先がない。
全記事 17,526 件で分類し直すと `other` は 9%（1,648 件）あり、その中の
未ルールタグは 1,419 種（2 件以上は 499 種）と裾が長い。

## 対象と不変条件

対象は**葉ジャンル**（子を持たない `genres` 行）の未読件数
（`is_read=0 AND is_saved=0 AND dismissed_at IS NULL`、`GET /articles/genres` と同じ定義）。
親の件数は子の合計なので、子が全部上限未満なら親が 100 でも超過とみなさない。

| 超過したもの | 分割先 |
|---|---|
| 子ジャンル | 同じ親の下に**兄弟**を追加 |
| 子を持たない親 | その親の下に**子**を新設 |
| `other` | 新しい**トップレベル**ジャンルを提案 |

いずれも階層は 2 段のまま。孫ジャンルは作らない。

## 分割プランナ

新規 `backend/app/services/genre_split_planner.py`。DB に触らない純関数として実装し、
`(未読記事の id とタグ, GenreRules スナップショット)` から提案リストを返す。
分類は既存 `genre_classifier.classify` を再利用し、ロジックを二重化しない。

```python
@dataclass(frozen=True)
class ProposedChild:
    key: str
    label_ja: str            # LLM 命名。失敗時はタグ名
    tags: tuple[str, ...]
    estimated_unread: int

@dataclass(frozen=True)
class SplitProposal:
    genre_key: str           # 超過している葉ジャンル
    strategy: str            # "demote_generic" | "split_own_tags" | "promote_free_tags"
    before: int
    projected_max: int       # この案が影響するバケットの最大件数（シミュレーション結果）
    children: tuple[ProposedChild, ...]
    demote_tags: tuple[str, ...]   # demote_generic 用
```

### 戦略（成立したものを全部提案し、選択はユーザーに委ねる）

1 つのジャンルに対して複数の戦略が成立することがある。**成立した案は全部提示し、
`projected_max` が小さい順（同値なら下表の順）に並べる。** どれを採るかは意味の
判断（例: 「AI＋セキュリティの記事は security に行くべきか」）を含むので、
機械が 1 つに絞らない。

| | 戦略 | 内容 | 適用条件 |
|---|---|---|---|
| C | `demote_generic` | 受け皿タグを `is_generic` に降格し、他ジャンルのルールに譲る | ジャンルを増やさず済むので最優先 |
| A | `split_own_tags` | 担当タグを件数降順に貪欲に詰めて兄弟を作る（各ビン上限の 80% まで）。最多の受け皿タグは元のジャンルに残す | 担当タグが 2 個以上 |
| B | `promote_free_tags` | 未ルールの共起タグを件数降順に新しい兄弟の担当タグにする | そのタグの記事が `_MIN_CHILD_ARTICLES` 以上 |

### シミュレーションによる検証（設計の中核）

**どの案も、実際の `classify` を候補ルールに対して未読記事全件に走らせ、
適用後の件数を実測してから提案する。** 推測した件数は出さない。

これは既存 `genre_seed.py` のコメントにある罠を潰すために必須である——
兄弟は親と同じ priority を持つので必ず同順位になり、`_resolve` の同値解決
（キーの辞書順）で決まる。したがって受け皿より辞書順で後にソートされる
キーを付けた新兄弟は、記事を 1 件も取れない。シミュレーションを通せば、
この失敗は「projected 件数 0」として自動的に検出され、案は棄却される。

棄却理由は提案に残さず、単に候補から落とす。

### `projected_max` は corpus 全体の最大値ではない

`projected_max` は「この案が影響するバケット」——適用の前後で件数が変化した
ジャンル（受け取った側と失った側の両方）と対象ジャンル自身——の最大件数である。
corpus 全体の最大値を採ると、**無関係なジャンルが 1 つ上限を超えているだけで
正しい案が全部棄却される**（戦略 C は「譲られた側が溢れないこと」を
`max(projected.values()) > limit` で見るため）。本番では `other` が 46 件あり、
これが 51 に育った瞬間に `ai_misc` の降格案が出なくなる、という形で仕組み全体が
無言で止まる。実装は `_affected_max(current, projected, genre_key)` に閉じ込める。

### 新しいキーは既存ジャンルと衝突してはならない

新兄弟・新トップレベルのキーが既存ジャンルのキーと一致する案は棄却する。
適用すると既存ジャンルを別の親の下に付け替え priority も上書きしてしまう
（データ破壊）。同一案の中でのキー重複も同様に棄却する。

### 定数

| 定数 | 既定値 | 根拠 |
|---|---|---|
| `SNOREADER_GENRE_UNREAD_LIMIT` | 50 | 設定（env）。一括 triage で確認できる上限 |
| `_MIN_CHILD_ARTICLES` | 8 | 下限なしだと `waymo 2` のような 2 件ジャンルができる（測定済み） |
| `_MAX_NEW_CHILDREN` | 4 | 1 提案で辞書が大きく動きすぎないように |

### ラベル生成

提案作成時に 1 回だけ LLM を呼び、タグ集合から日本語名を付ける
（`task_queue` の `reserved` レーン、foreground 優先度）。失敗したらタグ名に
フォールバックし、承認ダイアログで編集できる。
**分類そのものは従来通り辞書のみで、LLM に依存しない。**

## 提案の保存と適用

新テーブル `genre_split_suggestions`:

| 列 | 型 | 意味 |
|---|---|---|
| `id` | int PK | |
| `genre_key` | str | 超過していた葉ジャンル |
| `strategy` | str | 戦略名 |
| `payload` | str (JSON) | `SplitProposal` のシリアライズ |
| `before` | int | 検知時の未読件数 |
| `projected_max` | int | 適用後の最大バケット |
| `created_at` | datetime | |
| `dismissed_at` | datetime? | 無視した時刻 |
| `dismissed_at_count` | int? | 無視した時点の未読件数 |

1 ジャンルに複数行（複数の戦略）が並ぶ。

永続化する理由: 検知はフィード取得サイクル（1 時間ごと）で走り、閲覧は後から。
LaunchAgent は `make deploy` で頻繁に再起動するため、メモリ保持では LLM 命名を
毎回やり直すことになる。「無視」も永続が必要で、`dismissed_at_count` より
未読が増えたときだけ再提案する（毎時間つつかない）。

**無視と適用はどちらもジャンル単位で効く。** `dismiss` はその `genre_key` の
保留中の行すべてに `dismissed_at` を立てる（1 つの案だけ消えて残りが居座るのは
「無視した」という意思に反する）。`apply` も同様に、採用しなかった同ジャンルの
案を同時に閉じる——辞書が変わった後では他の案の `projected_max` は無効だからだ。

### エンドポイント（`routers/genres.py` に追加）

```
GET  /genres/split-suggestions              保留中の一覧
POST /genres/split-suggestions/{id}/apply   body でラベル編集を受ける
POST /genres/split-suggestions/{id}/dismiss  同ジャンルの保留を全部閉じる
POST /genres/split-suggestions/refresh      手動再計算（テストでも使う）
```

`apply` は 1 トランザクションで「子作成 / ルール移動 / `is_generic` 変更」を行い、
`reclassify_all()` を呼んで commit する。既存のジャンル変更と同じ作法で
`reclassified` 件数を返す。

**コスト**: `reclassify_all` は本番実測 47 秒（`articles` のどの列を更新しても
FTS トリガーで本文が再インデックスされる）。したがって走るのは apply の時だけ。
検知側は未読数百行 × 3 列の SELECT とメモリ内シミュレーションで済む。

## 検知の組み込み

`fetch_all_feeds()` の末尾（fetch → dedup → cleanup の後）に
`refresh_split_suggestions()` を 1 回呼ぶ。未読が増えるのはフィードを取得した
瞬間だけなので、これ以外の場所でチェックする意味がない。

## フロントエンド

- `hooks/useSplitSuggestions.ts` — 一覧・apply・dismiss
- `components/layout/GenreManagerModal.tsx` に提案パネル
  （before → after の件数、新しい子のキーとタグ、編集可能なラベル、[適用] [無視]）
- `FeedSidebar.tsx` のジャンル節にバッジ（保留中の件数）

## テスト

新規 `backend/tests/test_genre_split_planner.py`（LLM も DB も使わない純関数テスト）:

- 多タグの葉が上限超 → 戦略 A が全ビンを上限未満にする
- 単一タグの受け皿が上限超 → 戦略 C が降格を提案し、シミュレート後の最大が上限以下
- 未ルール共起タグが `_MIN_CHILD_ARTICLES` 未満 → 提案しない（2 件ジャンルを作らない）
- 受け皿より辞書順で後になる兄弟キー → シミュレーションで棄却される
- `other` 超過 → 新トップレベルの提案になる
- 上限以下のジャンルしかない → 提案なし
- 複数戦略が成立するジャンル → 全案が `projected_max` 昇順で返る

`test_genres_api.py` に追加:

- `apply` が子を作り、ルールを移し、記事を再分類する
- `dismiss` 後は未読が増えるまで再提案されない
- 同一ジャンルに複数案があるとき、1 つ適用すると残りも閉じる
- LLM 命名はモックする（live サーバに依存しない）

## 意図的に入れないもの

**逆向きの「統合提案」**（未読が減って小さくなった子ジャンルをまとめ直す機能）は
実装しない。未読件数は取得と既読で常に上下するのにジャンルは永続するので、
51 件で分割して読み進めれば 5 件のジャンルが残る——これは実際に起きる。しかし

1. まだ困っていない（YAGNI）
2. 「勝手に辞書が縮む」のは提案モデル（提案までは自動・適用は 1 クリック）と相性が悪い

運用して実際に邪魔になったら、独立機能ではなく**同じ `genre_split_suggestions` の
仕組みに逆向きの提案種別として足す**。テーブルと承認 UI は流用できる形にしてある。
