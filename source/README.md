# elixir-sqlite-chunker

按 Elixir AST 提取函数块，并通过独立所有权数据库和单一本地接口保护块的读取、覆写和删除。

## 导入

需要本机安装 Elixir。导入只接受合法的 `.ex`/`.exs` 源码；解析失败时整次导入回滚。

```powershell
python chunk_to_sqlite.py demo.ex `
  --db cohesive_ledger.sqlite `
  --permissions-db block_permissions.sqlite `
  --graph-node-id graph-001
```

每个 `def`、`defp`、`defmacro`、`defmacrop`、`defguard`、`defguardp` 分别成为一块，紧邻的注释、`@doc`、`@spec`、`@impl` 会纳入块中。模块外壳和其他模块级代码不会入库。

## 本地接口

```powershell
python permission_server.py --db cohesive_ledger.sqlite --permissions-db block_permissions.sqlite
```

唯一业务入口是 `POST http://127.0.0.1:8765/api/blocks`，`operation` 支持 `create`、`read`、`rewrite`、`delete`。服务只监听 `127.0.0.1`，响应和日志不包含密钥或正文之外的内部异常。

创建示例：

```json
{
  "operation": "create",
  "graph_node_id": "graph-001",
  "blocks": [{
    "source_id": 1,
    "chunk_index": 3,
    "start_line": 30,
    "end_line": 35,
    "text": "def example, do: :ok\n"
  }]
}
```

读取只需传块编号；权限服务会从自己的数据库查找该节点持有的密钥：

```json
{"operation":"read","graph_node_id":"graph-001","blocks":[{"id":1}]}
```

## 浏览器

先启动权限服务，然后指定要使用的图谱节点：

```powershell
python chunk_browser.py --db cohesive_ledger.sqlite --graph-node-id graph-001
```

浏览器只从块库发现编号和顺序，正文通过权限接口读取；界面只显示块编号、顺序、首末行和正文。

## 数据库迁移

导入器或权限服务首次打开旧数据库时，会事务性重建 `chunks` 表、保留原字段中的有效数据、删除五个废弃字段，并给旧块生成随机密钥。旧块此前没有图谱所有权关系，因此不会被擅自分配给任意节点；需要由管理方按实际归属重新导入或登记。

运行测试：`python -m unittest -v`。

## 图谱调用入口

新增独立入口 `POST http://127.0.0.1:8766/api/graph/blocks`。
通过图谱节点调用块服务，逐块结果返回入口，并将成功项的块引用写回图谱。
启动、协议、部分失败和同步边界见 [图谱接口说明](GRAPH_API.zh-CN.md)。
