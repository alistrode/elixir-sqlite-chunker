import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from graph_server import GraphError, GraphService, Handler, block_client


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'graph.json'
        self.original = {'nodes': [{'id': 'function:demo.ex:run/0', 'summary': 'keep', 'blockRefs': [{'id': 7}]}],
                         'edges': [{'source': 'a', 'target': 'b'}]}
        self.path.write_text(json.dumps(self.original), encoding='utf-8')
        self.calls = []
        self.results = [{'success': True, 'block': {'id': 7, 'start_line': 3, 'end_line': 8, 'text': 'private body'}}]
        def call(req):
            self.calls.append(req)
            return {'success': all(r['success'] for r in self.results), 'data': {'results': self.results}}
        self.service = GraphService(self.path, call)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, operation='read', blocks=None, node='function:demo.ex:run/0'):
        return {'operation': operation, 'graph_node_id': node, 'blocks': blocks or [{'id': 7}]}

    def test_node_result_persisted_without_body_and_returned_to_entry(self):
        result = self.service.handle(self.request())
        self.assertEqual(self.calls, [self.request()])
        self.assertTrue(result['data']['graph_synced'])
        self.assertEqual(result['data']['results'][0]['block']['text'], 'private body')
        graph = json.loads(self.path.read_text())
        self.assertNotIn('private body', self.path.read_text())
        self.assertEqual(graph['nodes'][0]['blockRefs'], [{'id': 7, 'start_line': 3, 'end_line': 8}])
        self.assertEqual(graph['edges'], self.original['edges'])
        self.assertEqual(graph['nodes'][0]['summary'], 'keep')

    def test_unknown_node_rejected_before_dispatch(self):
        with self.assertRaises(GraphError) as e:
            self.service.handle(self.request(node='absent'))
        self.assertEqual(e.exception.code, 'GRAPH_NODE_NOT_FOUND')
        self.assertEqual(self.calls, [])

    def test_partial_create_preserves_success_and_failure(self):
        self.results = [{'success': True, 'block': {'id': 8}},
                        {'success': False, 'id': 9, 'error_code': 'NOT_OWNER'}]
        result = self.service.handle(self.request('create', [{'id': 8}, {'id': 9}]))
        self.assertEqual(result['error_code'], 'PARTIAL_FAILURE')
        self.assertEqual(result['data']['results'], self.results)
        self.assertEqual([b['id'] for b in result['data']['graph_node']['blockRefs']], [7, 8])

    def test_successful_delete_removes_only_requested_reference(self):
        self.results = [{'success': True, 'id': 7}]
        result = self.service.handle(self.request('delete'))
        self.assertEqual(result['data']['graph_node']['blockRefs'], [])

    def test_failed_delete_keeps_reference(self):
        self.results = [{'success': False, 'id': 7, 'error_code': 'NOT_OWNER'}]
        result = self.service.handle(self.request('delete'))
        self.assertEqual(result['data']['graph_node']['blockRefs'], [{'id': 7}])

    def test_graph_write_failure_exposes_results_without_retry(self):
        with patch('graph_server.os.replace', side_effect=OSError('locked')):
            result = self.service.handle(self.request('rewrite'))
        self.assertEqual(result['error_code'], 'GRAPH_SYNC_FAILED')
        self.assertFalse(result['data']['graph_synced'])
        self.assertEqual(result['data']['results'], self.results)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_unwritable_graph_directory_does_not_dispatch(self):
        with patch('graph_server.tempfile.mkstemp', side_effect=OSError('denied')):
            with self.assertRaises(GraphError):
                self.service.handle(self.request('create'))
        self.assertFalse(self.calls)

    def test_incomplete_upstream_response_rejected(self):
        self.results = []
        with self.assertRaises(GraphError) as e:
            self.service.handle(self.request('rewrite'))
        self.assertEqual(e.exception.code, 'BLOCK_RESULT_UNKNOWN')
        self.assertEqual(json.loads(self.path.read_text()), self.original)

    def test_external_graph_change_is_not_overwritten(self):
        def call(req):
            self.path.write_text(json.dumps({'nodes': [{'id': 'new'}]}))
            return {'data': {'results': self.results}}
        service = GraphService(self.path, call)
        result = service.handle(self.request('rewrite'))
        self.assertEqual(result['error_code'], 'GRAPH_SYNC_FAILED')
        self.assertEqual(json.loads(self.path.read_text())['nodes'][0]['id'], 'new')

    def test_real_permission_service_read_and_delete_roundtrip(self):
        import sqlite3
        from block_core import BlockStore, OwnershipStore
        from permission_server import BlockService, Handler as BlockHandler
        db = Path(self.temp.name) / 'blocks.sqlite'
        owners = OwnershipStore(Path(self.temp.name) / 'owners.sqlite')
        store = BlockStore(db)
        # Seed an existing block without invoking the Elixir importer.
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO sources VALUES (1, 'demo.ex', 'demo.ex', 'elixir', 'fixture', 1, 1, 'now')")
            conn.execute("INSERT INTO chunks VALUES (7, 1, 0, 3, 8, 'body', 'fixture', '0011223344556677', 'now')")
        owners.assign('function:demo.ex:run/0', 7, '0011223344556677')
        handler = type('RealBlocks', (BlockHandler,), {'service': BlockService(store, owners)})
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=upstream.serve_forever)
        thread.start()
        service = GraphService(self.path, block_client(f'http://127.0.0.1:{upstream.server_port}/api/blocks'))
        try:
            result = service.handle(self.request())
            self.assertEqual(result['data']['results'][0]['block']['text'], 'body')
            self.assertTrue(result['data']['graph_synced'])
            denied_graph = json.loads(self.path.read_text())
            denied_graph['nodes'].append({'id': 'other'})
            self.path.write_text(json.dumps(denied_graph))
            denied = service.handle(self.request(node='other'))
            self.assertFalse(denied['success'])
            self.assertEqual(denied['data']['graph_node']['blockRefs'], [])
            deleted = service.handle(self.request('delete'))
            self.assertTrue(deleted['success'])
            self.assertEqual(deleted['data']['graph_node']['blockRefs'], [])
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)
        finally:
            upstream.shutdown(); upstream.server_close(); thread.join()

    def test_real_http_entry_to_block_http_and_graph_feedback(self):
        received = []
        class Blocks(BaseHTTPRequestHandler):
            def do_POST(handler):
                received.append(json.loads(handler.rfile.read(int(handler.headers['Content-Length']))))
                raw = json.dumps({'success': True, 'data': {'results': self.results}}).encode()
                handler.send_response(200)
                handler.send_header('Content-Length', str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw)
            def log_message(self, *_): pass
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Blocks)
        service = GraphService(self.path, block_client(f'http://127.0.0.1:{upstream.server_port}/api/blocks'))
        entry_handler = type('Entry', (Handler,), {'service': service})
        entry = ThreadingHTTPServer(('127.0.0.1', 0), entry_handler)
        threads = [threading.Thread(target=s.serve_forever) for s in (upstream, entry)]
        for t in threads: t.start()
        try:
            url = f'http://127.0.0.1:{entry.server_port}/api/graph/blocks'
            req = urllib.request.Request(url, data=json.dumps(self.request()).encode(), method='POST')
            with urllib.request.urlopen(req) as response:
                result = json.load(response)
            self.assertEqual(received, [self.request()])
            self.assertTrue(result['data']['graph_synced'])
            self.assertEqual(json.loads(self.path.read_text())['nodes'][0]['blockRefs'][0]['id'], 7)
            req = urllib.request.Request(url, data=b'{', method='POST')
            with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 400)
        finally:
            for s in (entry, upstream): s.shutdown(); s.server_close()
            for t in threads: t.join()


if __name__ == '__main__': unittest.main()
