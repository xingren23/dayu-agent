"""processed 快照仓储辅助函数。

该模块封装 `processed/{document_id}` 目录的仓储访问，避免 pipeline
直接依赖真实文件系统路径。当前职责包括：
- 校验快照文件集合是否匹配当前导出模式
- 清理非目标快照文件
- 安全读取 `tool_snapshot_meta.json`
- 清空某个 ticker 的全部 processed 产物
"""

from __future__ import annotations

import json
from typing import Any, Optional

from dayu.fins.domain.document_models import ProcessedHandle
from dayu.fins.storage import (
    DocumentBlobRepositoryProtocol,
    ProcessedDocumentRepositoryProtocol,
)

from .tool_snapshot_export import (
    TOOL_SNAPSHOT_META_FILE_NAME,
    build_snapshot_file_names,
)


def match_snapshot_files(
    *,
    repository: DocumentBlobRepositoryProtocol,
    ticker: str,
    document_id: str,
    ci: bool,
) -> bool:
    """校验当前模式所需的快照文件是否已全部存在。

    Args:
        repository: 文档文件对象仓储。
        ticker: 股票代码。
        document_id: 文档 ID。
        ci: 是否 CI 模式。

    Returns:
        目录非空、无子目录且当前模式所需快照文件均为现有文件的子集时返回
        `True`；否则返回 `False`。允许存在 sidecar/legacy 文件（如
        `financials.json`），不再要求与期望集合严格相等。

    Raises:
        OSError: 仓储读取失败时抛出。
    """

    handle = ProcessedHandle(ticker=ticker, document_id=document_id)
    entries = repository.list_entries(handle)
    if not entries:
        return False
    if any(not entry.is_file for entry in entries):
        return False
    expected_files = set(build_snapshot_file_names(ci=ci))
    existing_files = {entry.name for entry in entries if entry.is_file}
    return expected_files <= existing_files


def cleanup_processed_snapshot_dir(
    *,
    repository: DocumentBlobRepositoryProtocol,
    ticker: str,
    document_id: str,
    allowed_files: set[str],
) -> None:
    """清理 `processed/{document_id}` 中非目标快照条目。

    Args:
        repository: 文档文件对象仓储。
        ticker: 股票代码。
        document_id: 文档 ID。
        allowed_files: 允许保留的文件名集合。

    Returns:
        无。

    Raises:
        OSError: 仓储删除失败时抛出。
    """

    handle = ProcessedHandle(ticker=ticker, document_id=document_id)
    for entry in repository.list_entries(handle):
        if entry.name in allowed_files:
            continue
        repository.delete_entry(handle, entry.name)


def safe_read_snapshot_meta(
    *,
    repository: DocumentBlobRepositoryProtocol,
    ticker: str,
    document_id: str,
) -> Optional[dict[str, Any]]:
    """安全读取 `tool_snapshot_meta.json`。

    Args:
        repository: 文档文件对象仓储。
        ticker: 股票代码。
        document_id: 文档 ID。

    Returns:
        JSON 字典；文件不存在、解码失败或格式非法时返回 `None`。

    Raises:
        OSError: 仓储底层读取失败且不属于可恢复场景时抛出。
    """

    handle = ProcessedHandle(ticker=ticker, document_id=document_id)
    try:
        payload = json.loads(
            repository.read_file_bytes(handle, TOOL_SNAPSHOT_META_FILE_NAME).decode("utf-8")
        )
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def clear_processed_documents(
    *,
    repository: ProcessedDocumentRepositoryProtocol,
    ticker: str,
) -> None:
    """清空某个 ticker 下的全部 processed 产物。

    Args:
        repository: processed 文档仓储。
        ticker: 股票代码。

    Returns:
        无。

    Raises:
        OSError: 仓储清理失败时抛出。
    """

    repository.clear_processed_documents(ticker)
