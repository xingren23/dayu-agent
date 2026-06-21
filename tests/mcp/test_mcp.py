"""MCP server 测试：schema 校验、调度函数、端到端协议兼容性。

测试策略：
- TestToolSchemas: 验证 9 个工具的 MCP schema 结构（无外部依赖）
- TestDispatch: 使用真实 workspace 数据验证 dispatch_tool_call（需 workspace）
- TestEndToEnd: Streamable HTTP 协议兼容性冒烟测试（需 workspace 与 mcp extra）
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp.client.streamable_http")

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import InitializeResult, TextContent, Tool

from dayu.fins.service_runtime import DefaultFinsRuntime
from dayu.fins.tools.service import FinsToolService
from dayu.mcp.fins_tools import build_mcp_tools, dispatch_tool_call
from dayu.mcp.server import (
    TOOL_ARGUMENTS_LOG_MAX_CHARS,
    TOOL_RESPONSE_DEBUG_LOG_MAX_CHARS,
    TOOL_RESPONSE_LOG_MAX_CHARS,
    _format_tool_arguments_for_log,
    _summarize_tool_result_for_log,
    _truncate_log_text,
)

# 工作区根目录；若不存在则跳过需 workspace 的测试
_WORKSPACE_ROOT = Path(os.environ.get("DAYU_WORKSPACE", Path(__file__).resolve().parents[2] / "workspace"))
_HAS_WORKSPACE = _WORKSPACE_ROOT.is_dir() and (_WORKSPACE_ROOT / "portfolio" / "NVDA").is_dir()

requires_workspace = pytest.mark.skipif(
    not _HAS_WORKSPACE,
    reason="workspace 目录不存在，无真实财报数据",
)


# ---------------------------------------------------------------------------
# TestToolSchemas — schema 结构验证
# ---------------------------------------------------------------------------


class TestToolSchemas:
    """验证 9 个工具的 MCP schema 定义结构与内容。"""

    def test_all_tools_have_required_fields(self) -> None:
        """每个 tool 必须包含 name, description, inputSchema。"""
        tools = build_mcp_tools()
        assert len(tools) == 9
        for tool in tools:
            assert tool.name, f"工具缺少 name"
            assert tool.description, f"工具 {tool.name} 缺少 description"
            assert tool.inputSchema is not None, f"工具 {tool.name} 缺少 inputSchema"
            assert tool.inputSchema.get("type") == "object", f"工具 {tool.name} inputSchema 不是 object"
            assert "properties" in tool.inputSchema, f"工具 {tool.name} inputSchema 缺少 properties"

    def test_tool_count(self) -> None:
        """确认为 9 个工具。"""
        assert len(build_mcp_tools()) == 9

    def test_tool_names_are_unique(self) -> None:
        """工具名必须唯一。"""
        tools = build_mcp_tools()
        names = [t.name for t in tools]
        assert len(names) == len(set(names))

    def test_list_documents_required_params(self) -> None:
        """list_documents 仅 ticker 为必填。"""
        tool = _find_tool("list_documents")
        assert tool.inputSchema["required"] == ["ticker"]
        assert "ticker" in tool.inputSchema["properties"]

    def test_get_document_sections_required_params(self) -> None:
        """get_document_sections 需 ticker 和 document_id。"""
        tool = _find_tool("get_document_sections")
        assert set(tool.inputSchema["required"]) == {"ticker", "document_id"}

    def test_read_section_required_params(self) -> None:
        """read_section 需 ticker, document_id, ref。"""
        tool = _find_tool("read_section")
        assert set(tool.inputSchema["required"]) == {"ticker", "document_id", "ref"}

    def test_search_document_mode_enum(self) -> None:
        """search_document 的 mode 有 4 个合法值。"""
        tool = _find_tool("search_document")
        mode_prop = tool.inputSchema["properties"]["mode"]
        assert mode_prop["enum"] == ["auto", "exact", "keyword", "semantic"]

    def test_search_document_queries_max_items(self) -> None:
        """search_document 的 queries 上限为 20。"""
        tool = _find_tool("search_document")
        queries_prop = tool.inputSchema["properties"]["queries"]
        assert queries_prop["maxItems"] == 20

    def test_list_tables_financial_only_type(self) -> None:
        """list_tables 的 financial_only 为 boolean。"""
        tool = _find_tool("list_tables")
        fo_prop = tool.inputSchema["properties"]["financial_only"]
        assert fo_prop["type"] == "boolean"

    def test_get_table_required_params(self) -> None:
        """get_table 需 ticker, document_id, table_ref。"""
        tool = _find_tool("get_table")
        assert set(tool.inputSchema["required"]) == {"ticker", "document_id", "table_ref"}

    def test_get_page_content_page_no_minimum(self) -> None:
        """get_page_content 的 page_no 最小值为 1。"""
        tool = _find_tool("get_page_content")
        pn_prop = tool.inputSchema["properties"]["page_no"]
        assert pn_prop["minimum"] == 1

    def test_get_financial_statement_enum(self) -> None:
        """get_financial_statement 的 statement_type 有 5 个合法值。"""
        tool = _find_tool("get_financial_statement")
        st_prop = tool.inputSchema["properties"]["statement_type"]
        assert st_prop["enum"] == ["income", "balance_sheet", "cash_flow", "equity", "comprehensive_income"]

    def test_query_xbrl_facts_optional_params(self) -> None:
        """query_xbrl_facts 仅 ticker 和 document_id 为必填。"""
        tool = _find_tool("query_xbrl_facts")
        assert set(tool.inputSchema["required"]) == {"ticker", "document_id"}
        assert "concepts" in tool.inputSchema["properties"]
        assert "period_end" in tool.inputSchema["properties"]
        assert "min_value" in tool.inputSchema["properties"]


# ---------------------------------------------------------------------------
# TestDispatch — 调度函数测试（需真实 workspace 数据）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _service() -> FinsToolService:
    """模块级 fixture：FinsToolService 单例。"""
    if not _HAS_WORKSPACE:
        pytest.skip("workspace 目录不存在")
    runtime = DefaultFinsRuntime.create(workspace_root=_WORKSPACE_ROOT)
    return runtime.get_tool_service()


@pytest.fixture(scope="module")
def _nvda_doc_id(_service: FinsToolService) -> str:
    """返回 NVDA 的第一个有效 document_id。"""
    docs = _service.list_documents(ticker="NVDA")
    return docs["documents"][0]["document_id"]


class TestDispatch:
    """使用真实 workspace 数据验证 dispatch_tool_call。"""

    # ---- list_documents ----

    @requires_workspace
    def test_list_documents_nvda_returns_results(self, _service: FinsToolService) -> None:
        """正常返回 NVDA 文档列表。"""
        result = dispatch_tool_call(_service, "list_documents", {"ticker": "NVDA"})
        assert result.get("total", 0) > 0
        assert len(result["documents"]) > 0
        assert result["documents"][0]["document_id"]
        assert result["company"]["ticker"] == "NVDA"

    @requires_workspace
    def test_list_documents_invalid_ticker(self, _service: FinsToolService) -> None:
        """无效 ticker 返回错误。"""
        result = dispatch_tool_call(_service, "list_documents", {"ticker": "ZZ_INVALID_ZZ"})
        assert result.get("error") is True
        assert result.get("code") == "TOOL_BUSINESS_ERROR"

    # ---- get_document_sections ----

    @requires_workspace
    def test_get_document_sections_valid(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """正常返回文档章节结构。"""
        result = dispatch_tool_call(
            _service, "get_document_sections",
            {"ticker": "NVDA", "document_id": _nvda_doc_id},
        )
        assert len(result["sections"]) > 0
        assert result["sections"][0]["ref"]
        assert result["document_id"] == _nvda_doc_id

    @requires_workspace
    def test_get_document_sections_invalid_doc(self, _service: FinsToolService) -> None:
        """无效 document_id 返回错误。"""
        result = dispatch_tool_call(
            _service, "get_document_sections",
            {"ticker": "NVDA", "document_id": "fil_not_exist"},
        )
        assert result.get("error") is True

    # ---- read_section ----

    @requires_workspace
    def test_read_section_valid_ref(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """通过有效 ref 读取章节内容。"""
        sections = dispatch_tool_call(
            _service, "get_document_sections",
            {"ticker": "NVDA", "document_id": _nvda_doc_id},
        )
        first_ref = sections["sections"][0]["ref"]
        result = dispatch_tool_call(
            _service, "read_section",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "ref": first_ref},
        )
        assert result["ref"] == first_ref
        assert len(result["content"]) > 0
        assert result["content_word_count"] > 0

    @requires_workspace
    def test_read_section_invalid_ref(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """无效 ref 返回错误。"""
        result = dispatch_tool_call(
            _service, "read_section",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "ref": "s_nonexistent_99999"},
        )
        assert result.get("error") is True

    # ---- search_document ----

    @requires_workspace
    def test_search_document_single_query(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """单查询搜索返回匹配结果。"""
        result = dispatch_tool_call(
            _service, "search_document",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "query": "revenue"},
        )
        assert result["total_matches"] > 0
        assert len(result["matches"]) > 0
        assert result["mode"] == "auto"

    @requires_workspace
    def test_search_document_batch_queries(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """批量查询搜索返回聚合结果。"""
        result = dispatch_tool_call(
            _service, "search_document",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "queries": ["revenue", "net income"]},
        )
        assert result["total_matches"] > 0
        assert result["queries"] == ["revenue", "net income"]

    # ---- list_tables ----

    @requires_workspace
    def test_list_tables_all(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """列出全部表格。"""
        result = dispatch_tool_call(
            _service, "list_tables",
            {"ticker": "NVDA", "document_id": _nvda_doc_id},
        )
        assert result["total"] > 0
        assert "financial_count" in result
        if result["tables"]:
            assert result["tables"][0]["table_ref"]

    @requires_workspace
    def test_list_tables_financial_only(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """仅列出财务报表。"""
        result = dispatch_tool_call(
            _service, "list_tables",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "financial_only": True},
        )
        for table in result["tables"]:
            assert table.get("is_financial") is True

    # ---- get_table ----

    @requires_workspace
    def test_get_table_valid_ref(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """通过有效 table_ref 读取表格。"""
        tables = dispatch_tool_call(
            _service, "list_tables",
            {"ticker": "NVDA", "document_id": _nvda_doc_id},
        )
        first_ref = tables["tables"][0]["table_ref"]
        result = dispatch_tool_call(
            _service, "get_table",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "table_ref": first_ref},
        )
        assert result["table_ref"] == first_ref
        assert result["row_count"] > 0

    @requires_workspace
    def test_get_table_invalid_ref(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """无效 table_ref 返回错误。"""
        result = dispatch_tool_call(
            _service, "get_table",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "table_ref": "t_nonexistent_99999"},
        )
        assert result.get("error") is True

    # ---- get_page_content ----

    @requires_workspace
    def test_get_page_content(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """读取页面内容（结果可能 supported=True 或 False，均不抛异常）。"""
        result = dispatch_tool_call(
            _service, "get_page_content",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "page_no": 1},
        )
        assert "supported" in result
        assert result["page_no"] == 1

    # ---- get_financial_statement ----

    @requires_workspace
    def test_get_financial_statement_income(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """读取利润表。"""
        result = dispatch_tool_call(
            _service, "get_financial_statement",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "statement_type": "income"},
        )
        assert result.get("statement_type") == "income"
        assert len(result.get("rows", [])) > 0

    @requires_workspace
    def test_get_financial_statement_balance_sheet(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """读取资产负债表。"""
        result = dispatch_tool_call(
            _service, "get_financial_statement",
            {"ticker": "NVDA", "document_id": _nvda_doc_id, "statement_type": "balance_sheet"},
        )
        assert result.get("statement_type") == "balance_sheet"

    # ---- query_xbrl_facts ----

    @requires_workspace
    def test_query_xbrl_facts_default_concepts(self, _service: FinsToolService, _nvda_doc_id: str) -> None:
        """不带 concepts 使用默认概念包查询。"""
        result = dispatch_tool_call(
            _service, "query_xbrl_facts",
            {"ticker": "NVDA", "document_id": _nvda_doc_id},
        )
        assert result.get("document_id") == _nvda_doc_id
        assert "facts" in result

    # ---- 错误路径 ----

    @requires_workspace
    def test_unknown_tool(self, _service: FinsToolService) -> None:
        """未知工具名返回错误。"""
        result = dispatch_tool_call(_service, "no_such_tool_xyz", {})
        assert result.get("error") is True
        assert result.get("code") == "UNKNOWN_TOOL"

    @requires_workspace
    def test_missing_required_param(self, _service: FinsToolService) -> None:
        """缺少必填参数时，dispatch_tool_call 捕获并返回结构化错误。"""
        result = dispatch_tool_call(
            _service, "read_section",
            {"ticker": "NVDA", "document_id": "SOME_DOC"},
        )
        # KeyError 被 dispatch_tool_call 内部捕获并转为错误 dict
        assert result.get("error") is True
        assert result.get("code") in ("TOOL_ARGUMENT_ERROR", "NOT_FOUND")


# ---------------------------------------------------------------------------
# TestEndToEnd — Streamable HTTP 协议兼容性冒烟
# ---------------------------------------------------------------------------

MCP_SERVER_STARTUP_TIMEOUT_SECONDS = 15.0
MCP_SERVER_STARTUP_POLL_INTERVAL_SECONDS = 0.2


def _pick_free_port() -> int:
    """选取本机可用 TCP 端口。

    Returns:
        可用端口号。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _read_subprocess_stderr(proc: asyncio.subprocess.Process) -> str:
    """读取子进程 stderr 文本。

    Args:
        proc: 已启动或已退出的子进程。

    Returns:
        stderr 解码后的字符串；无 stderr 管道时返回空串。
    """
    if proc.stderr is None:
        return ""
    stderr = await proc.stderr.read()
    return stderr.decode()


async def _wait_for_mcp_server_ready(
    port: int,
    proc: asyncio.subprocess.Process,
    *,
    timeout_seconds: float = MCP_SERVER_STARTUP_TIMEOUT_SECONDS,
) -> None:
    """轮询直至 dayu-mcp 可完成 MCP initialize，或子进程已退出/超时。

    Args:
        port: HTTP 监听端口。
        proc: dayu-mcp 子进程。
        timeout_seconds: 最长等待秒数。

    Raises:
        RuntimeError: 子进程提前退出。
        TimeoutError: 超时仍未就绪。
    """
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = asyncio.get_running_loop().time() + timeout_seconds

    while asyncio.get_running_loop().time() < deadline:
        if proc.returncode is not None:
            raise RuntimeError(
                f"dayu-mcp 子进程启动失败，退出码={proc.returncode}: "
                f"{await _read_subprocess_stderr(proc)}"
            )
        try:
            async with streamable_http_client(url) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
            return
        except Exception:
            # 端口尚未监听或 MCP 握手尚未就绪，短暂等待后重试。
            await asyncio.sleep(MCP_SERVER_STARTUP_POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"dayu-mcp 在 {timeout_seconds}s 内未就绪: {url}"
    )


@asynccontextmanager
async def _running_mcp_server(port: int) -> AsyncIterator[asyncio.subprocess.Process]:
    """启动 dayu-mcp 子进程并在退出时清理。

    Args:
        port: HTTP 监听端口。

    Yields:
        已启动的子进程对象。

    Raises:
        RuntimeError: 子进程启动后立即退出时抛出。
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "dayu.mcp.server",
        "--workspace",
        str(_WORKSPACE_ROOT),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await _wait_for_mcp_server_ready(port, proc)
        yield proc
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


@asynccontextmanager
async def _mcp_client_session(
    port: int,
) -> AsyncIterator[tuple[ClientSession, InitializeResult]]:
    """连接运行中的 dayu-mcp Streamable HTTP 服务并建立 ClientSession。

    Args:
        port: HTTP 监听端口。

    Yields:
        已完成 initialize 的 ``(ClientSession, InitializeResult)`` 元组。
    """
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamable_http_client(url) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            yield session, init_result


class TestEndToEnd:
    """端到端 Streamable HTTP 协议兼容性测试。"""

    @requires_workspace
    @pytest.mark.asyncio
    async def test_initialize_and_list_tools(self) -> None:
        """启动 dayu-mcp 子进程，完成初始化握手 + 列出工具。"""
        port = _pick_free_port()
        async with _running_mcp_server(port):
            async with _mcp_client_session(port) as (session, init_result):
                assert init_result.serverInfo.name == "dayu-fins-reader"
                assert init_result.capabilities.tools is not None

                tools_result = await session.list_tools()
                assert len(tools_result.tools) == 9
                tool_names = [tool.name for tool in tools_result.tools]
                assert "list_documents" in tool_names
                assert "read_section" in tool_names
                assert "query_xbrl_facts" in tool_names

    @requires_workspace
    @pytest.mark.asyncio
    async def test_call_tool_list_documents(self) -> None:
        """通过 MCP 协议调用 list_documents 工具。"""
        port = _pick_free_port()
        async with _running_mcp_server(port):
            async with _mcp_client_session(port) as (session, _init_result):
                call_result = await session.call_tool(
                    "list_documents",
                    {"ticker": "NVDA"},
                )
                assert not call_result.isError
                assert len(call_result.content) > 0
                first_content = call_result.content[0]
                assert isinstance(first_content, TextContent)
                data = json.loads(first_content.text)
                assert data["company"]["ticker"] == "NVDA"
                assert data["total"] > 0


# ---------------------------------------------------------------------------
# TestMcpLogging — 请求/响应日志辅助函数
# ---------------------------------------------------------------------------


class TestMcpLogging:
    """验证 MCP 请求/响应日志摘要辅助函数。"""

    def test_truncate_log_text_appends_total_chars(self) -> None:
        """超长文本应截断并附带总长度。"""
        text = "x" * (TOOL_RESPONSE_LOG_MAX_CHARS + 10)
        truncated = _truncate_log_text(text, TOOL_RESPONSE_LOG_MAX_CHARS)
        assert truncated.endswith(f"(truncated, total_chars={len(text)})")
        assert len(truncated) > TOOL_RESPONSE_LOG_MAX_CHARS

    def test_truncate_log_text_debug_limit(self) -> None:
        """DEBUG 级正文预览也应受上限约束。"""
        text = "y" * (TOOL_RESPONSE_DEBUG_LOG_MAX_CHARS + 10)
        truncated = _truncate_log_text(text, TOOL_RESPONSE_DEBUG_LOG_MAX_CHARS)
        assert "truncated" in truncated
        assert f"total_chars={len(text)}" in truncated

    def test_format_tool_arguments_for_log_serializes_mapping(self) -> None:
        """工具参数摘要应输出 JSON 字符串。"""
        summary = _format_tool_arguments_for_log({"ticker": "NVDA", "page_no": 1})
        assert '"ticker": "NVDA"' in summary
        assert '"page_no": 1' in summary

    def test_format_tool_arguments_for_log_truncates_long_payload(self) -> None:
        """过长参数摘要应被截断。"""
        long_query = "q" * (TOOL_ARGUMENTS_LOG_MAX_CHARS + 20)
        summary = _format_tool_arguments_for_log({"query": long_query})
        assert "truncated" in summary

    def test_summarize_tool_result_for_log_success(self) -> None:
        """成功结果摘要应标记 is_error=false。"""
        assert _summarize_tool_result_for_log({"total": 3}) == "is_error=false"

    def test_summarize_tool_result_for_log_error(self) -> None:
        """错误结果摘要应包含 code 与 message。"""
        summary = _summarize_tool_result_for_log(
            {"error": True, "code": "NOT_FOUND", "message": "document missing"}
        )
        assert "is_error=true" in summary
        assert "code=NOT_FOUND" in summary
        assert "document missing" in summary


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _find_tool(name: str) -> Tool:
    """从 build_mcp_tools() 中按名称查找工具。

    Args:
        name: 工具名。

    Returns:
        对应的 Tool 对象。

    Raises:
        AssertionError: 工具不存在时。
    """
    tools = build_mcp_tools()
    for tool in tools:
        if tool.name == name:
            return tool
    raise AssertionError(f"工具 '{name}' 未找到")
