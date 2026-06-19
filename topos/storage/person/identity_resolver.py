"""Cross-source person identity resolution (PRD_04)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from ..db.migrations.remediation_person_model import apply_remediation_person_model_up


@dataclass(frozen=True)
class ResolvedPerson:
    person_id: str
    confidence: float
    created: bool = False


class IdentityResolver:
    """Resolve alias strings to stable owner-scoped person_id values."""

    ALIAS_TYPES = frozenset({"phone", "email", "signal_id", "imessage_handle", "display_name"})

    def __init__(self, conn, *, owner_user_id: str) -> None:
        self._conn = conn
        self._owner_user_id = str(owner_user_id or "").strip()
        apply_remediation_person_model_up(conn)

    def ensure_owner_person(self, *, display_name: str = "Owner") -> str:
        row = self._conn.execute(
            """
            SELECT person_id FROM persons
            WHERE owner_user_id=? AND is_owner=1
            LIMIT 1
            """,
            (self._owner_user_id,),
        ).fetchone()
        if row:
            return str(row[0])
        person_id = f"person-owner-{self._owner_user_id[:8]}"
        self._conn.execute(
            """
            INSERT OR IGNORE INTO persons (person_id, owner_user_id, display_name, is_owner)
            VALUES (?, ?, ?, 1)
            """,
            (person_id, self._owner_user_id, display_name),
        )
        self._conn.commit()
        return person_id

    def resolve(
        self,
        alias_type: str,
        alias_value: str,
        *,
        display_name: Optional[str] = None,
        is_owner: bool = False,
    ) -> ResolvedPerson:
        alias_type = str(alias_type or "").strip()
        alias_value = str(alias_value or "").strip()
        if not alias_type or not alias_value:
            raise ValueError("alias_type and alias_value required")
        if alias_type not in self.ALIAS_TYPES:
            raise ValueError(f"Unsupported alias_type: {alias_type}")

        row = self._conn.execute(
            """
            SELECT person_id, confidence FROM person_aliases
            WHERE owner_user_id=? AND alias_type=? AND alias_value=?
            """,
            (self._owner_user_id, alias_type, alias_value),
        ).fetchone()
        if row:
            return ResolvedPerson(person_id=str(row[0]), confidence=float(row[1]), created=False)

        person_id = self.ensure_owner_person() if is_owner else str(uuid.uuid4())
        if not is_owner:
            self._conn.execute(
                """
                INSERT INTO persons (person_id, owner_user_id, display_name, is_owner)
                VALUES (?, ?, ?, 0)
                """,
                (person_id, self._owner_user_id, display_name or alias_value),
            )
        alias_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO person_aliases (
                alias_id, person_id, owner_user_id, alias_type, alias_value, confidence
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (alias_id, person_id, self._owner_user_id, alias_type, alias_value, 1.0),
        )
        self._conn.commit()
        return ResolvedPerson(person_id=person_id, confidence=1.0, created=True)

    def resolve_sender(
        self,
        sender_id: Optional[str],
        *,
        source_id: str,
        is_self: bool = False,
    ) -> Tuple[Optional[str], float]:
        if not sender_id:
            return None, 0.0
        alias_type = "signal_id" if "signal" in source_id else "imessage_handle"
        resolved = self.resolve(alias_type, str(sender_id), is_owner=is_self)
        return resolved.person_id, resolved.confidence
