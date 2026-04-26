"""Web UI 层 Service 组合根。

为 Streamlit / FastAPI 等 Web UI 提供统一的 Service 装配入口。
UI 层仅依赖本模块返回的协议接口，不直接 import 具体 Service 实现类。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dayu.contracts.session import SessionSource
from dayu.services.chat_service import ChatService
from dayu.services.fins_service import FinsService
from dayu.services.host_admin_service import HostAdminService
from dayu.services.protocols import (
    ChatServiceProtocol,
    FinsServiceProtocol,
    HostAdminServiceProtocol,
    ReplyDeliveryServiceProtocol,
    WriteServiceProtocol,
)
from dayu.services.reply_delivery_service import ReplyDeliveryService
from dayu.services.startup_preparation import PreparedHostRuntimeDependencies, prepare_host_runtime_dependencies
from dayu.services.write_service import WriteService

if TYPE_CHECKING:
    from dayu.startup.workspace import WorkspaceResources


MODULE = "SERVICES.WEB_PREPARATION"


@dataclass(frozen=True)
class WebServices:
    """Web UI 所需的 Service 协议集合。

    属性:
        workspace_root: 工作区根目录。
        workspace: 工作区资源对象。
        fins_service: 财报服务协议实例。
        write_service: 写作服务协议实例。
        chat_service: 聊天服务协议实例。
        host_admin_service: 宿主管理服务协议实例。
        reply_delivery_service: 回复投递服务协议实例。
    """

    workspace_root: Path
    workspace: WorkspaceResources
    fins_service: FinsServiceProtocol
    write_service: WriteServiceProtocol
    chat_service: ChatServiceProtocol
    host_admin_service: HostAdminServiceProtocol
    reply_delivery_service: ReplyDeliveryServiceProtocol


@dataclass(frozen=True)
class WebServicePreparationResult:
    """Web Service 装配结果。

    属性:
        services: 成功装配的 Service 协议集合；装配失败时为 None。
        warnings: 装配过程中的非致命警告信息列表。
    """

    services: WebServices | None
    warnings: list[str]


def prepare_web_services(
    *,
    workspace_root: Path,
) -> WebServicePreparationResult:
    """为 Web UI 装配全部 Service 协议实例。

    本函数是 Web UI 的组合根入口。内部调用 ``prepare_host_runtime_dependencies()``
    获取共享 Host 运行时依赖，再分别构造 FinsService / WriteService / ChatService /
    HostAdminService / ReplyDeliveryService，全部以协议接口返回。

    UI 层不得直接 import 或实例化任何具体 Service 类。

    参数:
        workspace_root: 工作区根目录。

    返回值:
        ``WebServicePreparationResult``，包含可选的 ``WebServices`` 实例与警告列表。

    异常:
        不抛出异常。内部异常会被捕获并转换为 warnings。
    """

    warnings: list[str] = []

    prepared: PreparedHostRuntimeDependencies | None = None
    try:
        prepared = prepare_host_runtime_dependencies(
            workspace_root=workspace_root,
            config_root=None,
            execution_options=None,
            runtime_label="Web Host runtime",
            log_module=MODULE,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Host 运行时依赖初始化失败（功能不可用）: {exc}")
        return WebServicePreparationResult(services=None, warnings=warnings)

    fins_service: FinsServiceProtocol = FinsService(
        host=prepared.host,
        fins_runtime=prepared.fins_runtime,
    )
    host_admin_service: HostAdminServiceProtocol = HostAdminService(host=prepared.host)
    write_service: WriteServiceProtocol = WriteService(
        host=prepared.host,
        host_governance=prepared.host,
        workspace=prepared.workspace,
        scene_execution_acceptance_preparer=prepared.scene_execution_acceptance_preparer,
        company_name_resolver=prepared.fins_runtime.get_company_name,
        company_meta_summary_resolver=prepared.fins_runtime.get_company_meta_summary,
    )
    chat_service: ChatServiceProtocol = ChatService(
        host=prepared.host,
        scene_execution_acceptance_preparer=prepared.scene_execution_acceptance_preparer,
        company_name_resolver=prepared.fins_runtime.get_company_name,
        session_source=SessionSource.WEB,
    )
    reply_delivery_service: ReplyDeliveryServiceProtocol = ReplyDeliveryService(host=prepared.host)

    services = WebServices(
        workspace_root=workspace_root,
        workspace=prepared.workspace,
        fins_service=fins_service,
        write_service=write_service,
        chat_service=chat_service,
        host_admin_service=host_admin_service,
        reply_delivery_service=reply_delivery_service,
    )
    return WebServicePreparationResult(services=services, warnings=warnings)
