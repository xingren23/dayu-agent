# dayu MCP 开发手册

`dayu/mcp` 通过 MCP 协议（Model Context Protocol）把 dayu 的财报读取工具暴露给外部 Agent 系统
（opencode、codex、claude-code 等），使外部 LLM 能直接查询已下载到本地 workspace 的财报文档。

## 1. 设计考虑

### 1.1 定位：为什么需要 MCP 暴露面

dayu 本身就是 LLM-in-the-loop 的 Agent 系统，内部已有一整套工具注册/执行/截断/错误处理机制。
MCP 暴露面在此基础上，将**财报读取**能力以独立进程方式提供给外部 Agent，使 opencode / codex 等
无需引入 dayu 的 Host/Agent 层即可直接查询财报数据。

MCP 模块不取代 dayu 内部工具路径，而是并行的另一条暴露路径：
```
外部 Agent (opencode / codex / claude-code)
  │  MCP 协议（stdio: stdin 读 JSON-RPC / stdout 写 JSON-RPC）
  ▼
dayu/mcp/server.py          ← MCP 入口（stdio 传输 + 工具注册）
  │
  ▼
dayu/mcp/fins_tools.py      ← 9 工具 schema 定义 + 调度转发
  │
  ▼
dayu.fins.service_runtime   ← DefaultFinsRuntime.create() 装配仓储/处理器
  │
  ▼
dayu.fins.tools.service     ← FinsToolService（财报读取 API）
```

### 1.2 不引入的层

MCP 模块是 dayu 的**最低依赖暴露面**，仅依赖 `dayu.fins.*` 和 `dayu.engine.processors.*`，
**不引入**以下层：

- `dayu.host.*` —— 会话/运行/并发/取消治理（MCP 无状态工具调用不需要）
- `dayu.services.*` —— 业务语义解释（MCP 客户端承担）
- `dayu.cli.*` / `dayu.web.*` / `dayu.wechat.*` —— UI 入口
- `dayu.engine.agent` —— LLM 编排（MCP 客户端承担）

### 1.3 MCP 暴露与 dayu 内部工具的差异

| 维度 | dayu 内部（Agent augmentation path） | MCP 暴露 |
|------|--------------------------------------|----------|
| 调用者 | dayu 自己的 LLM（经由 Host/Agent） | 外部 LLM（经由 MCP 客户端） |
| 工具注册 | `toolset_registrars.json` → `ToolRegistry` | MCP `Server.list_tools()` → `build_mcp_tools()` |
| 工具执行 | `ToolRegistry.execute()` → `@tool()` 装饰器包装 | `asyncio.to_thread(dispatch_tool_call)` → 直接调 `FinsToolService` |
| 截断策略 | `@tool(truncate=...)` 由 `ToolRegistry` 应用 | 无二次截断，信任 `FinsToolLimits` 默认值 |
| 错误语义 | `ToolArgumentError` / `ToolBusinessError` 抛异常 | 返回结构化 `CallToolResult(isError=True)` 含 code/hint |

### 1.4 无状态设计

MCP 工具调用是**无状态**的——每次调用独立，无 session/run 概念。因此 MCP 模块：

- 不创建 `Session` / `Run` 记录
- 不使用 `ConversationMemory`（上下文由 MCP 客户端管理）
- 不使用 `PendingTurn` / `ReplyOutbox`（无多轮 resume 需求）
- 不经过 `CancellationToken` / `ConcurrencyGovernor`

这些能力的缺失是设计意图，不是遗漏。

### 1.5 工具边界决策

**只暴露读取，不暴露写入**。所有工具仅基于已下载到 workspace 的财报文档提供查询，
不提供下载（ingestion）工具，不修改 workspace 数据。

不暴露的具体原因：

- **ingestion 工具**（如 `start_download_job`）：长事务异步操作（可能数分钟），MCP 工具调用模型是同步请求-响应，体验劣于通过 `dayu-cli download` CLI 完成。
- **doc 工具**：暴露文件系统给外部 Agent 有安全风险。
- **web 工具**：外部 Agent（opencode/codex）自带联网能力，功能重叠。
- **utils 工具**（如 `get_current_time`）：外部 Agent 已有时钟能力。

### 1.6 异常与错误设计

MCP 返回的异常是**结构化 JSON**，外部 LLM 可直接解析并据此自我纠正：

```json
{
  "error": true,
  "code": "TOOL_ARGUMENT_ERROR | TOOL_BUSINESS_ERROR | NOT_FOUND | INTERNAL_ERROR | UNKNOWN_TOOL",
  "message": "...",
  "hint": "..."   // 仅 ToolArgumentError 附带
}
```

异常映射规则：

| dayu 异常 | MCP 返回 |
|-----------|----------|
| `ToolArgumentError` | `isError=True`，含 code/message/hint |
| `ToolBusinessError` | `isError=True`，code=`TOOL_BUSINESS_ERROR` |
| `FileNotFoundError` / `KeyError` | `isError=True`，code=`NOT_FOUND` |
| 未知工具名 | `isError=True`，code=`UNKNOWN_TOOL` |
| 其他 `Exception` | `isError=True`，code=`INTERNAL_ERROR` |

关键设计：`ToolArgumentError` 携带 `hint` 字段，引导 LLM 切换到正确的参数
（如切换到正确的 `document_id`、重新调用 `get_document_sections` 获取新 `ref`）。

### 1.7 能力降级

`get_page_content`、`get_financial_statement`、`query_xbrl_facts` 三个工具在底层 processor
不支持时**不抛异常**，而是返回 `supported: false`。外部 LLM 根据此字段判断是否需要切换文档或放弃该能力。

### 1.8 截断策略

`FinsToolService` 内部已通过 `FinsToolLimits` 做截断（如 `read_section_max_chars=80000`），
MCP 层信任此截止，不附加二次截断。如需调整，通过修改 `DefaultFinsRuntime.create()` 传参
或直接修改 `FinsToolLimits` 默认值。

### 1.9 并发模型

`FinsToolService` 全部方法是同步的（涉及 CPU 密集的文档解析和文件 I/O），
MCP server 通过 `asyncio.to_thread()` 将每次调用包装到线程池执行，避免阻塞 asyncio 事件循环。
`FinsToolService` 内部 `ProcessorLRUCache`（128 项）使用 `Lock` + `RLock` 保护，在线程池环境下并发安全。

## 2. 功能

### 2.1 暴露的 9 个工具

| 工具 | 必填参数 | 可选参数 |
|------|---------|---------|
| `list_documents` | `ticker` | `document_types`, `fiscal_years`, `fiscal_periods` |
| `get_document_sections` | `ticker`, `document_id` | — |
| `read_section` | `ticker`, `document_id`, `ref` | — |
| `search_document` | `ticker`, `document_id` | `query`/`queries`(互斥), `within_section_ref`, `mode` |
| `list_tables` | `ticker`, `document_id` | `financial_only`, `within_section_ref` |
| `get_table` | `ticker`, `document_id`, `table_ref` | — |
| `get_page_content` | `ticker`, `document_id`, `page_no`(≥1) | — |
| `get_financial_statement` | `ticker`, `document_id`, `statement_type`(枚举 5 种) | — |
| `query_xbrl_facts` | `ticker`, `document_id` | `concepts`, `statement_type`, `period_end`, `fiscal_year`, `fiscal_period`, `min_value`, `max_value` |

### 2.2 Schema 编写约定

- 面向外部 LLM（Claude/GPT/DeepSeek），用英文描述
- 包含：用途、关键参数说明、使用建议、重要约束
- `inputSchema` 遵循 JSON Schema draft-07
- 枚举参数用 `enum` 约束，数值参数用 `minimum`/`maximum` 约束，数组参数用 `maxItems` 约束
- `ref`、`table_ref` 等引用参数标注"原样复制"的约束

### 2.3 结果格式

正常返回：`[TextContent(type="text", text=json.dumps(result))]`

出错返回：`CallToolResult(isError=True, content=[TextContent(type="text", text=error_json)])`


## 3. 扩展方式

### 3.1 新增财报读取工具

1. 在 `fins_tools.py` 中新增工具 schema 常量（name、description、inputSchema）
2. 追加到 `_FINS_TOOL_DEFINITIONS` 列表
3. 新增对应的 `_dispatch_*()` 调度函数
4. 在 `dispatch_tool_call()` 的 if-else 链中添加分支
5. 在 `tests/mcp/test_mcp.py` 中新增对应的 schema + dispatch 测试

### 3.2 支持 HTTP 传输（SSE）

当前仅支持 stdio。如需远程部署或团队共享：
1. 新增 `dayu/mcp/http_server.py` 使用 `mcp.server.sse.SseServerTransport`
2. 在 `pyproject.toml` 中新增 `dayu-mcp-http` 入口点
3. 考虑鉴权（API token / OAuth）

### 3.3 支持 MCP Resources

如需将财报文档以 MCP Resources 方式暴露（而非仅 Tools）：
1. 使用 `@server.list_resources()` 和 `@server.read_resource()` 装饰器
2. 将 `workspace/portfolio/{ticker}/` 下的文档元数据映射为 `mcp.types.Resource`
3. Resource URI 形如 `filings://{ticker}/documents`、`filings://{ticker}/{doc_id}/sections`
