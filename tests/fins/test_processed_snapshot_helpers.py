"""processed 快照辅助函数测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dayu.fins.pipelines.processed_snapshot_helpers import match_snapshot_files
from dayu.fins.pipelines.tool_snapshot_export import build_snapshot_file_names
from tests.fins.storage_testkit import build_fs_storage_test_context


def _write_processed_snapshot_dir(
    *,
    processed_dir: Path,
    ci: bool,
    extra_files: tuple[str, ...] = (),
) -> None:
    """写入满足当前 CI 模式的 processed 快照目录。

    Args:
        processed_dir: processed 文档目录。
        ci: 是否 CI 模式。
        extra_files: 额外 sidecar 文件名。

    Returns:
        无。

    Raises:
        OSError: 写入失败时抛出。
    """

    processed_dir.mkdir(parents=True, exist_ok=True)
    for file_name in build_snapshot_file_names(ci=ci):
        (processed_dir / file_name).write_text("{}", encoding="utf-8")
    for file_name in extra_files:
        (processed_dir / file_name).write_text("{}", encoding="utf-8")


@pytest.mark.unit
def test_match_snapshot_files_allows_extra_financials_json(tmp_path: Path) -> None:
    """验证存在 financials.json 时仍可命中跳过判定。"""

    context = build_fs_storage_test_context(tmp_path)
    document_id = "fil_xbrl_skip"
    processed_dir = tmp_path / "portfolio" / "AAPL" / "processed" / document_id
    _write_processed_snapshot_dir(
        processed_dir=processed_dir,
        ci=True,
        extra_files=("financials.json",),
    )

    assert match_snapshot_files(
        repository=context.blob_repository,
        ticker="AAPL",
        document_id=document_id,
        ci=True,
    ) is True


@pytest.mark.unit
def test_match_snapshot_files_rejects_missing_expected_snapshot(tmp_path: Path) -> None:
    """验证缺少任一期望快照文件时不能跳过。"""

    context = build_fs_storage_test_context(tmp_path)
    document_id = "fil_missing_snapshot"
    processed_dir = tmp_path / "portfolio" / "AAPL" / "processed" / document_id
    processed_dir.mkdir(parents=True, exist_ok=True)
    for file_name in build_snapshot_file_names(ci=True)[:-1]:
        (processed_dir / file_name).write_text("{}", encoding="utf-8")

    assert match_snapshot_files(
        repository=context.blob_repository,
        ticker="AAPL",
        document_id=document_id,
        ci=True,
    ) is False


@pytest.mark.unit
def test_match_snapshot_files_rejects_nested_subdirectory(tmp_path: Path) -> None:
    """验证存在子目录时不能跳过。"""

    context = build_fs_storage_test_context(tmp_path)
    document_id = "fil_nested"
    processed_dir = tmp_path / "portfolio" / "AAPL" / "processed" / document_id
    _write_processed_snapshot_dir(processed_dir=processed_dir, ci=False)
    (processed_dir / "nested").mkdir()

    assert match_snapshot_files(
        repository=context.blob_repository,
        ticker="AAPL",
        document_id=document_id,
        ci=False,
    ) is False


@pytest.mark.unit
def test_match_snapshot_files_offline_mode_can_skip_when_ci_snapshots_exist(tmp_path: Path) -> None:
    """验证 offline 模式可在已有 CI 超集快照时跳过。"""

    context = build_fs_storage_test_context(tmp_path)
    document_id = "fil_ci_superset"
    processed_dir = tmp_path / "portfolio" / "AAPL" / "processed" / document_id
    _write_processed_snapshot_dir(processed_dir=processed_dir, ci=True)

    assert match_snapshot_files(
        repository=context.blob_repository,
        ticker="AAPL",
        document_id=document_id,
        ci=False,
    ) is True
