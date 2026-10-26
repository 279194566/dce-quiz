from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parent.parent
MIGRATION_FILE = ROOT / "migrations" / "001_initial.sql"
DB_INTEGRITY_ERRORS: tuple[type[BaseException], ...] = (psycopg.IntegrityError,)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(buffer).strip().rstrip(";").strip()
            buffer = []
            if statement:
                statements.append(statement)
    if any(part.strip() for part in buffer):
        raise RuntimeError("数据库迁移文件末尾缺少分号")
    return statements


def _placeholders(sql: str) -> str:
    """Translate the service layer's neutral placeholders for psycopg."""

    output: list[str] = []
    in_string = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            if in_string and index + 1 < len(sql) and sql[index + 1] == "'":
                output.append("''")
                index += 2
                continue
            in_string = not in_string
            output.append(char)
        elif char == "?" and not in_string:
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)


class DatabaseConnection:
    def __init__(self, raw: psycopg.Connection):
        self.raw = raw
        self.cursor = raw.cursor()

    def execute(self, sql: str, params=()):
        self.cursor.execute(_placeholders(sql), params)
        return self.cursor

    def executemany(self, sql: str, params):
        self.cursor.executemany(_placeholders(sql), params)
        return self.cursor

    def executescript(self, script: str) -> None:
        for statement in _split_sql(script):
            self.execute(statement)


class Database:
    def __init__(self, url: str):
        self.url = url.strip()
        if not self.url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("Database 仅支持 PostgreSQL 连接串")

    @contextmanager
    def connect(self):
        raw = psycopg.connect(self.url, row_factory=dict_row, autocommit=False)
        connection = DatabaseConnection(raw)
        try:
            yield connection
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()

    def initialize(self) -> None:
        if not MIGRATION_FILE.is_file():
            raise RuntimeError(f"缺少数据库迁移文件：{MIGRATION_FILE}")
        migration = MIGRATION_FILE.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7165202601)")
            conn.executescript(migration)
            conn.execute(
                """
                INSERT INTO schema_meta (key, value)
                VALUES ('schema_version', '1')
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """
            )

    def save_logout_hint(self, id_token: str) -> str:
        hint_id = new_id()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute("DELETE FROM oidc_logout_hints WHERE created_at < ?", (cutoff,))
            conn.execute(
                "INSERT INTO oidc_logout_hints (id, id_token, created_at) VALUES (?, ?, ?)",
                (hint_id, id_token, utcnow()),
            )
        return hint_id

    def get_logout_hint(self, hint_id: str) -> str:
        if not hint_id:
            return ""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id_token FROM oidc_logout_hints WHERE id = ?", (hint_id,)
            ).fetchone()
        return str(row["id_token"]) if row else ""

    def delete_logout_hint(self, hint_id: str) -> None:
        if not hint_id:
            return
        with self.connect() as conn:
            conn.execute("DELETE FROM oidc_logout_hints WHERE id = ?", (hint_id,))

    def audit(
        self,
        action: str,
        target_type: str,
        target_id: str,
        result: str,
        *,
        actor_user_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events
                  (id, actor_user_id, action, target_type, target_id, result, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    actor_user_id,
                    action,
                    target_type,
                    target_id,
                    result,
                    json.dumps(detail or {}, ensure_ascii=False),
                    utcnow(),
                ),
            )

    def upsert_default_permission_package(
        self,
        *,
        group_id: str,
        group_name: str,
        workspace_id: int,
        workspace_alias: str,
        workspace_role: str,
    ) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO permission_packages
                  (id, name, dce_group_id, dce_group_name, description, target_summary,
                   enabled, last_validated_at, created_at)
                VALUES ('package-dce-target', ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                  dce_group_id = EXCLUDED.dce_group_id,
                  dce_group_name = EXCLUDED.dce_group_name,
                  description = EXCLUDED.description,
                  enabled = 1,
                  last_validated_at = EXCLUDED.last_validated_at
                """,
                (
                    f"{workspace_alias} {workspace_role}",
                    group_id,
                    group_name,
                    "经 DCE 预授权用户组提供工作空间权限",
                    f"工作空间 {workspace_alias}（ID {workspace_id}）/ {workspace_role}",
                    now,
                    now,
                ),
            )

