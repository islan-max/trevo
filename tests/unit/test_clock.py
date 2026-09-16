from __future__ import annotations

from datetime import date

from freezegun import freeze_time

from app.shared import clock
from app.shared.dates import get_current_month


def test_current_month_respects_brazil_timezone_not_utc():
    """DOM-04: get_current_month() usava datetime.now(UTC) diretamente. Às
    23h30 de 30/09 em America/Sao_Paulo (UTC-3) já são 02h30 de 01/10 em
    UTC — o mês contábil trocava 3 horas antes da virada real no Brasil."""
    # 30/09/2026 23:30 em America/Sao_Paulo == 01/10/2026 02:30 em UTC.
    with freeze_time("2026-10-01 02:30:00"):
        assert get_current_month() == "2026-09"
        assert clock.current_month() == "2026-09"
        assert clock.today() == date(2026, 9, 30)


def test_today_after_local_midnight_advances_to_next_day():
    # 01/10/2026 00:30 em America/Sao_Paulo == 01/10/2026 03:30 em UTC.
    with freeze_time("2026-10-01 03:30:00"):
        assert clock.today() == date(2026, 10, 1)
        assert clock.current_month() == "2026-10"


def test_now_is_timezone_aware_in_business_timezone():
    with freeze_time("2026-09-15 12:00:00"):
        now = clock.now()
        assert now.tzinfo is not None
        # UTC-3 em America/Sao_Paulo (sem horário de verão atualmente).
        assert now.utcoffset().total_seconds() == -3 * 3600
        assert now.hour == 9  # 12:00 UTC - 3h
