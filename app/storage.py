from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class Message:
    message_id: str
    group_id: str
    user_id: str
    sender_name: str
    content: str
    timestamp: int


class Store:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY,
                    group_name TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    FOREIGN KEY (group_id) REFERENCES groups(group_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_group_time
                    ON messages(group_id, timestamp, message_id);

                CREATE TABLE IF NOT EXISTS summary_cursors (
                    group_id TEXT PRIMARY KEY,
                    last_message_id TEXT,
                    last_timestamp INTEGER,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (group_id) REFERENCES groups(group_id)
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    from_message_id TEXT,
                    to_message_id TEXT,
                    from_timestamp INTEGER,
                    to_timestamp INTEGER,
                    message_count INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (group_id) REFERENCES groups(group_id)
                );
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(summaries)").fetchall()
            }
            if "is_read" not in columns:
                conn.execute("ALTER TABLE summaries ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0")

    def upsert_group(self, group_id: str, group_name: str, updated_at: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO groups (group_id, group_name, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    group_name = excluded.group_name,
                    updated_at = excluded.updated_at
                """,
                (group_id, group_name or group_id, updated_at),
            )

    def save_message(self, message: Message, raw: dict[str, Any]) -> bool:
        with self.connect() as conn:
            result = conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (message_id, group_id, user_id, sender_name, content, timestamp, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.group_id,
                    message.user_id,
                    message.sender_name,
                    message.content,
                    message.timestamp,
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
            return result.rowcount > 0

    def list_groups(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    g.group_id,
                    g.group_name,
                    g.updated_at,
                    COUNT(m.message_id) AS message_count,
                    SUM(
                        CASE
                            WHEN c.last_timestamp IS NULL THEN 1
                            WHEN m.timestamp > c.last_timestamp THEN 1
                            WHEN m.timestamp = c.last_timestamp AND m.message_id > COALESCE(c.last_message_id, '') THEN 1
                            ELSE 0
                        END
                    ) AS unread_count,
                    MAX(m.timestamp) AS latest_timestamp
                FROM groups g
                LEFT JOIN messages m ON m.group_id = g.group_id
                LEFT JOIN summary_cursors c ON c.group_id = g.group_id
                GROUP BY g.group_id
                ORDER BY latest_timestamp DESC, g.updated_at DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT group_id, group_name, updated_at FROM groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_message(self, message_id: str) -> Message | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT message_id, group_id, user_id, sender_name, content, timestamp
                FROM messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            return Message(**dict(row)) if row else None

    def list_messages_with_raw_cq(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, group_id, user_id, sender_name, content, timestamp, raw_json
                FROM messages
                WHERE content LIKE '%[CQ:%'
                ORDER BY timestamp ASC, message_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_message_content(self, message_id: str, content: str) -> bool:
        with self.connect() as conn:
            result = conn.execute(
                "UPDATE messages SET content = ? WHERE message_id = ?",
                (content, message_id),
            )
            return result.rowcount > 0

    def get_unread_messages(self, group_id: str, limit: int = 500) -> list[Message]:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                SELECT last_message_id, last_timestamp
                FROM summary_cursors
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
            if cursor is None or cursor["last_timestamp"] is None:
                rows = conn.execute(
                    """
                    SELECT message_id, group_id, user_id, sender_name, content, timestamp
                    FROM messages
                    WHERE group_id = ?
                    ORDER BY timestamp ASC, message_id ASC
                    LIMIT ?
                    """,
                    (group_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT message_id, group_id, user_id, sender_name, content, timestamp
                    FROM messages
                    WHERE group_id = ?
                      AND (
                        timestamp > ?
                        OR (timestamp = ? AND message_id > ?)
                      )
                    ORDER BY timestamp ASC, message_id ASC
                    LIMIT ?
                    """,
                    (
                        group_id,
                        cursor["last_timestamp"],
                        cursor["last_timestamp"],
                        cursor["last_message_id"] or "",
                        limit,
                    ),
                ).fetchall()
            return [Message(**dict(row)) for row in rows]

    def get_unread_message_records(self, group_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                SELECT last_message_id, last_timestamp
                FROM summary_cursors
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
            if cursor is None or cursor["last_timestamp"] is None:
                rows = conn.execute(
                    """
                    SELECT message_id, group_id, user_id, sender_name, content, timestamp, raw_json
                    FROM messages
                    WHERE group_id = ?
                    ORDER BY timestamp ASC, message_id ASC
                    LIMIT ?
                    """,
                    (group_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT message_id, group_id, user_id, sender_name, content, timestamp, raw_json
                    FROM messages
                    WHERE group_id = ?
                      AND (
                        timestamp > ?
                        OR (timestamp = ? AND message_id > ?)
                      )
                    ORDER BY timestamp ASC, message_id ASC
                    LIMIT ?
                    """,
                    (
                        group_id,
                        cursor["last_timestamp"],
                        cursor["last_timestamp"],
                        cursor["last_message_id"] or "",
                        limit,
                    ),
                ).fetchall()
            return [dict(row) for row in rows]

    def list_recent_messages(self, group_id: str, limit: int = 50) -> list[Message]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, group_id, user_id, sender_name, content, timestamp
                FROM messages
                WHERE group_id = ?
                ORDER BY timestamp DESC, message_id DESC
                LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()
            return [Message(**dict(row)) for row in reversed(rows)]

    def list_recent_message_records(self, group_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, group_id, user_id, sender_name, content, timestamp, raw_json
                FROM messages
                WHERE group_id = ?
                ORDER BY timestamp DESC, message_id DESC
                LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def list_history_message_records(
        self,
        group_id: str,
        limit: int = 50,
        before_timestamp: int | None = None,
        before_message_id: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(int(limit), 1)
        filters = ["group_id = ?"]
        params: list[Any] = [group_id]
        if start_timestamp is not None:
            filters.append("timestamp >= ?")
            params.append(start_timestamp)
        if end_timestamp is not None:
            filters.append("timestamp < ?")
            params.append(end_timestamp)
        if before_timestamp is not None and before_message_id is not None:
            filters.append("(timestamp < ? OR (timestamp = ? AND message_id < ?))")
            params.extend([before_timestamp, before_timestamp, before_message_id])

        where_clause = " AND ".join(filters)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT message_id, group_id, user_id, sender_name, content, timestamp, raw_json
                FROM messages
                WHERE {where_clause}
                ORDER BY timestamp DESC, message_id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def count_message_records(
        self,
        group_id: str,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> int:
        filters = ["group_id = ?"]
        params: list[Any] = [group_id]
        if start_timestamp is not None:
            filters.append("timestamp >= ?")
            params.append(start_timestamp)
        if end_timestamp is not None:
            filters.append("timestamp < ?")
            params.append(end_timestamp)

        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM messages
                WHERE {" AND ".join(filters)}
                """,
                params,
            ).fetchone()
            return int(row["count"] or 0)

    def count_message_records_in_range(
        self,
        group_id: str,
        start_timestamp: int,
        end_timestamp: int,
    ) -> int:
        return self.count_message_records(group_id, start_timestamp, end_timestamp)

    def delete_message_records_in_range(
        self,
        group_id: str,
        start_timestamp: int,
        end_timestamp: int,
    ) -> int:
        with self.connect() as conn:
            result = conn.execute(
                """
                DELETE FROM messages
                WHERE group_id = ?
                  AND timestamp >= ?
                  AND timestamp < ?
                """,
                (group_id, start_timestamp, end_timestamp),
            )
            deleted_count = int(result.rowcount or 0)
        if deleted_count:
            vacuum_conn = sqlite3.connect(self.database_path)
            try:
                vacuum_conn.execute("VACUUM")
            finally:
                vacuum_conn.close()
        return deleted_count

    def save_summary(
        self,
        group_id: str,
        messages: list[Message],
        summary: str,
        model: str,
        created_at: int,
        mark_read: bool = True,
    ) -> int:
        if not messages:
            raise ValueError("Cannot save a summary without messages")

        first = messages[0]
        last = messages[-1]
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO summaries (
                    group_id,
                    from_message_id,
                    to_message_id,
                    from_timestamp,
                    to_timestamp,
                    message_count,
                    model,
                    summary,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    first.message_id,
                    last.message_id,
                    first.timestamp,
                    last.timestamp,
                    len(messages),
                    model,
                    summary,
                    created_at,
                ),
            )
            summary_id = int(cursor.lastrowid)
            if mark_read:
                conn.execute(
                    """
                    INSERT INTO summary_cursors (group_id, last_message_id, last_timestamp, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        last_message_id = excluded.last_message_id,
                        last_timestamp = excluded.last_timestamp,
                        updated_at = excluded.updated_at
                    """,
                    (group_id, last.message_id, last.timestamp, created_at),
                )
            return summary_id

    def list_summaries(
        self,
        group_id: str,
        limit: int = 20,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        filters = ["group_id = ?"]
        params: list[Any] = [group_id]
        if before_id is not None:
            filters.append("id < ?")
            params.append(before_id)

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, group_id, from_message_id, to_message_id, from_timestamp,
                       to_timestamp, message_count, model, summary, is_read, created_at
                FROM summaries
                WHERE {" AND ".join(filters)}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def count_summaries(self, group_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM summaries
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
            return int(row["count"] or 0)

    def mark_summary_read(self, group_id: str, summary_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT id
                FROM summaries
                WHERE group_id = ? AND id = ?
                """,
                (group_id, summary_id),
            ).fetchone()
            if existing is None:
                return None

            conn.execute(
                "UPDATE summaries SET is_read = 1 WHERE group_id = ? AND id = ?",
                (group_id, summary_id),
            )
            row = conn.execute(
                """
                SELECT id, group_id, from_message_id, to_message_id, from_timestamp,
                       to_timestamp, message_count, model, summary, is_read, created_at
                FROM summaries
                WHERE group_id = ? AND id = ?
                """,
                (group_id, summary_id),
            ).fetchone()
            return dict(row) if row else None

    def mark_read(self, group_id: str, now: int) -> dict[str, Any]:
        messages = self.list_recent_messages(group_id, limit=1)
        if not messages:
            raise ValueError("No messages to mark as read")

        last = messages[-1]
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO summary_cursors (group_id, last_message_id, last_timestamp, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    last_message_id = excluded.last_message_id,
                    last_timestamp = excluded.last_timestamp,
                    updated_at = excluded.updated_at
                """,
                (group_id, last.message_id, last.timestamp, now),
            )
        return {"last_message_id": last.message_id, "last_timestamp": last.timestamp}
