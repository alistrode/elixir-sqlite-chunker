import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from block_core import BlockError, BlockStore, OwnershipStore
from chunk_to_sqlite import ingest
from permission_server import BlockService


SOURCE = '''defmodule Demo do
  @doc "documented"
  @spec first(boolean()) :: integer()
  def first(value) do
    if value do
      1
    else
      0
    end
  end

  defp second(value), do: value
end
'''


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "demo.ex"
        self.source.write_text(SOURCE, encoding="utf-8")
        self.db = root / "blocks.sqlite"
        self.permissions = root / "permissions.sqlite"
        self.source_id, self.created = ingest(self.source, self.db, graph_node_id="node-a", permissions_db=self.permissions)
        self.blocks = BlockStore(self.db)
        self.owners = OwnershipStore(self.permissions)
        self.service = BlockService(self.blocks, self.owners)

    def tearDown(self):
        self.temp.cleanup()

    def test_function_only_parser_and_schema(self):
        with closing(sqlite3.connect(self.db)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
            rows = conn.execute("SELECT start_line, end_line, length(access_key) FROM chunks ORDER BY id").fetchall()
        self.assertEqual(columns, {"id", "source_id", "chunk_index", "start_line", "end_line", "text", "chunk_sha256", "access_key", "created_at"})
        self.assertEqual(rows, [(2, 10, 16), (12, 12, 16)])

    def test_wrong_key_rejected_and_rewrite_keeps_key(self):
        block_id, key = self.created[0]
        with self.assertRaises(BlockError) as error:
            self.blocks.read(block_id, "0" * 16)
        self.assertEqual(error.exception.code, "INVALID_KEY")
        rewritten = self.blocks.rewrite({"id": block_id, "start_line": 2, "end_line": 9, "text": "replacement"}, key)
        self.assertEqual(rewritten["text"], "replacement")
        with closing(sqlite3.connect(self.db)) as conn:
            saved_key, digest = conn.execute("SELECT access_key, chunk_sha256 FROM chunks WHERE id=?", (block_id,)).fetchone()
        self.assertEqual(saved_key, key)
        self.assertNotEqual(digest, "")

    def test_node_cannot_read_another_nodes_block_and_other_items_continue(self):
        first, second = self.created[0][0], self.created[1][0]
        response = self.service.handle({"operation": "read", "graph_node_id": "node-a", "blocks": [{"id": first}, {"id": 999}]})
        self.assertTrue(response["results"][0]["success"])
        self.assertFalse(response["results"][1]["success"])
        self.assertEqual(response["results"][1]["error_code"], "NOT_OWNER")

    def test_delete_is_explicit(self):
        first, second = self.created[0][0], self.created[1][0]
        key = self.owners.key_for("node-a", first)
        self.blocks.rewrite({"id": first, "start_line": 2, "end_line": 10, "text": "changed"}, key)
        self.assertIsNotNone(self.blocks.read(second, self.owners.key_for("node-a", second)))
        result = self.service.handle({"operation": "delete", "graph_node_id": "node-a", "blocks": [{"id": first}]})
        self.assertTrue(result["results"][0]["success"])
        with self.assertRaises(BlockError):
            self.blocks.read(first, key)


if __name__ == "__main__":
    unittest.main()
