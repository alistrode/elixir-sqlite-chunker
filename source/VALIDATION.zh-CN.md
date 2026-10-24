# 验证记录

2026-10-24，Linux 云环境，Python 3.12。

- `python -m unittest discover -s source -p test_graph_server.py -v`：11 项通过。
- 含实际 HTTP 图谱入口到 HTTP 块服务的往返测试，以及真实权限服务和 SQLite 的读取、越权拒绝、删除、图谱反馈测试。
- 覆盖部分失败、不存在节点、不可写目录、异常响应、外部图谱变更、图谱写回失败后保留块结果且不重试。
- 原有 Elixir AST 导入测试未在本云环境执行：未安装 Elixir；原有解析器、块核心和权限服务源码未改动。
- SSH Relay 仓库根目录要求的 `npm run build`、`npm run test:pro`、`npm run test:relay` 全部通过；它们验证 SSH Relay，不代替图谱接口测试。
- 未创建运行包，未部署常驻服务，未覆盖远端已有数据库或项目源码。

本分支包含目标项目全部源码、原有测试、样例和设计文档。
不包含 SQLite 业务数据、数据库日志、缓存、截图和已安装依赖。
可选截图脚本依赖 Pillow；桌面浏览器依赖系统 Tkinter；核心服务只用 Python 标准库，导入另需 Elixir。
现有 Understand Anything 适配器在其原项目中，本接口独立运行，无需复制其第三方工程。
