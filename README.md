# Elixir SQLite Chunker：图谱接口版

完整项目在 `source/`。原项目来自用户授权读取的
`H:\github\xxm\XXM\elixir-sqlite-chunker`，保留既有 Python / Elixir 源码与文档。
本次新增图谱接口与测试，不将原项目冒称本次从零实现。

- [项目说明](source/README.md)
- [图谱接口及启动](source/GRAPH_API.zh-CN.md)
- [验证范围](source/VALIDATION.zh-CN.md)

新增 `POST http://127.0.0.1:8766/api/graph/blocks`：
入口传图谱节点 ID → 图谱校验 → 块权限服务 → 逐块结果和图谱状态返回入口。
成功项的块引用写回图谱；块正文和密钥不进入图谱。

无需安装 Python 第三方包即可运行两项本地服务。Elixir 导入器需另装 Elixir。
本分支交付源码，不含业务数据库、常驻服务和运行包。
