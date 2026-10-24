#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from block_core import BlockError, BlockStore, OwnershipStore


class BlockService:
    def __init__(self, blocks: BlockStore, ownership: OwnershipStore):
        self.blocks, self.ownership = blocks, ownership

    def handle(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or request.get("operation") not in {"create", "read", "rewrite", "delete"}:
            raise BlockError("INVALID_ARGUMENT", "operation 无效")
        node, items = request.get("graph_node_id"), request.get("blocks")
        if not isinstance(node, str) or not node.strip() or not isinstance(items, list) or not items:
            raise BlockError("INVALID_ARGUMENT", "graph_node_id 和非空 blocks 数组为必填项")
        results = []
        for item in items:
            try:
                if not isinstance(item, dict):
                    raise BlockError("INVALID_ARGUMENT", "块必须是对象")
                operation = request["operation"]
                if operation == "create":
                    block, key = self.blocks.create(item)
                    try:
                        self.ownership.assign(node, block["id"], key)
                    except Exception:
                        self.blocks.delete(block["id"], key)
                        raise
                    results.append({"success": True, "block": block})
                else:
                    block_id = item.get("id")
                    if not isinstance(block_id, int):
                        raise BlockError("INVALID_ARGUMENT", "块 id 必须是整数")
                    key = self.ownership.key_for(node, block_id)
                    if operation == "read":
                        results.append({"success": True, "block": self.blocks.read(block_id, key)})
                    elif operation == "rewrite":
                        results.append({"success": True, "block": self.blocks.rewrite(item, key)})
                    else:
                        self.blocks.delete(block_id, key)
                        self.ownership.unassign(node, block_id)
                        results.append({"success": True, "id": block_id})
            except BlockError as exc:
                results.append({"success": False, "error_code": exc.code, "message": str(exc), "id": item.get("id") if isinstance(item, dict) else None})
        return {"results": results}


class Handler(BaseHTTPRequestHandler):
    service: BlockService

    def do_POST(self) -> None:
        if self.path != "/api/blocks":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            data = self.service.handle(request)
            success = all(x["success"] for x in data["results"])
            response = {"success": success, "error_code": None if success else "PARTIAL_FAILURE",
                        "message": "ok" if success else "部分块操作失败", "data": data}
            self._json(200, response)
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"success": False, "error_code": "INVALID_ARGUMENT", "message": "请求不是有效 JSON", "data": {}})
        except BlockError as exc:
            self._json(400, {"success": False, "error_code": exc.code, "message": str(exc), "data": {}})
        except Exception:
            self._json(500, {"success": False, "error_code": "INTERNAL_ERROR", "message": "内部错误", "data": {}})

    def _json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="块所有权本地接口")
    parser.add_argument("--db", type=Path, default=Path("cohesive_ledger.sqlite"))
    parser.add_argument("--permissions-db", type=Path, default=Path("block_permissions.sqlite"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    Handler.service = BlockService(BlockStore(args.db.resolve()), OwnershipStore(args.permissions_db.resolve()))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"本地接口：http://127.0.0.1:{args.port}/api/blocks")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
