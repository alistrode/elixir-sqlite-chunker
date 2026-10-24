#!/usr/bin/env python3
"""Local graph entrypoint. Block authorization remains in permission_server."""
from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

MAX_BODY = 2 * 1024 * 1024


class GraphError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def block_client(endpoint: str) -> Callable[[dict], dict]:
    def call(request: dict) -> dict:
        req = urllib.request.Request(endpoint, data=json.dumps(request).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            # A mutation is dispatched once; transport failures never trigger a retry.
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise GraphError("BLOCK_RESPONSE_INVALID", "块服务响应过大", 502)
            return json.loads(raw)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise GraphError("BLOCK_RESULT_UNKNOWN", "未取得块服务结果；请核对状态，不要重复提交写操作", 502) from exc
    return call


class GraphService:
    def __init__(self, graph: Path, call_blocks: Callable[[dict], dict]):
        self.graph, self.call_blocks = graph, call_blocks
        self.lock = threading.Lock()

    def handle(self, request: Any) -> dict:
        if not isinstance(request, dict) or request.get("operation") not in {"create", "read", "rewrite", "delete"}:
            raise GraphError("INVALID_ARGUMENT", "operation 无效")
        node_id, blocks = request.get("graph_node_id"), request.get("blocks")
        if not isinstance(node_id, str) or not node_id.strip() or not isinstance(blocks, list) or not blocks:
            raise GraphError("INVALID_ARGUMENT", "graph_node_id 和非空 blocks 数组为必填项")
        if any(not isinstance(item, dict) for item in blocks):
            raise GraphError("INVALID_ARGUMENT", "每个块必须是对象")
        with self.lock:
            try:
                original_text = self.graph.read_text(encoding="utf-8-sig")
                graph = json.loads(original_text)
            except (OSError, ValueError) as exc:
                raise GraphError("GRAPH_UNAVAILABLE", "无法读取图谱", 503) from exc
            nodes = graph.get("nodes") if isinstance(graph, dict) else None
            if not isinstance(nodes, list):
                raise GraphError("GRAPH_INVALID", "图谱缺少 nodes 数组", 503)
            matches = [n for n in nodes if isinstance(n, dict) and n.get("id") == node_id]
            if len(matches) != 1:
                raise GraphError("GRAPH_NODE_NOT_FOUND", "图谱节点不存在或编号重复", 404)
            node = matches[0]
            existing = node.get("blockRefs", [])
            if not isinstance(existing, list) or any(not isinstance(r, dict) or type(r.get("id")) is not int for r in existing):
                raise GraphError("GRAPH_INVALID", "节点 blockRefs 无效", 503)
            # Allocate a sibling file before dispatch, so an unwritable directory cannot
            # cause an otherwise avoidable mutation without graph feedback.
            try:
                fd, tmp = tempfile.mkstemp(prefix=".graph-feedback-", suffix=".json", dir=self.graph.parent)
            except OSError as exc:
                raise GraphError("GRAPH_NOT_WRITABLE", "图谱目录不可写，未调用块服务", 503) from exc
            os.close(fd)
            try:
                upstream = self.call_blocks({"operation": request["operation"], "graph_node_id": node_id, "blocks": blocks})
                results = upstream.get("data", {}).get("results") if isinstance(upstream, dict) else None
                if (not isinstance(results, list) or len(results) != len(blocks)
                        or any(not isinstance(r, dict) or type(r.get("success")) is not bool for r in results)):
                    raise GraphError("BLOCK_RESULT_UNKNOWN", "块服务结果不完整；请核对状态，不要重复提交写操作", 502)
                refs = {r["id"]: copy.deepcopy(r) for r in existing}
                for result in results:
                    if not result["success"]:
                        continue
                    if request["operation"] == "delete":
                        block_id = result.get("id")
                        if type(block_id) is not int:
                            raise GraphError("BLOCK_RESULT_UNKNOWN", "删除结果缺少块编号", 502)
                        refs.pop(block_id, None)
                    else:
                        block = result.get("block")
                        if not isinstance(block, dict) or type(block.get("id")) is not int:
                            raise GraphError("BLOCK_RESULT_UNKNOWN", "块结果缺少编号", 502)
                        refs[block["id"]] = {k: block[k] for k in ("id", "source_id", "chunk_index", "start_line", "end_line") if k in block}
                node["blockRefs"] = list(refs.values())
                succeeded = sum(r["success"] for r in results)
                node["lastBlockOperation"] = {"operation": request["operation"], "succeeded": succeeded,
                    "failed": len(results) - succeeded, "at": datetime.now(timezone.utc).isoformat()}
                all_ok = succeeded == len(results)
                response = {"success": all_ok, "error_code": None if all_ok else "PARTIAL_FAILURE",
                    "message": "ok" if all_ok else "部分块操作失败", "data": {
                        "graph_node_id": node_id, "results": results, "graph_node": copy.deepcopy(node), "graph_synced": False}}
                try:
                    with open(tmp, "w", encoding="utf-8") as out:
                        json.dump(graph, out, ensure_ascii=False, indent=2)
                        out.write("\n")
                        out.flush()
                        os.fsync(out.fileno())
                    if self.graph.read_text(encoding="utf-8-sig") != original_text:
                        raise OSError("Graph changed during block operation")
                    os.replace(tmp, self.graph)
                except OSError:
                    response.update(success=False, error_code="GRAPH_SYNC_FAILED",
                        message="块结果已返回，但图谱写入失败；不要重复提交写操作")
                    return response
                response["data"]["graph_synced"] = True
                return response
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)


class Handler(BaseHTTPRequestHandler):
    service: GraphService

    def do_POST(self) -> None:
        if self.path != "/api/graph/blocks":
            self.respond(404, {"success": False, "error_code": "NOT_FOUND", "data": {}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise GraphError("INVALID_ARGUMENT", "请求长度无效", 413)
            payload = self.service.handle(json.loads(self.rfile.read(length)))
            self.respond(200, payload)
        except GraphError as exc:
            self.respond(exc.status, {"success": False, "error_code": exc.code, "message": str(exc), "data": {}})
        except (ValueError, UnicodeError):
            self.respond(400, {"success": False, "error_code": "INVALID_ARGUMENT", "data": {}})
        except Exception:
            self.respond(500, {"success": False, "error_code": "INTERNAL_ERROR", "data": {}})

    def respond(self, status: int, value: dict) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="图谱节点到块服务的本机入口")
    parser.add_argument("--graph", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--blocks-port", type=int, default=8765)
    args = parser.parse_args()
    Handler.service = GraphService(args.graph.resolve(), block_client(f"http://127.0.0.1:{args.blocks_port}/api/blocks"))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"图谱接口：http://127.0.0.1:{args.port}/api/graph/blocks", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
