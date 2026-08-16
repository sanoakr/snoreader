"""テスト全体の共通設定。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_background_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    """lifespan を使うテストで背景 LLM ループを起動しない。

    起動すると開発機で動いているローカル LLM に実際に接続し、テスト用 DB の
    ai_summary / tag_suggestions / chat_suggestions を勝手に書き換えてしまう
    （テストを実 LLM に依存させないというプロジェクトの方針に反する）。
    main.py は ``start`` を import 時に束縛するので、各テストが main を
    reload する前にここで差し替えておく必要がある。
    """
    from app.services import background_processor

    monkeypatch.setattr(background_processor, "start", lambda: None)
