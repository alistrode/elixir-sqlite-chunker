#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
import json
import urllib.error
import urllib.request
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


@dataclass(frozen=True)
class SourceRow:
    id: int
    name: str
    path: str
    language: str
    line_count: int


@dataclass(frozen=True)
class ChunkRow:
    id: int
    source_id: int
    chunk_index: int
    start_line: int
    end_line: int
    text: str


def database_exists(path: Path) -> bool:
    return path.exists() and path.is_file()


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_sources(conn: sqlite3.Connection) -> list[SourceRow]:
    rows = conn.execute(
        """
        SELECT id, name, path, language, line_count
        FROM sources
        ORDER BY id DESC
        """
    ).fetchall()
    return [
        SourceRow(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            language=row["language"],
            line_count=row["line_count"],
        )
        for row in rows
    ]


def fetch_chunks(conn: sqlite3.Connection, source_id: int, api_url: str, graph_node_id: str) -> list[ChunkRow]:
    # 数据库查询只发现编号和显示顺序；受保护的正文必须由权限接口返回。
    rows = conn.execute("SELECT id, chunk_index FROM chunks WHERE source_id=? ORDER BY chunk_index", (source_id,)).fetchall()
    if not rows:
        return []
    payload = json.dumps({"operation": "read", "graph_node_id": graph_node_id,
                          "blocks": [{"id": row["id"]} for row in rows]}).encode("utf-8")
    request = urllib.request.Request(api_url, payload, {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.load(response)
    by_id = {row["id"]: row["chunk_index"] for row in rows}
    chunks = []
    for result in body.get("data", {}).get("results", []):
        if result.get("success"):
            block = result["block"]
            chunks.append(ChunkRow(block["id"], block["source_id"], by_id[block["id"]],
                                   block["start_line"], block["end_line"], block["text"]))
    return chunks


class ChunkBrowser(tk.Tk):
    def __init__(self, db_path: Path, api_url: str, graph_node_id: str):
        super().__init__()
        self.title("Elixir 块浏览器")
        self.geometry("1280x860")
        self.minsize(1000, 700)

        self.db_path = db_path
        self.api_url = api_url
        self.graph_node_id = graph_node_id
        self.conn: sqlite3.Connection | None = None
        self.sources: list[SourceRow] = []
        self.chunks: list[ChunkRow] = []
        self.source_map: dict[str, SourceRow] = {}

        self._build_ui()
        self._bind_events()
        self.load_database(db_path)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self, padding=8)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="数据库").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.db_var = tk.StringVar(value=str(self.db_path))
        self.db_entry = ttk.Entry(top, textvariable=self.db_var)
        self.db_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(top, text="打开", command=self.choose_db).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(top, text="重载", command=self.reload_database).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(top, text="刷新", command=self.refresh_chunks).grid(row=0, column=4, padx=(8, 0))

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        left = ttk.Frame(body, padding=8)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        body.add(left, weight=1)

        ttk.Label(left, text="源文件").grid(row=0, column=0, sticky="w")
        self.source_list = tk.Listbox(left, activestyle="dotbox", exportselection=False, height=18)
        self.source_list.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        source_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.source_list.yview)
        self.source_list.configure(yscrollcommand=source_scroll.set)
        source_scroll.grid(row=1, column=1, sticky="ns", pady=(6, 0))

        source_meta = ttk.LabelFrame(left, text="已选源文件", padding=8)
        source_meta.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        source_meta.columnconfigure(1, weight=1)

        self.source_name_var = tk.StringVar(value="-")
        self.source_path_var = tk.StringVar(value="-")
        self.source_info_var = tk.StringVar(value="-")

        ttk.Label(source_meta, text="名称").grid(row=0, column=0, sticky="w")
        ttk.Label(source_meta, textvariable=self.source_name_var).grid(row=0, column=1, sticky="w")
        ttk.Label(source_meta, text="路径").grid(row=1, column=0, sticky="w")
        ttk.Label(source_meta, textvariable=self.source_path_var).grid(row=1, column=1, sticky="w")
        ttk.Label(source_meta, text="信息").grid(row=2, column=0, sticky="w")
        ttk.Label(source_meta, textvariable=self.source_info_var).grid(row=2, column=1, sticky="w")

        right = ttk.Frame(body, padding=8)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        body.add(right, weight=4)

        ttk.Label(right, text="块列表").grid(row=0, column=0, sticky="w")
        columns = ("id", "index", "lines")
        self.tree = ttk.Treeview(right, columns=columns, show="headings", selectmode="browse")
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        for col, title, width in [
            ("id", "块编号", 100),
            ("index", "顺序", 100),
            ("lines", "首末行", 180),
        ]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="w")

        tree_scroll = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.grid(row=1, column=1, sticky="ns", pady=(6, 0))

        preview = ttk.LabelFrame(self, text="块内容", padding=8)
        preview.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)

        self.text = tk.Text(preview, wrap="none", height=12)
        self.text.grid(row=0, column=0, sticky="nsew")
        text_scroll_y = ttk.Scrollbar(preview, orient=tk.VERTICAL, command=self.text.yview)
        text_scroll_x = ttk.Scrollbar(preview, orient=tk.HORIZONTAL, command=self.text.xview)
        self.text.configure(yscrollcommand=text_scroll_y.set, xscrollcommand=text_scroll_x.set)
        text_scroll_y.grid(row=0, column=1, sticky="ns")
        text_scroll_x.grid(row=1, column=0, sticky="ew")

        status = ttk.Frame(self, padding=(8, 0, 8, 8))
        status.grid(row=3, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

    def _bind_events(self) -> None:
        self.source_list.bind("<<ListboxSelect>>", self.on_source_selected)
        self.tree.bind("<<TreeviewSelect>>", self.on_chunk_selected)

    def choose_db(self) -> None:
        selected = filedialog.askopenfilename(
            title="打开数据库",
            filetypes=[("SQLite database", "*.sqlite *.db *.sqlite3"), ("All files", "*.*")],
            initialdir=str(self.db_path.parent),
        )
        if selected:
            self.load_database(Path(selected))

    def load_database(self, path: Path) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

        if not database_exists(path):
            messagebox.showerror("数据库未找到", f"无法打开：\n{path}")
            self.status_var.set("数据库未找到")
            return

        try:
            self.conn = connect_db(path)
            self.db_path = path
            self.db_var.set(str(path))
            self.refresh_sources()
        except sqlite3.Error as exc:
            messagebox.showerror("数据库错误", str(exc))
            self.status_var.set("数据库错误")

    def reload_database(self) -> None:
        self.load_database(Path(self.db_var.get()).expanduser())

    def refresh_sources(self) -> None:
        if self.conn is None:
            return
        try:
            self.sources = fetch_sources(self.conn)
            self.source_map = {}
            self.source_list.delete(0, tk.END)
            for src in self.sources:
                display = f"#{src.id}  {src.name}"
                self.source_map[display] = src
                self.source_list.insert(tk.END, display)
            if self.sources:
                self.source_list.selection_set(0)
                # 初始化发生在 Tk 主事件循环之前；直接调用可避免虚拟事件尚未分派，
                # 导致首个源文件已高亮但块列表仍为空。
                self.on_source_selected()
            else:
                self.clear_chunks("数据库中没有源文件")
            self.status_var.set(f"已加载 {len(self.sources)} 个源文件")
        except sqlite3.Error as exc:
            messagebox.showerror("查询错误", str(exc))

    def refresh_chunks(self) -> None:
        selection = self.source_list.curselection()
        if selection:
            self.on_source_selected()

    def on_source_selected(self, event: tk.Event | None = None) -> None:
        if self.conn is None:
            return
        selection = self.source_list.curselection()
        if not selection:
            return
        display = self.source_list.get(selection[0])
        src = self.source_map.get(display)
        if src is None:
            return

        self.source_name_var.set(src.name)
        self.source_path_var.set(src.path)
        self.source_info_var.set(f"id={src.id}  language={src.language}  lines={src.line_count}")

        try:
            self.chunks = fetch_chunks(self.conn, src.id, self.api_url, self.graph_node_id)
            self.populate_tree()
            self.status_var.set(f"{src.name} 中共有 {len(self.chunks)} 个块")
        except (sqlite3.Error, urllib.error.URLError, ValueError) as exc:
            messagebox.showerror("查询错误", str(exc))

    def clear_chunks(self, message: str) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", message)

    def populate_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.text.delete("1.0", tk.END)
        for chunk in self.chunks:
            lines = f"{chunk.start_line}-{chunk.end_line}"
            self.tree.insert(
                "",
                tk.END,
                iid=str(chunk.id),
                values=(chunk.id, chunk.chunk_index, lines),
            )
        if self.chunks:
            self.tree.selection_set(str(self.chunks[0].id))
            self.tree.focus(str(self.chunks[0].id))
            self.show_chunk(self.chunks[0].id)

    def on_chunk_selected(self, event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.show_chunk(int(selection[0]))

    def show_chunk(self, chunk_id: int) -> None:
        chunk = next((row for row in self.chunks if row.id == chunk_id), None)
        if chunk is None:
            return
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", chunk.text)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在桌面窗口中浏览数据库块数据。")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("cohesive_ledger.sqlite"),
        help="要打开的数据库",
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8765/api/blocks")
    parser.add_argument("--graph-node-id", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    app = ChunkBrowser(args.db.expanduser().resolve(), args.api_url, args.graph_node_id)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
