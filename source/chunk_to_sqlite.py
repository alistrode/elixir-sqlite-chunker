#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import secrets
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ELIXIR_EXTENSIONS = {".ex", ".exs"}
BASE_DIR = Path(__file__).resolve().parent


class ImportError(ValueError):
    pass


@dataclass(frozen=True)
class Chunk:
    index: int
    start_line: int
    end_line: int
    text: str
    sha256: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_language(path: Path) -> str:
    return "elixir" if path.suffix.lower() in ELIXIR_EXTENSIONS else "plain"


def parse_elixir_spans(source: Path) -> list[tuple[int, int]]:
    executable = shutil.which("elixir.bat" if __import__("os").name == "nt" else "elixir")
    if not executable:
        raise ImportError("未找到 Elixir；函数切块需要 Elixir 解析器")
    command = [executable, str(BASE_DIR / "elixir_function_spans.exs"), str(source)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    except FileNotFoundError as exc:
        raise ImportError("未找到 Elixir；函数切块需要 Elixir 解析器") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "未知解析错误"
        raise ImportError(f"Elixir 语法解析失败：{detail}")
    spans: list[tuple[int, int]] = []
    for row in result.stdout.splitlines():
        if row.strip():
            start, end = row.split("\t", 1)
            spans.append((int(start), int(end)))
    return spans


def make_chunks(text: str, source: Path, language: str) -> list[Chunk]:
    if language != "elixir":
        raise ImportError("当前设计只允许导入 Elixir 源码")
    lines = text.splitlines(keepends=True)
    chunks = []
    for index, (start, end) in enumerate(parse_elixir_spans(source), 1):
        chunk_text = "".join(lines[start - 1:end])
        chunks.append(Chunk(index, start, end, chunk_text, sha256_text(chunk_text)))
    return chunks


def _create_chunks_table(conn: sqlite3.Connection, table: str = "chunks") -> None:
    conn.execute(f"""
        CREATE TABLE {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            start_line INTEGER NOT NULL CHECK (start_line > 0),
            end_line INTEGER NOT NULL CHECK (end_line >= start_line),
            text TEXT NOT NULL,
            chunk_sha256 TEXT NOT NULL,
            access_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
        )
    """)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, name TEXT NOT NULL,
            language TEXT NOT NULL, source_sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL,
            line_count INTEGER NOT NULL, created_at TEXT NOT NULL
        )
    """)
    existing = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'").fetchone()
    if not existing:
        _create_chunks_table(conn)
    else:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        wanted = {"id", "source_id", "chunk_index", "start_line", "end_line", "text", "chunk_sha256", "access_key", "created_at"}
        if columns != wanted:
            _create_chunks_table(conn, "chunks_new")
            rows = conn.execute("SELECT id, source_id, chunk_index, start_line, end_line, text, chunk_sha256, created_at FROM chunks").fetchall()
            for row in rows:
                conn.execute("INSERT INTO chunks_new VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*row[:7], _new_unique_key(conn, "chunks_new"), row[7]))
            conn.execute("DROP TABLE chunks")
            conn.execute("ALTER TABLE chunks_new RENAME TO chunks")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source_id ON chunks(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source_lines ON chunks(source_id, start_line, end_line)")


def _new_unique_key(conn: sqlite3.Connection, table: str = "chunks") -> str:
    while True:
        key = secrets.token_hex(8)
        if conn.execute(f"SELECT 1 FROM {table} WHERE access_key = ?", (key,)).fetchone() is None:
            return key


def store_source(conn: sqlite3.Connection, source: Path, language: str, text: str) -> int:
    cursor = conn.execute("""
        INSERT INTO sources (path, name, language, source_sha256, byte_length, line_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (str(source), source.name, language, sha256_text(text), len(text.encode("utf-8")), len(text.splitlines()), utc_now()))
    return int(cursor.lastrowid)


def store_chunks(conn: sqlite3.Connection, source_id: int, chunks: Sequence[Chunk]) -> list[tuple[int, str]]:
    created = []
    for chunk in chunks:
        key = _new_unique_key(conn)
        cursor = conn.execute("""
            INSERT INTO chunks (source_id, chunk_index, start_line, end_line, text, chunk_sha256, access_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (source_id, chunk.index, chunk.start_line, chunk.end_line, chunk.text, chunk.sha256, key, utc_now()))
        created.append((int(cursor.lastrowid), key))
    return created


def ingest(source: Path, db_path: Path, language: str | None = None,
           graph_node_id: str | None = None, permissions_db: Path | None = None) -> tuple[int, list[tuple[int, str]]]:
    text = source.read_text(encoding="utf-8-sig")
    actual_language = language or detect_language(source)
    chunks = make_chunks(text, source, actual_language)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        ensure_schema(conn)
        source_id = store_source(conn, source, actual_language, text)
        created = store_chunks(conn, source_id, chunks)
    if graph_node_id is not None:
        from block_core import OwnershipStore
        ownership = OwnershipStore(permissions_db or Path("block_permissions.sqlite"))
        try:
            for block_id, key in created:
                ownership.assign(graph_node_id, block_id, key)
        except Exception:
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            raise
    return source_id, created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只提取 Elixir 函数并保存到 SQLite。")
    parser.add_argument("source", type=Path)
    parser.add_argument("--db", type=Path, default=Path("chunks.sqlite"))
    parser.add_argument("--language", default=None)
    parser.add_argument("--graph-node-id", required=True, help="接收新块所有权的图谱节点")
    parser.add_argument("--permissions-db", type=Path, default=Path("block_permissions.sqlite"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source, db_path = args.source.expanduser().resolve(), args.db.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"未找到源文件：{source}")
    try:
        source_id, created = ingest(source, db_path, args.language, args.graph_node_id,
                                    args.permissions_db.expanduser().resolve())
    except ImportError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"已将 {source.name} 的 {len(created)} 个函数块写入 {db_path}")
    print(f"source_id={source_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
