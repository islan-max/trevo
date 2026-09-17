from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.cards.service as cards_service
from app.cards.service import enforce_card_pin_rate_limit, record_card_pin_failure


def test_pin_rate_limit_blocks_after_max_attempts(monkeypatch):
    # Teste de unidade da contagem em memória: sem o patch, `record_card_pin_failure`
    # tenta persistir em card_pin_failures_state e o usuário fictício viola a
    # foreign key para users. O caminho persistido é coberto pelos testes de
    # integração, que criam usuário e cartão de verdade.
    monkeypatch.setattr(cards_service, "storage_available", lambda: False)
    cards_service.card_pin_failures.clear()
    user_id = "00000000-0000-0000-0000-000000000001"
    card_id = 99
    for _ in range(cards_service.PIN_MAX_ATTEMPTS):
        record_card_pin_failure(user_id, card_id)
    with pytest.raises(HTTPException) as exc:
        enforce_card_pin_rate_limit(user_id, card_id)
    assert exc.value.status_code == 429
