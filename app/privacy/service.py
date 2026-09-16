"""LGPD data-subject services (pure DB logic; no FastAPI/HTTP coupling).

These take an open cursor (RealDictCursor) so they compose inside the existing
request handlers without a circular import against app.main.
"""

from __future__ import annotations

import re
from typing import Any

# Bump when the privacy policy / terms change so re-acceptance can be required.
POLICY_VERSION = "2025-07-01"

# Profile columns that are safe to export (never the password hash).
_PROFILE_COLUMNS = (
    "id, email, name, avatar_url, auth_provider, oauth_subject, "
    "send_monthly_summary, is_active, password_changed_at, created_at, updated_at"
)

# Operational/security tables are not personal content and must not be exported.
_SKIP_TABLES = {"revoked_tokens"}
_TABLE_NAME_RE = re.compile(r"^[a-z_]+$")

# SEC-08: card_pins não é pulada inteira (o usuário tem direito de saber que
# tem um PIN cadastrado em qual cartão) — só a coluna do hash, pelo mesmo
# motivo que _PROFILE_COLUMNS nunca inclui o hash de senha.
_TABLE_EXCLUDED_COLUMNS: dict[str, set[str]] = {
    "card_pins": {"pin_hash"},
}


def record_consent(
    cursor: Any,
    user_id: str,
    *,
    scope: str = "terms_privacy",
    granted: bool = True,
    ip_hash: str | None = None,
    channel: str = "register",
    policy_version: str = POLICY_VERSION,
) -> None:
    cursor.execute(
        """
        INSERT INTO consents (user_id, policy_version, scope, granted, ip_hash, channel)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (user_id, policy_version, scope, granted, ip_hash, channel),
    )


def latest_consents(cursor: Any, user_id: str) -> list[dict]:
    cursor.execute(
        """
        SELECT DISTINCT ON (scope) scope, policy_version, granted, channel, created_at
        FROM consents
        WHERE user_id = %s
        ORDER BY scope, created_at DESC
        """,
        (user_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def build_data_export(cursor: Any, user_id: str) -> dict:
    """Assemble every stored record tied to the user (Art. 18 access/portability).

    Discovers user-scoped tables via information_schema so new domains are
    included automatically, skipping operational/security state tables.
    """
    export: dict[str, Any] = {"policy_version": POLICY_VERSION}

    # _PROFILE_COLUMNS is a hardcoded constant; user_id is parameterized.
    cursor.execute(f"SELECT {_PROFILE_COLUMNS} FROM users WHERE id = %s", (user_id,))  # nosec B608
    row = cursor.fetchone()
    export["profile"] = dict(row) if row else None

    cursor.execute(
        """
        SELECT DISTINCT table_name
        FROM information_schema.columns
        WHERE column_name = 'user_id' AND table_schema = 'public'
        """
    )
    tables = sorted({r["table_name"] for r in cursor.fetchall()})
    for table in tables:
        if not _TABLE_NAME_RE.match(table) or table.endswith("_state") or table in _SKIP_TABLES:
            continue
        excluded_columns = _TABLE_EXCLUDED_COLUMNS.get(table)
        # table is validated by _TABLE_NAME_RE and sourced from information_schema;
        # user_id is parameterized.
        if excluded_columns:
            cursor.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND table_schema = 'public'
                ORDER BY ordinal_position
                """,
                (table,),
            )
            columns = [
                r["column_name"]
                for r in cursor.fetchall()
                if r["column_name"] not in excluded_columns and _TABLE_NAME_RE.match(r["column_name"])
            ]
            column_list = ", ".join(f'"{column}"' for column in columns)
            cursor.execute(f'SELECT {column_list} FROM "{table}" WHERE user_id = %s', (user_id,))  # nosec B608
        else:
            cursor.execute(f'SELECT * FROM "{table}" WHERE user_id = %s', (user_id,))  # nosec B608
        export[table] = [dict(r) for r in cursor.fetchall()]

    return export
