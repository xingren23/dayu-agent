"""财报读取工具的 MCP Schema 定义与调度函数。

职责：
- 定义 9 个财报读取工具的 MCP Tool schema。
- 提供 `dispatch_tool_call()` 将调用转发到 `FinsToolService`。
"""

from __future__ import annotations

from typing import Any, cast

from mcp.types import Tool

from dayu.engine.exceptions import ToolArgumentError
from dayu.engine.tool_errors import ToolBusinessError
from dayu.fins.tools.service import FinsToolService

# ---------------------------------------------------------------------------
# 9 个工具定义（MCP Tool schema）
# ---------------------------------------------------------------------------

_LIST_DOCUMENTS = {
    "name": "list_documents",
    "description": (
        "List available financial documents (annual reports, quarterly reports, etc.) "
        "for a given ticker symbol. Use this tool FIRST to discover what documents are "
        "available before reading specific sections. Returns a list of documents with "
        "metadata including document_id, filing date, fiscal year/period, and document type. "
        "Use the returned document_id for all subsequent document-reading tools."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol (e.g. 'AAPL', 'NVDA', '0700' for HK, '600519' for CN).",
            },
            "document_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional filter by document types. Available values: "
                    "'annual_report', 'quarterly_report', 'current_report', "
                    "'proxy_statement', 'shareholder_report', 'material', 'other'."
                ),
            },
            "fiscal_years": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Optional filter by fiscal years (e.g. [2024, 2025]).",
            },
            "fiscal_periods": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional filter by fiscal periods (e.g. ['FY', 'Q1', 'Q2', 'Q3', 'Q4']).",
            },
        },
        "required": ["ticker"],
    },
}

_GET_DOCUMENT_SECTIONS = {
    "name": "get_document_sections",
    "description": (
        "Get the complete section/heading structure of a financial document. "
        "Returns all sections with their unique 'ref' identifiers, titles, hierarchy levels, "
        "and page ranges. Each section's 'ref' MUST be used verbatim in subsequent calls "
        "to 'read_section' or 'search_document' — do NOT abbreviate, renumber, or invent refs."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol.",
            },
            "document_id": {
                "type": "string",
                "description": "Document ID returned from list_documents (e.g. 'fil_0001045810-25-000116').",
            },
        },
        "required": ["ticker", "document_id"],
    },
}

_READ_SECTION = {
    "name": "read_section",
    "description": (
        "Read the full text content of a specific section within a financial document. "
        "The 'ref' parameter MUST be copied exactly as returned from get_document_sections "
        "or search_document results. Do NOT abbreviate, renumber, or invent section references. "
        "Returns the complete section text, its title, item number, topic, and child sections."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol.",
            },
            "document_id": {
                "type": "string",
                "description": "Document ID.",
            },
            "ref": {
                "type": "string",
                "description": (
                    "Section reference string, copied exactly from get_document_sections "
                    "or search_document results (e.g. 's_12', 'part2_item7')."
                ),
            },
        },
        "required": ["ticker", "document_id", "ref"],
    },
}

_SEARCH_DOCUMENT = {
    "name": "search_document",
    "description": (
        "Search for keywords or phrases within a financial document. "
        "Supports exact matching, keyword splitting, and semantic expansion modes. "
        "Either 'query' (single search) or 'queries' (batch search, up to 20 items) must be provided. "
        "Use 'within_section_ref' to constrain search to a specific section. "
        "Returns ranked matches with surrounding context and recommended next section to read. "
        "Mode values: 'auto' (default, exact first then expand), 'exact' (exact phrase only), "
        "'keyword' (keyword token split), 'semantic' (phrase variants + synonyms + keywords)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol.",
            },
            "document_id": {
                "type": "string",
                "description": "Document ID.",
            },
            "query": {
                "type": "string",
                "description": "Single search query. Mutually exclusive with 'queries'.",
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
                "description": "Batch search queries (up to 20). Mutually exclusive with 'query'.",
            },
            "within_section_ref": {
                "type": "string",
                "description": "Optional section ref to constrain search scope.",
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "exact", "keyword", "semantic"],
                "description": "Search mode. Default is 'auto'.",
            },
        },
        "required": ["ticker", "document_id"],
    },
}

_LIST_TABLES = {
    "name": "list_tables",
    "description": (
        "List tables within a financial document with metadata (headers, row/col counts, "
        "financial/non-financial classification, captions). "
        "If 'financial_only' is set to true, only returns tables classified as financial statements. "
        "Use 'within_section_ref' to constrain results to a specific section. "
        "Each returned table has a 'table_ref' that MUST be used verbatim in 'get_table' calls."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol.",
            },
            "document_id": {
                "type": "string",
                "description": "Document ID.",
            },
            "financial_only": {
                "type": "boolean",
                "description": "If true, only returns financial statement tables.",
            },
            "within_section_ref": {
                "type": "string",
                "description": "Optional section ref to constrain table listing scope.",
            },
        },
        "required": ["ticker", "document_id"],
    },
}

_GET_TABLE = {
    "name": "get_table",
    "description": (
        "Read the full data of a specific table within a financial document. "
        "The 'table_ref' parameter MUST be copied exactly as returned from list_tables results. "
        "Do NOT abbreviate, renumber, or invent table references. "
        "Returns complete table data including all rows, columns, headers, and cell values."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol.",
            },
            "document_id": {
                "type": "string",
                "description": "Document ID.",
            },
            "table_ref": {
                "type": "string",
                "description": (
                    "Table reference string, copied exactly from list_tables results "
                    "(e.g. 't_1', 't_fin_3')."
                ),
            },
        },
        "required": ["ticker", "document_id", "table_ref"],
    },
}

_GET_PAGE_CONTENT = {
    "name": "get_page_content",
    "description": (
        "Get a summary of the content on a specific page (1-based) of a financial document. "
        "Returns sections and tables appearing on that page, along with a text preview. "
        "Useful for quick page-level orientation. "
        "Note: not all document formats support page-level access; check the 'supported' field in the response."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol.",
            },
            "document_id": {
                "type": "string",
                "description": "Document ID.",
            },
            "page_no": {
                "type": "integer",
                "minimum": 1,
                "description": "Page number (1-based).",
            },
        },
        "required": ["ticker", "document_id", "page_no"],
    },
}

_GET_FINANCIAL_STATEMENT = {
    "name": "get_financial_statement",
    "description": (
        "Read a standard financial statement from a document. "
        "Supported statement_type values: 'income' (income statement), "
        "'balance_sheet' (balance sheet), 'cash_flow' (cash flow statement), "
        "'equity' (statement of equity), 'comprehensive_income' (comprehensive income). "
        "Returns rows of financial data with period columns, units, and currency. "
        "Some document formats may not support this extraction; check the 'supported' field in the response."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol.",
            },
            "document_id": {
                "type": "string",
                "description": "Document ID.",
            },
            "statement_type": {
                "type": "string",
                "enum": ["income", "balance_sheet", "cash_flow", "equity", "comprehensive_income"],
                "description": "Type of financial statement to retrieve.",
            },
        },
        "required": ["ticker", "document_id", "statement_type"],
    },
}

_QUERY_XBRL_FACTS = {
    "name": "query_xbrl_facts",
    "description": (
        "Query structured XBRL numerical facts from a financial document. "
        "Filter by XBRL concepts (e.g. ['Revenue', 'NetIncome']), statement type, "
        "fiscal year/period, date range, and numeric value range. "
        "If 'concepts' is empty or omitted, uses default concept sets based on the "
        "document type. Returns individual XBRL facts with values, units, periods, and dimensions. "
        "Some documents may not have XBRL data; check the 'supported' field in the response."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Stock ticker symbol.",
            },
            "document_id": {
                "type": "string",
                "description": "Document ID.",
            },
            "concepts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "XBRL concept names to query (e.g. ['Revenue', 'NetIncomeLoss']). If empty, uses default concept set.",
            },
            "statement_type": {
                "type": "string",
                "enum": ["income", "balance_sheet", "cash_flow", "equity", "comprehensive_income"],
                "description": "Optional statement type to constrain the query.",
            },
            "period_end": {
                "type": "string",
                "description": "Optional period end date filter (e.g. '2025-01-26').",
            },
            "fiscal_year": {
                "type": "integer",
                "description": "Optional fiscal year filter.",
            },
            "fiscal_period": {
                "type": "string",
                "description": "Optional fiscal period filter (e.g. 'Q1', 'FY').",
            },
            "min_value": {
                "type": "number",
                "description": "Optional minimum fact value filter.",
            },
            "max_value": {
                "type": "number",
                "description": "Optional maximum fact value filter.",
            },
        },
        "required": ["ticker", "document_id"],
    },
}

# 全部 9 个工具定义的列表
_FINS_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _LIST_DOCUMENTS,
    _GET_DOCUMENT_SECTIONS,
    _READ_SECTION,
    _SEARCH_DOCUMENT,
    _LIST_TABLES,
    _GET_TABLE,
    _GET_PAGE_CONTENT,
    _GET_FINANCIAL_STATEMENT,
    _QUERY_XBRL_FACTS,
]


def build_mcp_tools() -> list[Tool]:
    """构建 9 个财报读取工具的 MCP Tool 列表。

    Returns:
        MCP Tool 对象列表。
    """
    return [
        Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in _FINS_TOOL_DEFINITIONS
    ]


def _build_error(code: str, message: str) -> dict[str, Any]:
    """构建结构化错误返回。

    Args:
        code: 错误码。
        message: 错误消息。

    Returns:
        结构化错误字典。
    """
    return {"error": True, "code": code, "message": message}


def dispatch_tool_call(
    service: FinsToolService,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """将 MCP 工具调用转发到 FinsToolService。

    Args:
        service: FinsToolService 实例，必须在调用前已完成初始化。
        tool_name: 工具名称。
        arguments: 调用参数字典。

    Returns:
        工具执行结果字典；出错时返回包含 ``error`` 字段的结构化错误。
    """
    try:
        if tool_name == "list_documents":
            return _dispatch_list_documents(service, arguments)
        elif tool_name == "get_document_sections":
            return _dispatch_get_document_sections(service, arguments)
        elif tool_name == "read_section":
            return _dispatch_read_section(service, arguments)
        elif tool_name == "search_document":
            return _dispatch_search_document(service, arguments)
        elif tool_name == "list_tables":
            return _dispatch_list_tables(service, arguments)
        elif tool_name == "get_table":
            return _dispatch_get_table(service, arguments)
        elif tool_name == "get_page_content":
            return _dispatch_get_page_content(service, arguments)
        elif tool_name == "get_financial_statement":
            return _dispatch_get_financial_statement(service, arguments)
        elif tool_name == "query_xbrl_facts":
            return _dispatch_query_xbrl_facts(service, arguments)
        else:
            return _build_error("UNKNOWN_TOOL", f"未知工具: {tool_name}")
    except ToolArgumentError as exc:
        error_info = {"error": True, "code": "TOOL_ARGUMENT_ERROR", "message": str(exc)}
        hint = getattr(exc, "hint", None)
        if hint:
            error_info["hint"] = str(hint)
        return error_info
    except ToolBusinessError as exc:
        return _build_error("TOOL_BUSINESS_ERROR", str(exc))
    except FileNotFoundError as exc:
        return _build_error("NOT_FOUND", str(exc))
    except KeyError as exc:
        return _build_error("NOT_FOUND", str(exc))
    except Exception as exc:
        return _build_error("INTERNAL_ERROR", str(exc))


# ---------------------------------------------------------------------------
# 各工具私有调度函数
# ---------------------------------------------------------------------------


def _dispatch_list_documents(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 list_documents。"""
    return cast(dict[str, Any], service.list_documents(
        ticker=args["ticker"],
        document_types=args.get("document_types"),
        fiscal_years=args.get("fiscal_years"),
        fiscal_periods=args.get("fiscal_periods"),
    ))


def _dispatch_get_document_sections(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 get_document_sections。"""
    return cast(dict[str, Any], service.get_document_sections(
        ticker=args["ticker"],
        document_id=args["document_id"],
    ))


def _dispatch_read_section(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 read_section。"""
    return cast(dict[str, Any], service.read_section(
        ticker=args["ticker"],
        document_id=args["document_id"],
        ref=args["ref"],
    ))


def _dispatch_search_document(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 search_document。"""
    return cast(dict[str, Any], service.search_document(
        ticker=args["ticker"],
        document_id=args["document_id"],
        query=args.get("query"),
        queries=args.get("queries"),
        within_section_ref=args.get("within_section_ref"),
        mode=args.get("mode"),
    ))


def _dispatch_list_tables(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 list_tables。"""
    return cast(dict[str, Any], service.list_tables(
        ticker=args["ticker"],
        document_id=args["document_id"],
        financial_only=args.get("financial_only", False),
        within_section_ref=args.get("within_section_ref"),
    ))


def _dispatch_get_table(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 get_table。"""
    return cast(dict[str, Any], service.get_table(
        ticker=args["ticker"],
        document_id=args["document_id"],
        table_ref=args["table_ref"],
    ))


def _dispatch_get_page_content(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 get_page_content。"""
    return cast(dict[str, Any], service.get_page_content(
        ticker=args["ticker"],
        document_id=args["document_id"],
        page_no=args["page_no"],
    ))


def _dispatch_get_financial_statement(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 get_financial_statement。"""
    return cast(dict[str, Any], service.get_financial_statement(
        ticker=args["ticker"],
        document_id=args["document_id"],
        statement_type=args["statement_type"],
    ))


def _dispatch_query_xbrl_facts(
    service: FinsToolService,
    args: dict[str, Any],
) -> dict[str, Any]:
    """分派 query_xbrl_facts。"""
    return cast(dict[str, Any], service.query_xbrl_facts(
        ticker=args["ticker"],
        document_id=args["document_id"],
        concepts=args.get("concepts"),
        statement_type=args.get("statement_type"),
        period_end=args.get("period_end"),
        fiscal_year=args.get("fiscal_year"),
        fiscal_period=args.get("fiscal_period"),
        min_value=args.get("min_value"),
        max_value=args.get("max_value"),
    ))
