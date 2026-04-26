## `dayu.web` 开发说明

本文档说明 `dayu.web` 下 Web 适配层的当前实现边界，重点说明 Streamlit 模块。

## 1. 模块定位

- `dayu.web` 是 UI 适配层，负责把宿主入口请求转成 `Service` 调用，并把结果渲染给用户。
- 当前 Web 有两条并行入口：
  - Streamlit UI：`dayu/web/streamlit_app.py`（用户交互主入口），为用户提供本地访问的 Web 页面
  - FastAPI：`dayu/web/fastapi_app.py`（HTTP API 入口）,为独立的 Web 服务提供 API 接口。
- 设计基线保持稳定分层：`UI -> Service -> Host -> Agent`。

细分职责如下：

| 入口文件 | 目标对象 | 主要职责 | 不负责 |
| --- | --- | --- | --- |
| `dayu/web/streamlit_app.py` | 人工交互用户（浏览器页面） | 页面级 UI 交互、会话状态管理（`st.session_state`）、页面路由与本地文件预览入口装配 | 对外 HTTP API 契约、路由 schema 设计 |
| `dayu/web/fastapi_app.py` | 程序化调用方（HTTP 客户端/Worker） | API 装配、路由注册、请求/响应契约与后台任务受理边界 | 页面渲染、前端会话态维护、Streamlit 组件状态 |

统一约束：

- 两条入口都遵循 `UI -> Service -> Host -> Agent`，不允许 UI 直接绕过 `Service` 调 `Host` 内部细节。
- Streamlit UI 通过 `dayu.services.web_service_preparation.prepare_web_services()` 装配全部 Service 协议，UI 层不直接 import 具体 Service 实现类。
- `streamlit_app.py` 可以维护 UI 会话状态，但不扩展成通用 API 网关。
- `fastapi_app.py` 负责稳定 API 契约，但不承载 Streamlit 页面行为或页面状态。
- 同一业务能力优先复用同一组 `ServiceProtocol`，保证 CLI / Streamlit / FastAPI 语义一致。

## 2. Streamlit Web 当前功能

- **自选股管理**（已实现）：添加、删除、编辑自选股，数据持久化至 `workspace/.dayu/streamlit/watchlist.json`
- **页面骨架**（已实现）：侧边栏自选股导航 + 三个功能 Tab（财报管理 / 交互式分析 / 分析报告）
- 财报管理 Tab（占位）：后续接入 `FinsServiceProtocol` 提供财报下载与管理
- 交互式分析 Tab（占位）：后续接入 `ChatServiceProtocol` 提供多轮对话
- 分析报告 Tab（占位）：后续接入 `WriteServiceProtocol` 提供报告生成与展示

## 3. Service 装配

Streamlit UI 的 Service 装配统一由 `dayu.services.web_service_preparation.prepare_web_services()` 完成。
该函数返回 `WebServices` dataclass，包含所有 Service 协议实例（`FinsServiceProtocol`、`WriteServiceProtocol`、`ChatServiceProtocol`、`HostAdminServiceProtocol`、`ReplyDeliveryServiceProtocol`）。

工作区路径通过环境变量 `DAYU_WORKSPACE` 指定，默认为当前目录下的 `workspace/`。
