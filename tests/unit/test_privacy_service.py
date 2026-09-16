from __future__ import annotations

from app.privacy.service import POLICY_VERSION, build_data_export, record_consent


class FakeCursor:
    """Minimal RealDictCursor stand-in driven by canned results."""

    def __init__(self, user_tables, rows_by_table, columns_by_table=None):
        self.user_tables = user_tables
        self.rows_by_table = rows_by_table
        self.columns_by_table = columns_by_table or {}
        self.executed = []
        self._one = None
        self._all = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "information_schema.columns" in normalized and "column_name = 'user_id'" in normalized:
            self._all = [{"table_name": t} for t in self.user_tables]
        elif "information_schema.columns" in normalized and "table_name = %s" in normalized:
            table = params[0]
            self._all = [{"column_name": c} for c in self.columns_by_table.get(table, [])]
        elif "FROM users WHERE id" in normalized:
            self._one = {"id": "u1", "email": "a@b.c", "name": "A"}
        elif 'FROM "' in normalized:
            table = normalized.split('FROM "', 1)[1].split('"', 1)[0]
            self._all = self.rows_by_table.get(table, [])

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


def test_build_data_export_skips_operational_tables():
    cursor = FakeCursor(
        user_tables=[
            "transactions",
            "cards",
            "card_pin_failures_state",  # operational → skip
            "revoked_tokens",  # security → skip
            "BadName",  # invalid identifier → skip
        ],
        rows_by_table={"transactions": [{"id": 1}], "cards": []},
    )

    export = build_data_export(cursor, "u1")

    assert export["policy_version"] == POLICY_VERSION
    assert export["profile"]["email"] == "a@b.c"
    assert export["transactions"] == [{"id": 1}]
    assert export["cards"] == []
    assert "card_pin_failures_state" not in export
    assert "revoked_tokens" not in export
    assert "BadName" not in export

    # The profile query must never select the password hash.
    profile_sql = next(sql for sql, _ in cursor.executed if "FROM users WHERE id" in sql)
    assert "hashed_password" not in profile_sql


def test_build_data_export_excludes_pin_hash_from_card_pins():
    """SEC-08: card_pins não é pulada inteira — só o hash do PIN fica de
    fora, pelo mesmo motivo que o perfil nunca inclui a senha."""
    cursor = FakeCursor(
        user_tables=["card_pins"],
        rows_by_table={"card_pins": [{"id": "p1", "card_id": 1, "created_at": "2026-01-01"}]},
        columns_by_table={"card_pins": ["id", "card_id", "user_id", "pin_hash", "created_at"]},
    )

    export = build_data_export(cursor, "u1")

    assert export["card_pins"] == [{"id": "p1", "card_id": 1, "created_at": "2026-01-01"}]
    select_sql = next(sql for sql, _ in cursor.executed if 'FROM "card_pins"' in sql)
    assert "pin_hash" not in select_sql
    assert '"id"' in select_sql and '"card_id"' in select_sql


def test_record_consent_inserts_ledger_row():
    cursor = FakeCursor([], {})
    record_consent(cursor, "u1", scope="monthly_summary", granted=False, ip_hash="deadbeef", channel="settings")

    sql, params = cursor.executed[-1]
    assert "INSERT INTO consents" in sql
    assert params == ("u1", POLICY_VERSION, "monthly_summary", False, "deadbeef", "settings")
