from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chunk_to_sqlite import ensure_schema, utc_now


class BlockError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_range(start: Any, end: Any) -> tuple[int, int]:
    if not isinstance(start, int) or isinstance(start, bool) or start <= 0:
        raise BlockError("INVALID_ARGUMENT", "start_line 必须是正整数")
    if not isinstance(end, int) or isinstance(end, bool) or end < start:
        raise BlockError("INVALID_ARGUMENT", "end_line 必须不小于 start_line")
    return start, end


class BlockStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(self.connect()) as conn, conn:
            ensure_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _authorize(row: sqlite3.Row | None, key: str) -> None:
        if row is None:
            raise BlockError("BLOCK_NOT_FOUND", "块不存在")
        if not isinstance(key, str) or not hmac.compare_digest(row["access_key"], key):
            raise BlockError("INVALID_KEY", "块密钥错误")

    @staticmethod
    def _check_overlap(conn: sqlite3.Connection, source_id: int, start: int, end: int, exclude: int | None = None) -> None:
        sql = "SELECT 1 FROM chunks WHERE source_id=? AND start_line <= ? AND end_line >= ?"
        args: list[Any] = [source_id, end, start]
        if exclude is not None:
            sql += " AND id <> ?"
            args.append(exclude)
        if conn.execute(sql, args).fetchone():
            raise BlockError("LINE_RANGE_CONFLICT", "块行范围与已有块交叠")

    def read(self, block_id: int, key: str) -> dict[str, Any]:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM chunks WHERE id=?", (block_id,)).fetchone()
            self._authorize(row, key)
            return {name: row[name] for name in ("id", "source_id", "chunk_index", "start_line", "end_line", "text", "created_at")}

    def create(self, block: dict[str, Any]) -> tuple[dict[str, Any], str]:
        start, end = _validate_range(block.get("start_line"), block.get("end_line"))
        text = block.get("text")
        if not isinstance(text, str):
            raise BlockError("INVALID_ARGUMENT", "text 必须是字符串")
        source_id, index = block.get("source_id"), block.get("chunk_index")
        if not isinstance(source_id, int) or not isinstance(index, int):
            raise BlockError("INVALID_ARGUMENT", "source_id 和 chunk_index 必须是整数")
        with closing(self.connect()) as conn, conn:
            if not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BlockError("INVALID_ARGUMENT", "source_id 不存在")
            self._check_overlap(conn, source_id, start, end)
            for _ in range(10):
                key = secrets.token_hex(8)
                try:
                    cursor = conn.execute("""
                        INSERT INTO chunks (id, source_id, chunk_index, start_line, end_line, text, chunk_sha256, access_key, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (block.get("id"), source_id, index, start, end, text, _hash(text), key, utc_now()))
                    return {"id": int(cursor.lastrowid), "source_id": source_id, "chunk_index": index,
                            "start_line": start, "end_line": end, "text": text}, key
                except sqlite3.IntegrityError as exc:
                    if "access_key" not in str(exc):
                        raise BlockError("BLOCK_CONFLICT", "块编号或数据约束冲突") from exc
            raise BlockError("INTERNAL_ERROR", "无法生成唯一块密钥")

    def rewrite(self, block: dict[str, Any], key: str) -> dict[str, Any]:
        block_id = block.get("id")
        start, end = _validate_range(block.get("start_line"), block.get("end_line"))
        text = block.get("text")
        if not isinstance(block_id, int) or not isinstance(text, str):
            raise BlockError("INVALID_ARGUMENT", "id 必须是整数且 text 必须是字符串")
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM chunks WHERE id=?", (block_id,)).fetchone()
            self._authorize(row, key)
            self._check_overlap(conn, row["source_id"], start, end, block_id)
            cursor = conn.execute("""
                UPDATE chunks SET start_line=?, end_line=?, text=?, chunk_sha256=?
                WHERE id=? AND access_key=?
            """, (start, end, text, _hash(text), block_id, key))
            if cursor.rowcount != 1:
                raise BlockError("CONCURRENT_CONFLICT", "块已被并发修改")
        return self.read(block_id, key)

    def delete(self, block_id: int, key: str) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT access_key FROM chunks WHERE id=?", (block_id,)).fetchone()
            self._authorize(row, key)
            if conn.execute("DELETE FROM chunks WHERE id=? AND access_key=?", (block_id, key)).rowcount != 1:
                raise BlockError("CONCURRENT_CONFLICT", "块已被并发修改")


class OwnershipStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("""CREATE TABLE IF NOT EXISTS graph_nodes (
                graph_node_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS block_ownership (
                graph_node_id TEXT NOT NULL, block_id INTEGER NOT NULL UNIQUE, block_key TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(graph_node_id, block_id),
                FOREIGN KEY(graph_node_id) REFERENCES graph_nodes(graph_node_id) ON DELETE CASCADE)""")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_node(self, conn: sqlite3.Connection, node: str) -> None:
        now = utc_now()
        conn.execute("INSERT OR IGNORE INTO graph_nodes VALUES (?, ?, ?)", (node, now, now))
        conn.execute("UPDATE graph_nodes SET updated_at=? WHERE graph_node_id=?", (now, node))

    def assign(self, node: str, block_id: int, key: str) -> None:
        with closing(self.connect()) as conn, conn:
            self.ensure_node(conn, node)
            now = utc_now()
            try:
                conn.execute("INSERT INTO block_ownership VALUES (?, ?, ?, ?, ?)", (node, block_id, key, now, now))
            except sqlite3.IntegrityError as exc:
                raise BlockError("BLOCK_CONFLICT", "块已归属于图谱节点") from exc

    def key_for(self, node: str, block_id: int) -> str:
        with closing(self.connect()) as conn:
            if not conn.execute("SELECT 1 FROM graph_nodes WHERE graph_node_id=?", (node,)).fetchone():
                raise BlockError("GRAPH_NODE_NOT_FOUND", "图谱节点不存在")
            row = conn.execute("SELECT block_key FROM block_ownership WHERE graph_node_id=? AND block_id=?", (node, block_id)).fetchone()
            if not row:
                raise BlockError("NOT_OWNER", "图谱节点不拥有该块")
            return str(row[0])

    def unassign(self, node: str, block_id: int) -> None:
        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM block_ownership WHERE graph_node_id=? AND block_id=?", (node, block_id))
