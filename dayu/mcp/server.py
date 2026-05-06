"""dayu MCP 服务器入口。

通过 stdio 将 dayu 财报读取工具暴露为 MCP 工具，
供 opencode / codex / claude-code 等外部 Agent 调用。

使用方式::

    # 通过环境变量指定工作区
    export DAYU_WORKSPACE=/path/to/workspace
    dayu-mcp

    # 或通过命令行参数
    dayu-mcp --workspace /path/to/workspace

环境变量:
    DAYU_WORKSPACE: 工作区根目录路径（优先级高于 --workspace）
    DAYU_LOG_LEVEL: 日志级别，可选 DEBUG/INFO/WARNING/ERROR（默认 WARNING）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent

from dayu.fins.service_runtime import DefaultFinsRuntime
from dayu.fins.tools.service import FinsToolService
from dayu.mcp.fins_tools import build_mcp_tools, dispatch_tool_call

logger = logging.getLogger("dayu.mcp")

_server = Server(
    name="dayu-fins-reader",
    version="0.1.0",
)

# 模块级全局，在 server 启动时注入
_tool_service: FinsToolService | None = None


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


@_server.list_tools()
async def handle_list_tools() -> list:
    """返回 9 个财报读取工具的 MCP 工具列表。

    Returns:
        MCP Tool 对象列表。
    """
    return build_mcp_tools()


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
    assert _tool_service is not None, "FinsToolService 未初始化"

    result = await asyncio.to_thread(
        dispatch_tool_call, _tool_service, name, arguments
    )

    result_json = json.dumps(result, ensure_ascii=False, default=str)

    if isinstance(result, dict) and result.get("error"):
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=result_json)],
        )

    return [TextContent(type="text", text=result_json)]


def _build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    Returns:
        ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(
        description="dayu MCP 服务器 — 将财报读取工具暴露给外部 Agent",
    )
    parser.add_argument(
        "--workspace",
        help="工作区根目录路径（也可通过环境变量 DAYU_WORKSPACE 设置）",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（输出到 stderr）",
    )
    return parser


def main() -> int:
    """MCP 服务器入口函数。

    Returns:
        进程退出码，0 表示正常退出。
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    log_level = os.environ.get("DAYU_LOG_LEVEL", args.log_level or "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.WARNING),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        workspace = _resolve_workspace(args)
    except SystemExit:
        return 1

    logger.info("正在初始化 Fins 运行时...")
    runtime = DefaultFinsRuntime.create(workspace_root=workspace)
    global _tool_service
    _tool_service = runtime.get_tool_service()
    logger.info("Fins 运行时初始化完成，工具就绪。")

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await _server.run(
                read_stream,
                write_stream,
                _server.create_initialization_options(),
            )

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
