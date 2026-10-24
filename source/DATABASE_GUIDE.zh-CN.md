# 数据库说明

实现以 `BLOCK_OWNERSHIP_DESIGN.zh-CN.md` 为准。

- `sources` 保留来源元数据。
- `chunks` 仅保存 `id`、`source_id`、`chunk_index`、`start_line`、`end_line`、`text`、`chunk_sha256`、`access_key`、`created_at`。
- `access_key` 是鉴权的最终依据；`chunk_sha256` 只用于内容存档。
- `block_permissions.sqlite` 独立保存 `graph_nodes` 和 `block_ownership`，一个块最多归属一个图谱节点。
- 业务调用只能使用本机 `POST /api/blocks` 接口，不应直接查询块正文。

具体启动、导入和请求示例见 `README.md`。
