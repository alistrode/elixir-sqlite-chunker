# 图谱接口

调用链：入口 → 图谱接口 → 本地块权限服务 → 图谱写回和响应 → 入口。
图谱界面不参与块操作。此接口为独立 Python 服务，不依赖 Understand Anything 的 TypeScript 适配器。

## 启动

Python 3.10+，全部使用标准库。导入 Elixir 文件另需安装 Elixir，并确保 `elixir` 在 PATH。
在本目录分别运行：

```powershell
python permission_server.py --db cohesive_ledger.sqlite --permissions-db block_permissions.sqlite
python graph_server.py --graph path/to/knowledge-graph.json
```

默认块服务监听 `127.0.0.1:8765`，图谱接口监听 `127.0.0.1:8766`。
可用 `--port` 和 `--blocks-port` 修改端口。图谱 JSON 须有 `nodes` 数组，每个节点的 `id` 唯一。
现有 demo 的节点编号只存在于示例，实际请求应使用实际图谱节点 ID。

## 请求

```http
POST http://127.0.0.1:8766/api/graph/blocks
Content-Type: application/json

{"operation":"read","graph_node_id":"function:demo.ex:enabled?/1","blocks":[{"id":1}]}
```

`operation` 支持 `create`、`read`、`rewrite`、`delete`，块参数与原 `POST /api/blocks` 一致。
先确认节点存在，才调用块服务；块所有权仍由权限数据库决定。节点存在不代表拥有某块。
禁止直接将任意字符串当作已验证图谱节点。

## 结果如何反馈

- `data.results` 原样返回逐块操作结果，包括失败项和成功项。
- `data.graph_node` 返回目标图谱节点操作后的状态。
- 成功的创建、读取、覆写更新节点的 `blockRefs`，仅记录块 ID、来源、顺序和行范围。
- 成功删除只移除相应引用；失败操作不添加或删除引用。
- `lastBlockOperation` 保存操作类型、成功数、失败数和时间。
- `data.graph_synced=true` 才表示图谱文件写入完成。
- 图谱不保存块正文或密钥；原有节点字段、边和其他节点保留。

## 失败和并发

部分块失败返回 `PARTIAL_FAILURE`，成功项仍写回图谱，调用方必须检查逐块结果。
网络中断或异常响应返回 `BLOCK_RESULT_UNKNOWN`；禁止直接重放写请求，应先核对实际块状态。
块操作与图谱文件不是同一个事务。若块操作返回后图谱保存失败，返回 `GRAPH_SYNC_FAILED`，
同时保留 `data.results`，`graph_synced=false`。不要因图谱失败重复创建、覆写或删除块。

同一服务实例内串行处理请求。文件以临时文件替换；写回前检查外部改动，发现变更则拒绝覆盖。
同一图谱只启动一个写服务；分析器重新生成图谱时先停此服务。此接口不负责重建函数调用关系、
修改原始 Elixir 文件或重新解析 AST，也不会把块正文同步到源码文件。

接口只供本机受信进程使用，不提供跨用户身份认证；不要将端口公开到网络。
默认 HTTP 客户端等待最多 30 秒，服务不会自动重试业务写入。

## 测试

```powershell
python -m unittest -v test_graph_server
python -m unittest -v test_block_ownership
```

第一组测试含真实本机 HTTP 入口与模拟块服务往返，不需要 Elixir。
原有 `test_block_ownership` 需要 Elixir；它验证原始切块和权限实现。
