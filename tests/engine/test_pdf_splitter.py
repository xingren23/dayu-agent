"""PDF 拆分工具测试。"""

from __future__ import annotations

from io import BytesIO

import pytest
from pypdf import PdfWriter

from dayu.pdf_splitter import get_pdf_page_count, split_pdf_bytes

pytestmark = pytest.mark.unit


def _build_test_pdf(page_count: int) -> bytes:
    """构建指定页数的测试 PDF 字节流。

    Args:
        page_count: 目标页数，必须 >= 1。

    Returns:
        生成的 PDF 字节流。

    Raises:
        ValueError: page_count 非法时抛出。
    """
    if page_count < 1:
        raise ValueError(f"page_count 必须 >= 1，实际值: {page_count}")
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestGetPdfPageCount:
    """get_pdf_page_count 单元测试。"""

    def test_single_page(self) -> None:
        """验证单页 PDF 返回 1。"""
        pdf_bytes = _build_test_pdf(1)
        assert get_pdf_page_count(pdf_bytes) == 1

    def test_multiple_pages(self) -> None:
        """验证多页 PDF 返回正确页数。"""
        pdf_bytes = _build_test_pdf(3)
        assert get_pdf_page_count(pdf_bytes) == 3

    def test_large_pdf(self) -> None:
        """验证大 PDF（120 页）返回正确页数。"""
        pdf_bytes = _build_test_pdf(120)
        assert get_pdf_page_count(pdf_bytes) == 120

    def test_empty_bytes_raises(self) -> None:
        """验证空字节流抛出异常。"""
        with pytest.raises(Exception):
            get_pdf_page_count(b"")


class TestSplitPdfBytes:
    """split_pdf_bytes 单元测试。"""

    def test_no_split_when_within_limit(self) -> None:
        """验证总页数不超过分片大小时返回原始字节。"""
        pdf_bytes = _build_test_pdf(3)
        result = split_pdf_bytes(pdf_bytes, pages_per_chunk=10)
        assert len(result) == 1
        assert result[0] == pdf_bytes

    def test_exact_chunk_size(self) -> None:
        """验证恰好等于分片大小时返回原始字节。"""
        pdf_bytes = _build_test_pdf(5)
        result = split_pdf_bytes(pdf_bytes, pages_per_chunk=5)
        assert len(result) == 1
        assert result[0] == pdf_bytes

    def test_splits_into_chunks(self) -> None:
        """验证超过阈值时正确拆分为多个分片。"""
        pdf_bytes = _build_test_pdf(10)
        result = split_pdf_bytes(pdf_bytes, pages_per_chunk=3)
        assert len(result) == 4
        # 前 3 个分片各 3 页，最后 1 个分片 1 页
        for i in range(3):
            assert get_pdf_page_count(result[i]) == 3
        assert get_pdf_page_count(result[3]) == 1

    def test_splits_two_chunks(self) -> None:
        """验证 5 页按 3 页分片拆为两个分片。"""
        pdf_bytes = _build_test_pdf(5)
        result = split_pdf_bytes(pdf_bytes, pages_per_chunk=3)
        assert len(result) == 2
        assert get_pdf_page_count(result[0]) == 3
        assert get_pdf_page_count(result[1]) == 2

    def test_zero_pages_per_chunk_raises(self) -> None:
        """验证 pages_per_chunk=0 时抛出 ValueError。"""
        pdf_bytes = _build_test_pdf(5)
        with pytest.raises(ValueError, match="pages_per_chunk"):
            split_pdf_bytes(pdf_bytes, pages_per_chunk=0)

    def test_negative_pages_per_chunk_raises(self) -> None:
        """验证 pages_per_chunk 为负数时抛出 ValueError。"""
        pdf_bytes = _build_test_pdf(5)
        with pytest.raises(ValueError, match="pages_per_chunk"):
            split_pdf_bytes(pdf_bytes, pages_per_chunk=-1)

    def test_chunks_are_valid_pdfs(self) -> None:
        """验证拆分后的各分片均为合法 PDF。"""
        pdf_bytes = _build_test_pdf(10)
        result = split_pdf_bytes(pdf_bytes, pages_per_chunk=4)
        for i, chunk in enumerate(result):
            page_count = get_pdf_page_count(chunk)
            assert page_count > 0, f"chunk {i} 页数为 0"
            assert page_count <= 4, f"chunk {i} 页数 {page_count} 超过上限 4"
