"""dayu MCP 服务器入口。

通过 Streamable HTTP 将 dayu 财报读取工具暴露为 MCP 工具，
供 opencode / codex / claude-code 等外部 Agent 远程调用。

使用方式::

    export DAYU_WORKSPACE=/path/to/workspace
    dayu-mcp --host 127.0.0.1 --port 8000
    # 服务地址: http://127.0.0.1:8000/mcp

    # 或通过命令行参数指定工作区
    dayu-mcp --workspace /path/to/workspace --port 8000

环境变量:
    DAYU_WORKSPACE: 工作区根目录路径（优先级高于 --workspace）
    DAYU_MCP_HOST: 监听地址（优先级高于 --host，默认 127.0.0.1）
    DAYU_MCP_PORT: 监听端口（优先级高于 --port，默认 8000）
    DAYU_LOG_LEVEL: 日志级别，可选 DEBUG/INFO/WARNING/ERROR（默认 INFO）；
        INFO 记录 HTTP 与 tools/call 请求/响应摘要（含响应正文截断），
        DEBUG 在 INFO 基础上输出更长正文预览（上限 4096 字符）
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging.config
import os
import sys
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, TypeAlias

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dayu.fins.service_runtime import DefaultFinsRuntime
from dayu.fins.tools.service import FinsToolService
from dayu.mcp.fins_tools import build_mcp_tools, dispatch_tool_call

logger = logging.getLogger("dayu.mcp")

DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8000
DEFAULT_MCP_MOUNT_PATH = "/mcp"
DEFAULT_MCP_LOG_LEVEL = "INFO"
TOOL_ARGUMENTS_LOG_MAX_CHARS = 256
TOOL_RESPONSE_LOG_MAX_CHARS = 512
TOOL_RESPONSE_DEBUG_LOG_MAX_CHARS = 4096
MILLISECONDS_PER_SECOND = 1000
MCP_LOGGER_NAMES = (
    "dayu.mcp",
    "mcp",
    "mcp.server",
    "mcp.server.streamable_http_manager",
    "uvicorn",
    "uvicorn.error",
)

_server = Server(
    name="dayu-fins-reader",
    version="0.1.0",
)

# 模块级全局，在 server 启动时注入
_tool_service: FinsToolService | None = None

_LogConfigFormatter: TypeAlias = dict[str, str]
_LogConfigHandler: TypeAlias = dict[str, str]
_LogConfigLogger: TypeAlias = dict[str, str | bool | list[str]]
UvicornLogConfigDict: TypeAlias = dict[
    str,
    int
    | bool
    | _LogConfigLogger
    | dict[str, _LogConfigFormatter]
    | dict[str, _LogConfigHandler]
    | dict[str, _LogConfigLogger],
]


def _truncate_log_text(text: str, max_chars: int) -> str:
    """截断日志文本，避免超大响应污染日志。

    Args:
        text: 原始文本。
        max_chars: 允许输出的最大字符数。

    Returns:
        截断后的文本；未超长时返回原文。
    """
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...(truncated, total_chars={len(text)})"


def _format_tool_arguments_for_log(arguments: Mapping[str, Any]) -> str:
    """格式化工具参数摘要，供 INFO 级请求日志使用。

    Args:
        arguments: MCP tools/call 参数字典。

    Returns:
        单行 JSON 摘要；序列化失败时返回占位说明。
    """
    try:
        serialized = json.dumps(dict(arguments), ensure_ascii=False, default=str)
    except TypeError:
        return "<unserializable arguments>"
    return _truncate_log_text(serialized, TOOL_ARGUMENTS_LOG_MAX_CHARS)


def _summarize_tool_result_for_log(result: Mapping[str, Any]) -> str:
    """提取工具结果摘要，供 INFO 级响应日志使用。

    Args:
        result: ``dispatch_tool_call`` 返回的结果字典。

    Returns:
        单行结果摘要。
    """
    if result.get("error"):
        code = str(result.get("code", "UNKNOWN"))
        message = str(result.get("message", ""))
        return f"is_error=true code={code} message={_truncate_log_text(message, TOOL_RESPONSE_LOG_MAX_CHARS)}"
    return "is_error=false"


def _elapsed_milliseconds(started_at: float) -> int:
    """计算自 ``started_at`` 起的耗时（毫秒）。

    Args:
        started_at: ``time.perf_counter()`` 起始值。

    Returns:
        耗时毫秒数。
    """
    return int((time.perf_counter() - started_at) * MILLISECONDS_PER_SECOND)


class _McpHttpAccessLogMiddleware:
    """记录 MCP HTTP 请求与响应状态的 ASGI 中间件。"""

    def __init__(self, app: ASGIApp) -> None:
        """初始化中间件。

        Args:
            app: 被包装的 ASGI 应用。
        """
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """处理 ASGI 请求并在响应开始时记录状态码。

        Args:
            scope: ASGI scope。
            receive: 接收 callable。
            send: 发送 callable。
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        client = scope.get("client")
        client_host = client[0] if client is not None else "-"
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        logger.info(
            "MCP HTTP 请求: client=%s method=%s path=%s",
            client_host,
            method,
            path,
        )
        await self._app(scope, receive, send_wrapper)
        logger.info(
            "MCP HTTP 响应: client=%s method=%s path=%s status=%d",
            client_host,
            method,
            path,
            status_code,
        )


def _resolve_log_level_name(args: argparse.Namespace) -> str:
    """解析日志级别名称。

    优先级：环境变量 ``DAYU_LOG_LEVEL`` > CLI ``--log-level`` > 默认值。

    Args:
        args: 解析后的 CLI 参数。

    Returns:
        大写日志级别名称，例如 ``INFO``。
    """
    env_level = os.environ.get("DAYU_LOG_LEVEL")
    if env_level:
        return env_level.strip().upper()
    if args.log_level:
        return args.log_level.strip().upper()
    return DEFAULT_MCP_LOG_LEVEL


def _build_uvicorn_log_config(level_name: str) -> UvicornLogConfigDict:
    """构建与 dayu-mcp 统一的 uvicorn 日志配置。

    Args:
        level_name: 大写日志级别名称。

    Returns:
        可直接传给 ``logging.config.dictConfig`` 与 ``uvicorn.run(log_config=...)`` 的配置字典。
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "dayu": {
                "format": "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            },
        },
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "formatter": "dayu",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            logger_name: {
                "handlers": ["stderr"],
                "level": level_name,
                "propagate": False,
            }
            for logger_name in MCP_LOGGER_NAMES
        },
        "root": {
            "handlers": ["stderr"],
            "level": level_name,
            "propagate": True,
        },
    }


def _resolve_workspace(args: argparse.Namespace) -> Path:
    """解析工作区根目录。

    优先级：环境变量 ``DAYU_WORKSPACE`` > CLI ``--workspace`` > 当前目录下的 ``workspace/``。

    Args:
        args: 解析后的 CLI 参数。

    Returns:
        工作区根目录绝对路径。

    Raises:
        SystemExit: 工作区目录不存在时退出。
    """
    env_path = os.environ.get("DAYU_WORKSPACE")
    if env_path:
        workspace = Path(env_path).expanduser().resolve()
    elif args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
    else:
        workspace = Path.cwd() / "workspace"

    if not workspace.is_dir():
        print(f"错误：工作区目录不存在: {workspace}", file=sys.stderr)
        raise SystemExit(1)

    logger.info("工作区: %s", workspace)
    return workspace


def _resolve_host(args: argparse.Namespace) -> str:
    """解析 MCP HTTP 监听地址。

    优先级：环境变量 ``DAYU_MCP_HOST`` > CLI ``--host`` > 默认值。

    Args:
        args: 解析后的 CLI 参数。

    Returns:
        监听地址字符串。
    """
    env_host = os.environ.get("DAYU_MCP_HOST")
    if env_host:
        return env_host.strip()
    if args.host:
        return args.host.strip()
    return DEFAULT_MCP_HOST


def _resolve_port(args: argparse.Namespace) -> int:
    """解析 MCP HTTP 监听端口。

    优先级：环境变量 ``DAYU_MCP_PORT`` > CLI ``--port`` > 默认值。

    Args:
        args: 解析后的 CLI 参数。

    Returns:
        监听端口号。

    Raises:
        SystemExit: 端口非法时退出。
    """
    env_port = os.environ.get("DAYU_MCP_PORT")
    if env_port:
        try:
            return int(env_port.strip())
        except ValueError:
            print(f"错误：DAYU_MCP_PORT 非法: {env_port}", file=sys.stderr)
            raise SystemExit(1) from None
    if args.port is not None:
        return args.port
    return DEFAULT_MCP_PORT


@_server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """返回 9 个财报读取工具的 MCP 工具列表。

    Returns:
        MCP Tool 对象列表。
    """
    logger.info("MCP tools/list 请求")
    tools = build_mcp_tools()
    tool_names = ", ".join(tool.name for tool in tools)
    logger.info(
        "MCP tools/list 响应: tool_count=%d tools=%s",
        len(tools),
        _truncate_log_text(tool_names, TOOL_RESPONSE_LOG_MAX_CHARS),
    )
    return tools


@_server.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict[str, Any],
) -> list[TextContent] | CallToolResult:
    """处理 MCP 工具调用请求。

    Args:
        name: 工具名称。
        arguments: 工具调用参数。

    Returns:
        正常时返回 ``list[TextContent]``；出错时返回 ``CallToolResult(isError=True)``。
    """
    if _tool_service is None:
        raise RuntimeError("FinsToolService 未初始化")

    started_at = time.perf_counter()
    logger.info(
        "MCP tools/call 请求: tool=%s arguments=%s",
        name,
        _format_tool_arguments_for_log(arguments),
    )

    result = await asyncio.to_thread(
        dispatch_tool_call, _tool_service, name, arguments
    )

    result_json = json.dumps(result, ensure_ascii=False, default=str)
    elapsed_ms = _elapsed_milliseconds(started_at)
    result_summary = _summarize_tool_result_for_log(result)

    if isinstance(result, dict) and result.get("error"):
        logger.warning(
            "MCP tools/call 响应: tool=%s %s response_bytes=%d elapsed_ms=%d excerpt=%s",
            name,
            result_summary,
            len(result_json),
            elapsed_ms,
            _truncate_log_text(result_json, TOOL_RESPONSE_LOG_MAX_CHARS),
        )
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=result_json)],
        )

    logger.info(
        "MCP tools/call 响应: tool=%s %s response_bytes=%d elapsed_ms=%d excerpt=%s",
        name,
        result_summary,
        len(result_json),
        elapsed_ms,
        _truncate_log_text(result_json, TOOL_RESPONSE_LOG_MAX_CHARS),
    )
    logger.debug(
        "MCP tools/call 响应正文: tool=%s body=%s",
        name,
        _truncate_log_text(result_json, TOOL_RESPONSE_DEBUG_LOG_MAX_CHARS),
    )
    return [TextContent(type="text", text=result_json)]


def _build_streamable_http_app() -> ASGIApp:
    """构建 Streamable HTTP ASGI 应用。

    Returns:
        已挂载 MCP 路由并启用 CORS 的 ASGI 应用。
    """
    session_manager = StreamableHTTPSessionManager(
        app=_server,
        event_store=None,
        json_response=True,
        stateless=True,
    )

    async def handle_streamable_http(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info("Streamable HTTP session manager 已启动")
            yield

    starlette_app = Starlette(
        routes=[
            Mount(DEFAULT_MCP_MOUNT_PATH, app=handle_streamable_http),
        ],
        lifespan=lifespan,
    )

    return _McpHttpAccessLogMiddleware(
        CORSMiddleware(
            starlette_app,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE"],
            expose_headers=["Mcp-Session-Id"],
        )
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    Returns:
        ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(
        description="dayu MCP 服务器 — 通过 Streamable HTTP 暴露财报读取工具",
    )
    parser.add_argument(
        "--workspace",
        help="工作区根目录路径（也可通过环境变量 DAYU_WORKSPACE 设置）",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_MCP_HOST,
        help=f"HTTP 监听地址（默认 {DEFAULT_MCP_HOST}，也可通过 DAYU_MCP_HOST 设置）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_MCP_PORT,
        help=f"HTTP 监听端口（默认 {DEFAULT_MCP_PORT}，也可通过 DAYU_MCP_PORT 设置）",
    )
    parser.add_argument(
        "--log-level",
        default=DEFAULT_MCP_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help=f"日志级别（默认 {DEFAULT_MCP_LOG_LEVEL}，输出到 stderr）",
    )
    return parser


def main() -> int:
    """MCP 服务器入口函数。

    Returns:
        进程退出码，0 表示正常退出。
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    log_level_name = _resolve_log_level_name(args)
    # 启动 uvicorn 前先配置日志，使 Fins 初始化阶段也能输出到 stderr。
    log_config = _build_uvicorn_log_config(log_level_name)
    logging.config.dictConfig(log_config)

    try:
        workspace = _resolve_workspace(args)
        host = _resolve_host(args)
        port = _resolve_port(args)
    except SystemExit:
        return 1

    logger.info("正在初始化 Fins 运行时...")
    runtime = DefaultFinsRuntime.create(workspace_root=workspace)
    global _tool_service
    _tool_service = runtime.get_tool_service()
    logger.info("Fins 运行时初始化完成，工具就绪。")

    mcp_url = f"http://{host}:{port}{DEFAULT_MCP_MOUNT_PATH}"
    logger.info("正在启动 Streamable HTTP MCP 服务: %s", mcp_url)
    logger.info(
        "日志级别=%s；客户端连接后将记录 HTTP 与 tools/list、tools/call 请求/响应",
        log_level_name,
    )

    app = _build_streamable_http_app()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=log_config,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
