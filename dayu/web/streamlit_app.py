"""Streamlit Web 应用入口。

大禹 Agent 的 Web UI 实现，基于 Streamlit 框架。
提供自选股管理、财报下载、财务分析等功能。

自选股数据存储于 workspace/.dayu/streamlit/watchlist.json，刷新不丢失。

页面布局：
- 左侧边栏：自选股列表 + 管理按钮
- 主功能区：
  - 未选中自选股时：系统介绍 + 操作指引
  - 选中自选股后：财报管理 / 交互式分析 / 分析报告 Tab

Usage:
    streamlit run dayu/web/streamlit_app.py
    # 或使用 CLI：
    dayu-web
    # 或使用模块入口：
    python -m dayu.web
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import streamlit as st

from dayu.log import Log

if TYPE_CHECKING:
    from dayu.services.web_service_preparation import WebServices
    from dayu.web.streamlit.components.watchlist import WatchlistItem

MODULE = "WEB.STREAMLIT.APP"


def _resolve_workspace_root() -> Path:
    """解析 Streamlit 运行工作区目录。

    优先级：
    1. 环境变量 ``DAYU_WORKSPACE``
    2. 当前目录下的 ``workspace``

    使用环境变量而非命令行参数，避免与 Streamlit 自身的参数解析冲突。

    参数:
        无。

    返回值:
        解析后的绝对路径。

    异常:
        无。
    """

    env_workspace = os.environ.get("DAYU_WORKSPACE")
    if env_workspace:
        return Path(env_workspace).resolve()

    return (Path.cwd() / "workspace").resolve()


def _initialize_services() -> WebServices | None:
    """初始化 Streamlit 页面所需 Service 依赖。

    调用 Service 层组合根 ``prepare_web_services()`` 完成全部 Service 装配，
    UI 层不直接 import 或实例化任何具体 Service 类。

    参数:
        无。

    返回值:
        成功时返回 ``WebServices`` 实例；失败时返回 None 并通过 st.warning 展示错误。

    异常:
        不抛出异常。内部异常会转换为 UI warning。
    """

    workspace_root = _resolve_workspace_root()
    Log.info(f"工作区根目录: {workspace_root}", module=MODULE)

    from dayu.services.web_service_preparation import prepare_web_services

    result = prepare_web_services(workspace_root=workspace_root)

    for warning in result.warnings:
        st.warning(warning)

    return result.services


def _configure_streamlit_page() -> None:
    """配置 Streamlit 页面元信息。"""

    st.set_page_config(
        page_title="大禹 Agent",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def _ensure_session_state_initialized() -> None:
    """初始化 Streamlit 会话状态默认键。"""

    if "initialized" not in st.session_state:
        st.session_state["initialized"] = False
        st.session_state["web_services"] = None


def main() -> None:
    """Streamlit 应用主入口。"""

    from dayu.web.streamlit.components.sidebar import render_sidebar
    from dayu.web.streamlit.pages.main_page import render_welcome_page, render_stock_detail_page

    _configure_streamlit_page()
    _ensure_session_state_initialized()

    web_services: WebServices | None = st.session_state["web_services"]

    # 初始化服务（只执行一次）
    if not bool(st.session_state["initialized"]):
        try:
            web_services = _initialize_services()
            st.session_state["web_services"] = web_services
            st.session_state["initialized"] = True
        except Exception as exc:
            st.error(f"服务初始化失败: {exc}")
            st.stop()

    workspace_root = web_services.workspace_root if web_services is not None else _resolve_workspace_root()

    # 左侧边栏：渲染自选股列表
    selected_stock = render_sidebar(workspace_root=workspace_root)

    # 主功能区
    if selected_stock is None:
        render_welcome_page()
    elif web_services is not None:
        render_stock_detail_page(
            selected_stock=selected_stock,
            workspace_root=web_services.workspace_root,
            web_services=web_services,
        )
    else:
        st.error("财报服务不可用，无法展示股票详情。请检查配置后刷新页面。")


def run_streamlit() -> int:
    """启动 Streamlit 服务并返回进程退出码。

    参数:
        无。

    返回值:
        Streamlit 子进程退出码；正常退出通常为 ``0``。

    异常:
        无。子进程启动失败时由 ``subprocess.run`` 抛出异常。
    """

    import subprocess

    app_path = Path(__file__).resolve()

    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(app_path),
        *sys.argv[1:],
    ]

    completed = subprocess.run(cmd, check=False)
    return completed.returncode


if __name__ == "__main__":
    main()
