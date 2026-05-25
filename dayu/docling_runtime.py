"""Docling 运行时装配辅助。

本模块是 Dayu 所有 Docling PDF 转换入口的总控真源，统一负责：

1. 解析稳定的设备策略；
2. 构造带统一参数的 Docling `DocumentConverter`；
3. 维护一条二维（backend × device）的有序回退尝试链，自动绕开
   docling-parse 后端在某些上市公司年报 PDF 上把合法文档判定为
   ``not valid`` 的情况，并兼容 GPU/MPS 推理崩溃后的 CPU 兜底。

当前策略：

- 若显式设置环境变量 ``DAYU_DOCLING_DEVICE``，则以该值为准。
- 若未显式设置，则默认使用 ``auto``。
- 转换尝试链按平台展开：
  - 非 Windows（macOS / Linux）：
    1. ``backend=docling-parse, device=resolved``：保留现状，覆盖正常路径。
    2. ``backend=pypdfium2, device=resolved``：救 docling-parse 解析失败。
    3. ``backend=docling-parse, device=cpu``：仅当 ``resolved == auto`` 追加，
       救加速器栈在 ``auto`` 阶段崩溃的故障。
  - Windows + 显式加速器设备（``cuda/mps/xpu``）或 ``auto`` 且 CUDA 可用：
    1. ``backend=docling-parse, device=resolved``：GPU 路径实测稳定，
       优先保留解析质量。
    2. ``backend=pypdfium2, device=resolved``：救 docling-parse 解析失败。
  - Windows + ``cpu``，或 ``auto`` 但 CUDA 不可用：
    1. ``backend=pypdfium2, device=resolved``：优先，规避 docling-parse 在
       Windows 上的 ``std::bad_alloc`` 与 mbcs 路径编码两类已知崩溃。
    2. ``backend=docling-parse, device=resolved``：兜底，给特殊文档保留机会。
    3. ``backend=docling-parse, device=cpu``：仅当 ``resolved == auto`` 追加。
- 任意一档成功即返回；全部失败时抛出最后一次异常，并以**首次**失败
  作为 ``__cause__``，便于排查首因。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeVar, cast

from dayu.log import Log
from dayu.pdf_splitter import get_pdf_page_count as _get_pdf_page_count, split_pdf_bytes as _split_pdf_bytes

if TYPE_CHECKING:
    from docling.backend.abstract_backend import AbstractDocumentBackend
    from docling.datamodel.accelerator_options import AcceleratorOptions
    from docling.datamodel.base_models import DocumentStream
    from docling.datamodel.document import ConversionResult
    from docling_core.types.doc.document import DoclingDocument
    from docling.datamodel.pipeline_options import PipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter

DOCLING_DEVICE_ENV = "DAYU_DOCLING_DEVICE"
DOCLING_CHUNK_SIZE_ENV = "DAYU_DOCLING_CHUNK_SIZE"
_SUPPORTED_DOCLING_DEVICES = frozenset({"auto", "cpu", "cuda", "mps", "xpu"})
_AUTO_DEVICE_NAME = "auto"
_CPU_DEVICE_NAME = "cpu"
_CUDA_DEVICE_NAME = "cuda"
_MPS_DEVICE_NAME = "mps"
_XPU_DEVICE_NAME = "xpu"
_ACCELERATOR_DEVICE_NAMES = frozenset(
    {_CUDA_DEVICE_NAME, _MPS_DEVICE_NAME, _XPU_DEVICE_NAME}
)
_DOCLING_PARSE_BACKEND_NAME = "docling-parse"
_PYPDFIUM2_BACKEND_NAME = "pypdfium2"
_SUPPORTED_DOCLING_BACKENDS = frozenset({_DOCLING_PARSE_BACKEND_NAME, _PYPDFIUM2_BACKEND_NAME})
_TABLE_MODE_ACCURATE = "accurate"
_TABLE_MODE_FAST = "fast"
_MODULE = __name__
_WINDOWS_PLATFORM_NAME = "win32"
_TResult = TypeVar("_TResult")
# Protocol 返回值需要协变，才能让更具体的转换结果回调安全替换更宽的调用点。
_TResultCovariant = TypeVar("_TResultCovariant", covariant=True)

_DOCLING_CHUNK_PAGE_SIZE_DEFAULT = 40
_CHUNK_CATEGORY_NAMES = ("texts", "tables", "groups", "pictures")


class _DoclingConversionResult(Protocol):
    """Docling 转换结果最小协议（兼容 ConversionResult 与分片包装）。"""

    @property
    def document(self) -> "DoclingDocument":
        ...


@dataclass(frozen=True)
class _ChunkedConversionResult:
    """分片合并后的转换结果包装。"""

    document: "DoclingDocument"



class DoclingRuntimeInitializationError(RuntimeError):
    """Docling 运行时初始化错误。"""


class _DoclingTableStructureOptionsProtocol(Protocol):
    """Docling 表格结构选项最小协议。"""

    mode: "TableFormerMode"
    do_cell_matching: bool


class _DoclingPdfPipelineOptionsProtocol(Protocol):
    """Docling PDF pipeline 选项最小协议。"""

    do_ocr: bool
    do_table_structure: bool
    accelerator_options: "AcceleratorOptions | None"
    table_structure_options: _DoclingTableStructureOptionsProtocol


class _DoclingPdfConvertOperation(Protocol[_TResultCovariant]):
    """Docling PDF 转换执行回调协议。"""

    def __call__(self, converter: "DocumentConverter") -> _TResultCovariant:
        """使用已构造的转换器执行一次转换。"""

        ...


@dataclass(frozen=True)
class _DoclingConversionAttempt:
    """一次 Docling PDF 转换尝试的描述。

    每个尝试由 (backend_name, device_name) 二元组唯一标识：

    - ``backend_name``：决定 PDF 解析后端，可选 ``docling-parse`` 或 ``pypdfium2``。
    - ``device_name``：决定加速器设备，与 ``DAYU_DOCLING_DEVICE`` 同空间。
    """

    backend_name: str
    device_name: str


def _normalize_docling_device_name(device_name: str) -> str:
    """规范化并校验 Docling 设备名。

    Args:
        device_name: 候选设备名。

    Returns:
        规范化后的设备名。

    Raises:
        DoclingRuntimeInitializationError: 设备名不在允许列表时抛出。
    """

    normalized_device_name = device_name.strip().lower()
    if normalized_device_name not in _SUPPORTED_DOCLING_DEVICES:
        supported = ", ".join(sorted(_SUPPORTED_DOCLING_DEVICES))
        raise DoclingRuntimeInitializationError(
            f"{DOCLING_DEVICE_ENV} 不支持 {normalized_device_name!r}；"
            f"允许值: {supported}"
        )
    return normalized_device_name


def _normalize_docling_backend_name(backend_name: str) -> str:
    """规范化并校验 Docling backend 名。

    Args:
        backend_name: 候选 backend 名。

    Returns:
        规范化后的 backend 名。

    Raises:
        DoclingRuntimeInitializationError: backend 名不在允许列表时抛出。
    """

    normalized_backend_name = backend_name.strip().lower()
    if normalized_backend_name not in _SUPPORTED_DOCLING_BACKENDS:
        supported = ", ".join(sorted(_SUPPORTED_DOCLING_BACKENDS))
        raise DoclingRuntimeInitializationError(
            f"不支持的 Docling backend {normalized_backend_name!r}；"
            f"允许值: {supported}"
        )
    return normalized_backend_name


def _resolve_backend_class(backend_name: str) -> type["AbstractDocumentBackend"]:
    """把 backend 名映射到 Docling 后端实现类。

    集中此处的 lazy import 是为了将 Docling 第三方依赖收口到真源单点，
    便于错误信息统一与依赖缺失的兜底。

    Args:
        backend_name: 已规范化的 backend 名。

    Returns:
        Docling 后端实现类。

    Raises:
        DoclingRuntimeInitializationError: Docling 依赖缺失或 backend 名非法时抛出。
    """

    normalized_backend_name = _normalize_docling_backend_name(backend_name)
    try:
        if normalized_backend_name == _DOCLING_PARSE_BACKEND_NAME:
            from docling.backend.docling_parse_backend import DoclingParseDocumentBackend

            return DoclingParseDocumentBackend
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

        return PyPdfiumDocumentBackend
    except ImportError as exc:  # pragma: no cover - 依赖缺失保护
        raise DoclingRuntimeInitializationError(
            f"Docling 未安装，无法解析 backend {normalized_backend_name!r}"
        ) from exc


def resolve_docling_device_name() -> str:
    """解析当前 Docling PDF 转换应使用的设备名。

    Args:
        无。

    Returns:
        Docling 设备名，取值为 ``auto/cpu/cuda/mps/xpu`` 之一。

    Raises:
        DoclingRuntimeInitializationError: 当 ``DAYU_DOCLING_DEVICE`` 配置了不支持的值时抛出。
    """

    configured_device = str(os.environ.get(DOCLING_DEVICE_ENV, "") or "").strip()
    if configured_device:
        return _normalize_docling_device_name(configured_device)

    return _AUTO_DEVICE_NAME


def resolve_docling_chunk_size() -> int:
    """解析 Docling PDF 分片转换的页数阈值。

    通过环境变量 ``DAYU_DOCLING_CHUNK_SIZE`` 配置，默认 40。
    解析到非法值（非正整数）时静默回退到默认值。

    Args:
        无。

    Returns:
        分片页数阈值，始终为正整数。

    Raises:
        无。
    """
    env_value = str(os.environ.get(DOCLING_CHUNK_SIZE_ENV, "") or "").strip()
    if env_value:
        try:
            size = int(env_value)
            if size > 0:
                return size
        except (ValueError, TypeError):
            pass
    return _DOCLING_CHUNK_PAGE_SIZE_DEFAULT


def _is_windows_platform() -> bool:
    """判断当前进程是否运行在 Windows 平台。

    Args:
        无。

    Returns:
        True 表示当前为 Windows，False 表示 macOS / Linux 等其它平台。

    Raises:
        无。
    """

    return sys.platform == _WINDOWS_PLATFORM_NAME


def _is_explicit_accelerator_device(device_name: str) -> bool:
    """判断设备名是否指向显式加速器设备。

    Args:
        device_name: 已规范化的 Docling 设备名。

    Returns:
        True 表示设备名是显式加速器，False 表示 ``auto`` 或 ``cpu``。

    Raises:
        无。
    """

    return device_name in _ACCELERATOR_DEVICE_NAMES


def _is_windows_cuda_available() -> bool:
    """探测当前 Windows 运行环境是否可用 CUDA。

    此处 lazy import `torch` 是为了把硬件探测成本限制在 Windows auto 设备
    的策略分支内；Docling 运行时本身已经依赖 torch，探测失败时保守视为
    CUDA 不可用。

    Args:
        无。

    Returns:
        True 表示 CUDA 当前可用，False 表示不可用或探测失败。

    Raises:
        无。
    """

    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _should_prefer_docling_parse_first_on_windows(resolved_device_name: str) -> bool:
    """判断 Windows 平台是否应优先使用 docling-parse。

    Args:
        resolved_device_name: 已规范化的 Docling 设备名。

    Returns:
        True 表示优先使用 ``docling-parse``，False 表示优先使用 ``pypdfium2``。

    Raises:
        无。
    """

    if _is_explicit_accelerator_device(resolved_device_name):
        return True
    return resolved_device_name == _AUTO_DEVICE_NAME and _is_windows_cuda_available()


def _plan_conversion_attempts(resolved_device_name: str) -> list[_DoclingConversionAttempt]:
    """按二维回退策略生成 Docling 转换尝试链。

    Windows 上 docling-parse 后端在某些 PDF 上会在 C++ 层抛 ``std::bad_alloc``，
    且对 mbcs 路径编码也存在已知 bug；为减少 CPU/auto 路径的首次失败概率
    与日志刷屏，Windows 在未显式选择加速器设备且 CUDA 不可用时把
    ``pypdfium2`` 提到首位。Windows 显式加速器设备、Windows auto 且 CUDA
    可用、以及其它平台保持 ``docling-parse`` 优先，以维持既有解析质量。

    Args:
        resolved_device_name: 已规范化的 Docling 设备名。

    Returns:
        有序的尝试链，至少包含一项。

    Raises:
        无。
    """

    prefers_pypdfium2_first = _is_windows_platform() and not (
        _should_prefer_docling_parse_first_on_windows(resolved_device_name)
    )
    if prefers_pypdfium2_first:
        attempts: list[_DoclingConversionAttempt] = [
            _DoclingConversionAttempt(
                backend_name=_PYPDFIUM2_BACKEND_NAME,
                device_name=resolved_device_name,
            ),
            _DoclingConversionAttempt(
                backend_name=_DOCLING_PARSE_BACKEND_NAME,
                device_name=resolved_device_name,
            ),
        ]
    else:
        attempts = [
            _DoclingConversionAttempt(
                backend_name=_DOCLING_PARSE_BACKEND_NAME,
                device_name=resolved_device_name,
            ),
            _DoclingConversionAttempt(
                backend_name=_PYPDFIUM2_BACKEND_NAME,
                device_name=resolved_device_name,
            ),
        ]
    if resolved_device_name == _AUTO_DEVICE_NAME:
        attempts.append(
            _DoclingConversionAttempt(
                backend_name=_DOCLING_PARSE_BACKEND_NAME,
                device_name=_CPU_DEVICE_NAME,
            )
        )
    return attempts


def build_docling_pdf_converter(
    *,
    do_ocr: bool = True,
    do_table_structure: bool = True,
    table_mode: str = _TABLE_MODE_ACCURATE,
    do_cell_matching: bool = True,
    device_name: str | None = None,
    backend_name: str = _DOCLING_PARSE_BACKEND_NAME,
) -> "DocumentConverter":
    """构造带稳定设备与 backend 策略的 Docling PDF 转换器。

    Args:
        do_ocr: 是否开启 OCR。
        do_table_structure: 是否开启表格结构识别。
        table_mode: 表格结构模式，仅支持 ``accurate`` 或 ``fast``。
        do_cell_matching: 是否开启表格单元格匹配。
        device_name: 显式设备名；为空时按 `resolve_docling_device_name()` 解析。
        backend_name: 显式 PDF backend 名；默认 ``docling-parse``。

    Returns:
        配置完成的 Docling `DocumentConverter`。

    Raises:
        DoclingRuntimeInitializationError: Docling 依赖未安装、设备或 backend 配置非法时抛出。
        ValueError: `table_mode` 非法时抛出。
    """

    pipeline_options = build_docling_pdf_pipeline_options(
        do_ocr=do_ocr,
        do_table_structure=do_table_structure,
        table_mode=table_mode,
        do_cell_matching=do_cell_matching,
        device_name=device_name,
    )

    backend_class = _resolve_backend_class(backend_name)

    try:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:  # pragma: no cover - 依赖缺失保护
        raise DoclingRuntimeInitializationError("Docling 未安装，无法构造 PDF 转换器") from exc

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=cast("PipelineOptions", pipeline_options),
                backend=backend_class,
            ),
        }
    )


def build_docling_pdf_pipeline_options(
    *,
    do_ocr: bool = True,
    do_table_structure: bool = True,
    table_mode: str = _TABLE_MODE_ACCURATE,
    do_cell_matching: bool = True,
    device_name: str | None = None,
) -> _DoclingPdfPipelineOptionsProtocol:
    """构造带稳定设备策略的 Docling PDF pipeline 选项。

    Args:
        do_ocr: 是否开启 OCR。
        do_table_structure: 是否开启表格结构识别。
        table_mode: 表格结构模式，仅支持 ``accurate`` 或 ``fast``。
        do_cell_matching: 是否开启表格单元格匹配。
        device_name: 显式设备名；为空时按 `resolve_docling_device_name()` 解析。

    Returns:
        配置完成的 Docling PDF pipeline 选项对象。

    Raises:
        DoclingRuntimeInitializationError: Docling 依赖未安装或设备环境变量非法时抛出。
        ValueError: `table_mode` 非法时抛出。
    """

    normalized_table_mode = table_mode.strip().lower()
    if normalized_table_mode not in {_TABLE_MODE_ACCURATE, _TABLE_MODE_FAST}:
        raise ValueError(f"不支持的 Docling table_mode: {table_mode}")

    try:
        from docling.datamodel.accelerator_options import (
            AcceleratorOptions,
            AcceleratorDevice,
        )
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            TableFormerMode,
        )
    except ImportError as exc:  # pragma: no cover - 依赖缺失保护
        raise DoclingRuntimeInitializationError("Docling 未安装，无法构造 PDF pipeline 选项") from exc

    normalized_device_name = (
        resolve_docling_device_name()
        if device_name is None
        else _normalize_docling_device_name(device_name)
    )

    pipeline_options = cast(_DoclingPdfPipelineOptionsProtocol, PdfPipelineOptions())
    pipeline_options.do_ocr = do_ocr
    pipeline_options.do_table_structure = do_table_structure
    pipeline_options.accelerator_options = AcceleratorOptions(
        device=AcceleratorDevice(normalized_device_name)
    )

    if do_table_structure:
        table_structure_options = cast(
            _DoclingTableStructureOptionsProtocol,
            pipeline_options.table_structure_options,
        )
        table_structure_options.mode = (
            TableFormerMode.ACCURATE
            if normalized_table_mode == _TABLE_MODE_ACCURATE
            else TableFormerMode.FAST
        )
        table_structure_options.do_cell_matching = do_cell_matching

    return pipeline_options


def _build_attempt_converter(
    attempt: _DoclingConversionAttempt,
    *,
    do_ocr: bool,
    do_table_structure: bool,
    table_mode: str,
    do_cell_matching: bool,
    attempt_index: int,
    total_attempts: int,
) -> "DocumentConverter":
    """为指定尝试构造 Docling 转换器，并把初始化异常包装成统一类型。

    Args:
        attempt: 当前尝试描述。
        do_ocr: 是否开启 OCR。
        do_table_structure: 是否开启表格结构识别。
        table_mode: 表格结构模式。
        do_cell_matching: 是否开启表格单元格匹配。
        attempt_index: 当前尝试在尝试链中的 0 基序号。
        total_attempts: 尝试链总长度。

    Returns:
        Docling 转换器实例。

    Raises:
        DoclingRuntimeInitializationError: 初始化失败时抛出。
    """

    try:
        return build_docling_pdf_converter(
            do_ocr=do_ocr,
            do_table_structure=do_table_structure,
            table_mode=table_mode,
            do_cell_matching=do_cell_matching,
            device_name=attempt.device_name,
            backend_name=attempt.backend_name,
        )
    except DoclingRuntimeInitializationError:
        raise
    except Exception as exc:
        raise DoclingRuntimeInitializationError(
            f"Docling 转换器初始化失败 (attempt {attempt_index + 1}/{total_attempts}: "
            f"backend={attempt.backend_name}, device={attempt.device_name}): {exc}"
        ) from exc


def run_docling_pdf_conversion(
    convert_operation: _DoclingPdfConvertOperation[_TResult],
    *,
    do_ocr: bool = True,
    do_table_structure: bool = True,
    table_mode: str = _TABLE_MODE_ACCURATE,
    do_cell_matching: bool = True,
) -> _TResult:
    """执行带二维（backend × device）回退的 Docling PDF 转换。

    尝试链由 `_plan_conversion_attempts` 给出，命中即返回；全链失败时抛出
    最后一次异常，并以首次失败作为 ``__cause__`` 保留首因。

    Args:
        convert_operation: 接收 `DocumentConverter` 并执行具体转换的回调。
        do_ocr: 是否开启 OCR。
        do_table_structure: 是否开启表格结构识别。
        table_mode: 表格结构模式，仅支持 ``accurate`` 或 ``fast``。
        do_cell_matching: 是否开启表格单元格匹配。

    Returns:
        由 `convert_operation` 返回的转换结果。

    Raises:
        DoclingRuntimeInitializationError: Docling 依赖缺失或设备配置非法时抛出。
        ValueError: `table_mode` 非法时抛出。
    """

    resolved_device_name = resolve_docling_device_name()
    attempts = _plan_conversion_attempts(resolved_device_name)
    total_attempts = len(attempts)
    Log.debug(
        (
            "Docling 转换尝试链装配完成: "
            f"platform={'windows' if _is_windows_platform() else 'unix'} "
            f"resolved_device={resolved_device_name} "
            f"total_attempts={total_attempts} "
            f"chain={[(a.backend_name, a.device_name) for a in attempts]}"
        ),
        module=_MODULE,
    )
    first_failure: Exception | None = None
    last_failure: Exception | None = None
    for attempt_index, attempt in enumerate(attempts):
        Log.debug(
            (
                "Docling 转换尝试启动: "
                f"attempt={attempt_index + 1}/{total_attempts} "
                f"backend={attempt.backend_name} device={attempt.device_name}"
            ),
            module=_MODULE,
        )
        converter = _build_attempt_converter(
            attempt,
            do_ocr=do_ocr,
            do_table_structure=do_table_structure,
            table_mode=table_mode,
            do_cell_matching=do_cell_matching,
            attempt_index=attempt_index,
            total_attempts=total_attempts,
        )
        try:
            result = convert_operation(converter)
        except Exception as exc:
            last_failure = exc
            if first_failure is None:
                first_failure = exc
            if attempt_index + 1 < total_attempts:
                next_attempt = attempts[attempt_index + 1]
                Log.warn(
                    (
                        "Docling 转换失败，准备按尝试链回退: "
                        f"attempt={attempt_index + 1}/{total_attempts} "
                        f"failed_backend={attempt.backend_name} "
                        f"failed_device={attempt.device_name} "
                        f"next_backend={next_attempt.backend_name} "
                        f"next_device={next_attempt.device_name} "
                        f"error_type={type(exc).__name__} error={exc}"
                    ),
                    module=_MODULE,
                )
                continue
        else:
            Log.debug(
                (
                    "Docling 转换尝试成功: "
                    f"attempt={attempt_index + 1}/{total_attempts} "
                    f"backend={attempt.backend_name} device={attempt.device_name}"
                ),
                module=_MODULE,
            )
            return result
    # 全链失败：保留首次失败为 __cause__，便于排查首因。
    assert last_failure is not None
    if first_failure is not None and first_failure is not last_failure:
        raise last_failure from first_failure
    raise last_failure


def _build_docling_document_stream(raw_bytes: bytes, *, stream_name: str) -> "DocumentStream":
    """构造 Docling ``DocumentStream``。

    Args:
        raw_bytes: 原始字节内容。
        stream_name: 流名称，决定 Docling 解析模式（按扩展名判别）。

    Returns:
        构造完成的 Docling ``DocumentStream`` 对象。

    Raises:
        DoclingRuntimeInitializationError: Docling 依赖缺失时抛出。
    """

    from io import BytesIO

    try:
        from docling.datamodel.base_models import DocumentStream
    except ImportError as exc:  # pragma: no cover - 依赖缺失保护
        raise DoclingRuntimeInitializationError("Docling 未安装，无法构造 DocumentStream") from exc

    return DocumentStream(name=stream_name, stream=BytesIO(raw_bytes))


def _merge_docling_dicts(
    chunk_dicts: list[dict],
    page_offsets: list[int],
) -> dict:
    """合并多个 Docling export_to_dict() 结果为一个完整文档。

    处理三件事：
    1. 页码偏移 — 后续 chunk 的 prov[].page_no 和 pages key 累加偏移量。
    2. self_ref / $ref 全局重编号 — texts/tables/groups/pictures 引用唯一化。
    3. 数组拼接 — texts/tables/groups/pictures 拼接，body.children 合并。

    Args:
        chunk_dicts: 各分片的 export_to_dict() 结果列表。
        page_offsets: 各分片的页码起始偏移量列表，长度与 chunk_dicts 一致。

    Returns:
        合并后的 Docling 文档字典。

    Raises:
        ValueError: 输入列表为空时抛出。
    """
    if not chunk_dicts:
        raise ValueError("chunk_dicts 不能为空")

    # 1. 计算各分类的累积长度，用于 ref 全局重编号。
    cum_text = 0
    cum_table = 0
    cum_group = 0
    cum_picture = 0
    text_counts: list[int] = []
    table_counts: list[int] = []
    group_counts: list[int] = []
    picture_counts: list[int] = []
    for chunk in chunk_dicts:
        tc = len(chunk.get("texts", []))
        tac = len(chunk.get("tables", []))
        gc = len(chunk.get("groups", []))
        pc = len(chunk.get("pictures", []))
        text_counts.append(tc)
        table_counts.append(tac)
        group_counts.append(gc)
        picture_counts.append(pc)

    # 2. 处理首个 chunk 作为基础。
    merged: dict = dict(chunk_dicts[0])
    for cat in ("texts", "tables", "groups", "pictures"):
        merged.setdefault(cat, [])
    merged.setdefault("body", {})
    merged["body"].setdefault("children", [])
    merged.setdefault("pages", {})

    cum_text += text_counts[0]
    cum_table += table_counts[0]
    cum_group += group_counts[0]
    cum_picture += picture_counts[0]

    # 3. 逐一合并后续 chunk。
    for i in range(1, len(chunk_dicts)):
        chunk = chunk_dicts[i]
        page_offset = page_offsets[i]

        # 构建当前 chunk 的 ref 映射。
        ref_map: dict[str, str] = {}
        for idx in range(text_counts[i]):
            old_ref = f"#/texts/{idx}"
            new_ref = f"#/texts/{cum_text + idx}"
            ref_map[old_ref] = new_ref
        for idx in range(table_counts[i]):
            old_ref = f"#/tables/{idx}"
            new_ref = f"#/tables/{cum_table + idx}"
            ref_map[old_ref] = new_ref
        for idx in range(group_counts[i]):
            old_ref = f"#/groups/{idx}"
            new_ref = f"#/groups/{cum_group + idx}"
            ref_map[old_ref] = new_ref
        for idx in range(picture_counts[i]):
            old_ref = f"#/pictures/{idx}"
            new_ref = f"#/pictures/{cum_picture + idx}"
            ref_map[old_ref] = new_ref

        # 应用 ref 映射与页码偏移到当前 chunk。
        _apply_ref_remap_and_page_offset(chunk, ref_map, page_offset)

        # 拼接数组。
        merged["texts"].extend(chunk.get("texts", []))
        merged["tables"].extend(chunk.get("tables", []))
        merged["groups"].extend(chunk.get("groups", []))
        merged["pictures"].extend(chunk.get("pictures", []))

        # 合并 body.children。
        chunk_body_children = chunk.get("body", {}).get("children", [])
        merged["body"]["children"].extend(chunk_body_children)

        # 合并 pages（偏移 key）。
        chunk_pages = chunk.get("pages", {})
        if isinstance(chunk_pages, dict):
            for page_key, page_val in chunk_pages.items():
                try:
                    new_key = str(int(page_key) + page_offset)
                except (ValueError, TypeError):
                    new_key = str(page_key)
                merged["pages"][new_key] = page_val

        cum_text += text_counts[i]
        cum_table += table_counts[i]
        cum_group += group_counts[i]
        cum_picture += picture_counts[i]

    return merged


def _apply_ref_remap_and_page_offset(
    chunk_dict: dict,
    ref_map: dict[str, str],
    page_offset: int,
) -> None:
    """原地应用 ref 映射与页码偏移到单个 chunk 的 dict。

    修改 chunk_dict 自身：
    - 各分类数组中 item 的 self_ref 按 ref_map 更新。
    - prov[].page_no 累加 page_offset。
    - item 内所有 {"$ref": ...} 值（parent、children、captions、footnotes 等）按 ref_map 递归更新。

    Args:
        chunk_dict: 单个 chunk 的 export_to_dict() 结果（原地修改）。
        ref_map: old_ref → new_ref 映射。
        page_offset: 页码偏移量。

    Returns:
        无。

    Raises:
        无。
    """
    # 更新各分类数组的 self_ref 和页码偏移。
    for cat in _CHUNK_CATEGORY_NAMES:
        items = chunk_dict.get(cat)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            old_ref = item.get("self_ref")
            if isinstance(old_ref, str) and old_ref in ref_map:
                item["self_ref"] = ref_map[old_ref]
            # 页码偏移
            if page_offset:
                prov = item.get("prov")
                if isinstance(prov, list):
                    for prov_entry in prov:
                        if isinstance(prov_entry, dict) and "page_no" in prov_entry:
                            try:
                                prov_entry["page_no"] = int(prov_entry["page_no"]) + page_offset
                            except (TypeError, ValueError):
                                pass
            # 递归 remap item 内所有 $ref（parent、children、captions、footnotes、references 等）
            _remap_nested_refs(item, ref_map)

    # 递归 remap body 内所有 $ref。
    body = chunk_dict.get("body")
    if isinstance(body, dict):
        _remap_nested_refs(body, ref_map)


def _remap_nested_refs(obj: object, ref_map: dict[str, str]) -> None:
    """递归遍历 obj 内所有 dict/list，将匹配的 {\"$ref\": ...} 值按 ref_map 更新。

    覆盖 item 内的 parent、children、captions、footnotes、references、annotations
    等所有含 $ref 的字段，不依赖枚举特定键名。

    Args:
        obj: 待遍历的 dict / list / 其他对象（原地修改）。
        ref_map: old_ref → new_ref 映射。

    Returns:
        无。

    Raises:
        无。
    """
    if isinstance(obj, dict):
        ref_value = obj.get("$ref")
        if isinstance(ref_value, str) and ref_value in ref_map:
            obj["$ref"] = ref_map[ref_value]
        for value in obj.values():
            _remap_nested_refs(value, ref_map)
    elif isinstance(obj, list):
        for item in obj:
            _remap_nested_refs(item, ref_map)


def _build_chunked_result(merged_dict: dict) -> _ChunkedConversionResult:
    """从合并后的 dict 重建 DoclingDocument 并包装为转换结果。

    优先使用 model_validate 直接构建；失败时回退到临时 JSON 文件加载。

    Args:
        merged_dict: 合并后的 Docling 文档字典。

    Returns:
        包装后的分片转换结果。

    Raises:
        RuntimeError: DoclingDocument 构建失败时抛出。
    """
    try:
        from docling_core.types.doc.document import DoclingDocument
    except ImportError as exc:  # pragma: no cover - 依赖缺失保护
        raise DoclingRuntimeInitializationError(
            "docling-core 未安装，无法重建 DoclingDocument"
        ) from exc

    try:
        doc = DoclingDocument.model_validate(merged_dict)
    except Exception:
        Log.debug(
            "model_validate \u5931\u8d25\uff0c\u56de\u9000\u5230\u4e34\u65f6\u6587\u4ef6\u52a0\u8f7d DoclingDocument",
            module=_MODULE,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(merged_dict, tmp, ensure_ascii=False)
            tmp_path = tmp.name
        try:
            doc = DoclingDocument.load_from_json(tmp_path)
        finally:
            os.unlink(tmp_path)

    return _ChunkedConversionResult(document=doc)



def convert_pdf_bytes_with_docling(
    raw_bytes: bytes,
    *,
    stream_name: str,
    do_ocr: bool = True,
    do_table_structure: bool = True,
    table_mode: str = _TABLE_MODE_ACCURATE,
    do_cell_matching: bool = True,
) -> _DoclingConversionResult:
    """以字节流形式调用 Docling，规避 Windows 非 ASCII 路径编码问题。

    Docling 在 Windows 上把 ``Path`` 输入按 mbcs（cp936）编码后传给
    docling-parse C++ 后端，导致合法文档被判 ``not valid``。本函数把字节流
    封装成 ``DocumentStream`` 直接喂给 Docling，绕开文件系统路径编码层。

    当 PDF 页数超过分片阈值（由环境变量 ``DAYU_DOCLING_CHUNK_SIZE`` 配置，
    默认 40 页）时，自动拆分为多个分片分别转换，再合并结果，避免大文件
    单次转换内存溢出。

    Args:
        raw_bytes: PDF 原始字节内容。
        stream_name: 流名称，建议直接传文件名以保留扩展名。
        do_ocr: 是否开启 OCR。
        do_table_structure: 是否开启表格结构识别。
        table_mode: 表格结构模式，仅支持 ``accurate`` 或 ``fast``。
        do_cell_matching: 是否开启表格单元格匹配。

    Returns:
        Docling 转换结果对象。

    Raises:
        DoclingRuntimeInitializationError: Docling 依赖缺失或装配失败时抛出。
        ValueError: ``table_mode`` 非法时抛出。
    """

    try:
        page_count = _get_pdf_page_count(raw_bytes)
    except Exception:
        page_count = 0

    chunk_size = resolve_docling_chunk_size()

    if page_count <= chunk_size:
        stream = _build_docling_document_stream(raw_bytes, stream_name=stream_name)
        return run_docling_pdf_conversion(
            lambda converter: converter.convert(stream),
            do_ocr=do_ocr,
            do_table_structure=do_table_structure,
            table_mode=table_mode,
            do_cell_matching=do_cell_matching,
        )

    Log.info(
        f"PDF 页数 {page_count} 超过阈值 {chunk_size}，"
        f"启用分片转换，流名: {stream_name}",
        module=_MODULE,
    )
    chunk_bytes_list = _split_pdf_bytes(raw_bytes, chunk_size)
    chunk_dicts: list[dict] = []
    page_offsets: list[int] = []

    for chunk_index, chunk_bytes in enumerate(chunk_bytes_list):
        chunk_stream = _build_docling_document_stream(
            chunk_bytes, stream_name=stream_name
        )
        chunk_result = run_docling_pdf_conversion(
            lambda converter: converter.convert(chunk_stream),
            do_ocr=do_ocr,
            do_table_structure=do_table_structure,
            table_mode=table_mode,
            do_cell_matching=do_cell_matching,
        )
        chunk_dict = chunk_result.document.export_to_dict()
        chunk_dicts.append(chunk_dict)
        page_offsets.append(chunk_index * chunk_size)
        Log.info(
            f"Docling 分片转换完成: chunk={chunk_index + 1}/{len(chunk_bytes_list)} "
            f"流名={stream_name}",
            module=_MODULE,
        )

    merged_dict = _merge_docling_dicts(chunk_dicts, page_offsets)
    return _build_chunked_result(merged_dict)
