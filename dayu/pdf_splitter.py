"""PDF 拆分工具模块。

提供 PDF 页数统计与按页分段拆分为独立 PDF 字节流的能力，用于大 PDF 分批交给
Docling 解析，避免单次转换内存溢出。
"""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader, PdfWriter


def get_pdf_page_count(raw_bytes: bytes) -> int:
    """统计 PDF 字节流的页数。

    Args:
        raw_bytes: PDF 原始字节内容。

    Returns:
        PDF 总页数。

    Raises:
        ValueError: 当字节流不是合法 PDF 时抛出。
    """

    reader = PdfReader(BytesIO(raw_bytes))
    return len(reader.pages)


def split_pdf_bytes(raw_bytes: bytes, pages_per_chunk: int) -> list[bytes]:
    """将 PDF 字节流按指定页数拆分为多个独立 PDF 字节流。

    Args:
        raw_bytes: PDF 原始字节内容。
        pages_per_chunk: 每个分片包含的最大页数，必须为正整数。

    Returns:
        各分片的 PDF 字节流列表；若总页数不超过 pages_per_chunk，
        返回包含原始字节的单元素列表。

    Raises:
        ValueError: 当 pages_per_chunk 非正数或字节流不是合法 PDF 时抛出。
    """

    if pages_per_chunk <= 0:
        raise ValueError(f"pages_per_chunk 必须为正整数，实际值: {pages_per_chunk}")

    reader = PdfReader(BytesIO(raw_bytes))
    total_pages = len(reader.pages)

    if total_pages <= pages_per_chunk:
        return [raw_bytes]

    chunks: list[bytes] = []
    for start in range(0, total_pages, pages_per_chunk):
        writer = PdfWriter()
        end = min(start + pages_per_chunk, total_pages)
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        buf = BytesIO()
        writer.write(buf)
        chunks.append(buf.getvalue())

    return chunks
