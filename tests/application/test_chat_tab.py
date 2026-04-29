"""chat_tab 模块测试。"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from queue import Queue
from typing import cast

import pytest

from dayu.contracts.events import AppEvent, AppEventType
from dayu.services.contracts import SessionTurnExcerptView
from dayu.services.protocols import ChatServiceProtocol
from dayu.web.streamlit.pages import chat_tab as chat_tab_module
from dayu.web.streamlit.pages.chat import stream_runtime as stream_runtime_module
from dayu.web.streamlit.pages.chat.stream_runtime import (
    StreamQueueItem,
    _CHAT_STREAM_RUNTIME_HANDLES,
    _ChatStreamRuntimeHandle,
    _STREAM_FIRST_CHUNK_TIMEOUT_SECONDS,
    _STREAM_TIMEOUT_MESSAGE,
    _new_stream_frame_state,
    clear_chat_stream_runtime as _clear_chat_stream_runtime,
    poll_chat_stream_events as _poll_chat_stream_events,
    start_chat_stream_runtime as _start_chat_stream_runtime,
)
from dayu.web.streamlit.pages.chat.utils import (
    build_chat_session_id,
    build_request_trace_id,
    extract_stream_text,
    fold_app_events_to_assistant_text,
    normalize_stream_text_for_markdown,
    summarize_user_text,
)
from dayu.web.streamlit.pages.chat_tab import (
    _build_state_key,
)


class _FakeStreamlit:
    """最小化 Streamlit 替身，仅提供 session_state。"""

    def __init__(self) -> None:
        self.session_state: dict[str, object] = {}


class _FakeSubmission:
    """测试用提交结果。"""

    def __init__(self, *, session_id: str, events: list[AppEvent]) -> None:
        self.session_id = session_id
        self.event_stream = self._stream(events)

    async def _stream(self, events: list[AppEvent]) -> AsyncIterator[AppEvent]:
        for event in events:
            yield event


class _FakeChatService:
    """测试用聊天服务。"""

    def __init__(self, *, session_id: str, events: list[AppEvent]) -> None:
        self._session_id = session_id
        self._events = events
        self.cleared_sessions: list[str] = []

    async def submit_turn(self, _request: object) -> _FakeSubmission:
        return _FakeSubmission(session_id=self._session_id, events=self._events)

    def list_session_recent_turns(
        self, _session_id: str, *, limit: int = 100
    ) -> list[SessionTurnExcerptView]:
        return []

    def clear_session(self, session_id: str) -> None:
        self.cleared_sessions.append(session_id)


@pytest.fixture
def fake_st(monkeypatch: pytest.MonkeyPatch) -> _FakeStreamlit:
    """将 chat_tab 与 stream_runtime 内部的 st 替换为可控假对象。"""

    fake = _FakeStreamlit()
    monkeypatch.setattr(chat_tab_module, "st", fake)
    monkeypatch.setattr(stream_runtime_module, "st", fake)
    _CHAT_STREAM_RUNTIME_HANDLES.clear()
    return fake


def _build_finished_worker() -> threading.Thread:
    """构造已结束线程，便于执行 join。"""

    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join()
    return worker


# ── extract_stream_text ─────────────────────────────────────────────


@pytest.mark.unit
def test_extract_string_payload() -> None:
    """字符串负载应直接返回规范化文本。"""
    result = extract_stream_text("分析结果")
    assert result == "分析结果"


@pytest.mark.unit
def test_extract_empty_string_returns_empty() -> None:
    """空白字符串应返回空字符串。"""
    assert extract_stream_text("   ") == ""


@pytest.mark.unit
def test_extract_dict_payload_content_key() -> None:
    """字典负载应优先提取 content 字段。"""
    result = extract_stream_text({"content": "正文内容"})
    assert result == "正文内容"


@pytest.mark.unit
def test_extract_dict_payload_text_key() -> None:
    """字典负载 content 为空时，应提取 text 字段。"""
    result = extract_stream_text({"text": "文本内容"})
    assert result == "文本内容"


@pytest.mark.unit
def test_extract_dict_payload_answer_key() -> None:
    """字典负载 content/text 为空时，应提取 answer 字段。"""
    result = extract_stream_text({"answer": "回答内容"})
    assert result == "回答内容"


@pytest.mark.unit
def test_extract_dict_payload_priority_content_first() -> None:
    """多个字段同时存在时，优先取 content。"""
    result = extract_stream_text({"content": "A", "text": "B", "answer": "C"})
    assert result == "A"


@pytest.mark.unit
def test_extract_dict_payload_all_blank() -> None:
    """字典所有文本字段均为空时返回空字符串。"""
    result = extract_stream_text({"content": "  ", "text": ""})
    assert result == ""


@pytest.mark.unit
def test_extract_dict_payload_no_valid_keys() -> None:
    """字典不包含已知文本字段时返回空字符串。"""
    result = extract_stream_text({"unknown_key": "value"})
    assert result == ""


@pytest.mark.unit
def test_extract_escaped_newline_in_string() -> None:
    """字符串负载中的 \\n 应被规整为真实换行。"""
    result = extract_stream_text(r"行1\n行2")
    assert result == "行1\n行2"


# ── build_request_trace_id ──────────────────────────────────────────


@pytest.mark.unit
def test_trace_id_contains_ticker_prefix() -> None:
    """trace_id 应以股票代码前缀开头。"""
    trace_id = build_request_trace_id(ticker="AAPL", user_text="test")
    assert trace_id.startswith("AAPL-")


@pytest.mark.unit
def test_trace_id_truncates_long_ticker() -> None:
    """过长的股票代码应截断到前 8 字符。"""
    trace_id = build_request_trace_id(ticker="VERYLONGTICKER", user_text="q")
    assert trace_id.startswith("VERYLONG-")


@pytest.mark.unit
def test_trace_id_empty_ticker_uses_unknown() -> None:
    """空股票代码应使用 UNKNOWN 占位。"""
    trace_id = build_request_trace_id(ticker="", user_text="q")
    assert trace_id.startswith("UNKNOWN-")


@pytest.mark.unit
def test_trace_id_different_user_text_different_trace() -> None:
    """不同用户输入应产生不同的 trace_id。"""
    id1 = build_request_trace_id(ticker="AAPL", user_text="question A")
    id2 = build_request_trace_id(ticker="AAPL", user_text="question B")
    assert id1 != id2


@pytest.mark.unit
def test_trace_id_same_input_same_trace() -> None:
    """相同输入应产生一致的 trace_id（hash 确定性）。"""
    id1 = build_request_trace_id(ticker="AAPL", user_text="hello")
    id2 = build_request_trace_id(ticker="AAPL", user_text="hello")
    assert id1 == id2


# ── build_chat_session_id ────────────────────────────────────────────


@pytest.mark.unit
def test_session_id_simple_us_ticker() -> None:
    """纯美股 ticker 生成正确的 session_id。"""
    result = build_chat_session_id("AAPL")
    assert result == "dayu-streamlit-chat-AAPL"


@pytest.mark.unit
def test_session_id_us_ticker_with_suffix() -> None:
    """带 .US 后缀的美股应归一化到 canonical 形态。"""
    result = build_chat_session_id("AAPL.US")
    assert result == "dayu-streamlit-chat-AAPL"


@pytest.mark.unit
def test_session_id_hk_ticker_pad_zero() -> None:
    """港股短码应补齐前导零。"""
    result = build_chat_session_id("700")
    assert result == "dayu-streamlit-chat-0700"


@pytest.mark.unit
def test_session_id_hk_ticker_with_suffix() -> None:
    """带 .HK 后缀的港股应剥离后缀并补齐。"""
    result = build_chat_session_id("0700.HK")
    assert result == "dayu-streamlit-chat-0700"


@pytest.mark.unit
def test_session_id_us_ticker_with_prefix() -> None:
    """带 US. 前缀的美股应剥离前缀。"""
    result = build_chat_session_id("US.AAPL")
    assert result == "dayu-streamlit-chat-AAPL"


@pytest.mark.unit
def test_session_id_same_company_different_writings() -> None:
    """同一公司的不同 ticker 写法应映射到相同 session_id。"""
    id1 = build_chat_session_id("AAPL")
    id2 = build_chat_session_id("AAPL.US")
    assert id1 == id2 == "dayu-streamlit-chat-AAPL"


@pytest.mark.unit
def test_session_id_invalid_ticker_fallback() -> None:
    """归一化失败的非法 ticker 回退到 upper 形态。"""
    result = build_chat_session_id("@@@")
    assert result == "dayu-streamlit-chat-@@@"


@pytest.mark.unit
def test_session_id_empty_ticker_fallback() -> None:
    """空字符串回退到 upper（即空串）。"""
    result = build_chat_session_id("")
    assert result == "dayu-streamlit-chat-"


# ── summarize_user_text ─────────────────────────────────────────────


@pytest.mark.unit
def test_summarize_short_text() -> None:
    """短文本（<= 48 字符）不应截断。"""
    summary = summarize_user_text("你好")
    assert "preview='你好'" in summary
    assert "..." not in summary


@pytest.mark.unit
def test_summarize_long_text_truncates() -> None:
    """长文本（> 48 字符）应截断并追加省略号。"""
    long_text = "这是一个很长的用户输入问题，" + "包含了大量详细信息和分析要求以及更多补充内容。" * 3
    summary = summarize_user_text(long_text)
    assert "..." in summary


@pytest.mark.unit
def test_summarize_includes_length() -> None:
    """摘要应包含原始文本长度。"""
    summary = summarize_user_text("hello world")
    assert "len=11" in summary


@pytest.mark.unit
def test_summarize_collapses_whitespace() -> None:
    """多空白字符应被折叠成单个空格。"""
    summary = summarize_user_text("hello   world\t\ntest")
    normalized_preview = "hello world test"
    assert f"preview={normalized_preview!r}" in summary


# ── StreamQueueItem ─────────────────────────────────────────────────


@pytest.mark.unit
def test_stream_queue_item_defaults() -> None:
    """StreamQueueItem 默认 done=False, kind='content', chunk=''."""
    item = StreamQueueItem(done=True)
    assert item.done is True
    assert item.kind == "content"
    assert item.chunk == ""


@pytest.mark.unit
def test_stream_queue_item_reasoning_kind() -> None:
    """StreamQueueItem 可标记为 reasoning 类型。"""
    item = StreamQueueItem(done=False, kind="reasoning", chunk="思考中")
    assert item.kind == "reasoning"
    assert item.chunk == "思考中"
    assert item.done is False


@pytest.mark.unit
def test_stream_queue_item_frozen() -> None:
    """StreamQueueItem 应为不可变数据类。"""
    item = StreamQueueItem(done=True)
    with pytest.raises(Exception):
        item.done = False  # type: ignore[misc]


# ── normalize_stream_text_for_markdown ──────────────────────────────


@pytest.mark.unit
def test_normalize_empty_text_returns_empty() -> None:
    """空字符串输入应返回空字符串。"""
    assert normalize_stream_text_for_markdown("") == ""


@pytest.mark.unit
def test_normalize_plain_text_passthrough() -> None:
    """无 Markdown 语法的纯文本应原样返回。"""
    text = "这是一段普通的分析文本。"
    assert normalize_stream_text_for_markdown(text) == text


@pytest.mark.unit
def test_normalize_escaped_newlines() -> None:
    """\\n 字面量应转换为真实换行符。"""
    assert normalize_stream_text_for_markdown(r"第一行\n第二行") == "第一行\n第二行"


@pytest.mark.unit
def test_normalize_inline_heading_adds_newline() -> None:
    """内联标题标记（如文本后紧跟 ### 且无空格）前应插入换行。"""
    result = normalize_stream_text_for_markdown("文本内容###标题")
    assert result == "文本内容\n### 标题"


@pytest.mark.unit
def test_normalize_heading_missing_space() -> None:
    """标题标记后缺少空格时应补空格。"""
    result = normalize_stream_text_for_markdown("##标题")
    assert result == "## 标题"


@pytest.mark.unit
def test_normalize_inline_star_list() -> None:
    """行内出现的无序列表标记前应插入换行。"""
    result = normalize_stream_text_for_markdown("文本* 列表项")
    assert result == "文本\n* 列表项"


@pytest.mark.unit
def test_normalize_code_fence_preserved() -> None:
    """代码块内的内容应保持原样，不触发外部规整规则。"""
    text = "介绍\n```python\nprint('hello')\n```\n结尾"
    result = normalize_stream_text_for_markdown(text)
    assert "```python" in result
    assert "print('hello')" in result
    assert "结尾" in result


@pytest.mark.unit
def test_normalize_escaped_newlines_inside_code_fence() -> None:
    """代码块内的 \\n 应被替换为真实换行。"""
    text = "```python\\nprint('hello')\\n```"
    result = normalize_stream_text_for_markdown(text)
    assert result == "```python\nprint('hello')\n```"


@pytest.mark.unit
def test_normalize_inline_table_rows_not_in_code_fence() -> None:
    """非代码块内的表格内联换行应正确处理。"""
    text = "col1 |---|---|---| | data"
    result = normalize_stream_text_for_markdown(text)
    assert "|---|---|\n|" in result


# ── fold_app_events_to_assistant_text ───────────────────────────────


def _make_event(event_type: AppEventType, payload: str | dict) -> AppEvent:
    """构造测试用 AppEvent。"""
    return AppEvent(type=event_type, payload=payload)


@pytest.mark.unit
def test_fold_empty_events_returns_empty() -> None:
    """空事件列表应返回空文本、空副作用消息、过滤标记为 False。"""
    text, sides, filtered = fold_app_events_to_assistant_text([])
    assert text == ""
    assert sides == []
    assert filtered is False


@pytest.mark.unit
def test_fold_content_delta_accumulates_text() -> None:
    """CONTENT_DELTA 事件的文本应累积拼接。"""
    events = [
        _make_event(AppEventType.CONTENT_DELTA, "你好"),
        _make_event(AppEventType.CONTENT_DELTA, "，世界"),
    ]
    text, sides, filtered = fold_app_events_to_assistant_text(events)
    assert text == "你好，世界"
    assert sides == []
    assert filtered is False


@pytest.mark.unit
def test_fold_reasoning_delta_accumulates_text() -> None:
    """REASONING_DELTA 事件的文本也应累积到主文本。"""
    events = [
        _make_event(AppEventType.REASONING_DELTA, "思考1"),
        _make_event(AppEventType.REASONING_DELTA, "思考2"),
    ]
    text, _sides, _filtered = fold_app_events_to_assistant_text(events)
    assert text == "思考1思考2"


@pytest.mark.unit
def test_fold_content_delta_dict_payload() -> None:
    """CONTENT_DELTA 使用字典负载时应提取 content 字段。"""
    events = [
        _make_event(AppEventType.CONTENT_DELTA, {"content": "分析结果"}),
    ]
    text, _sides, _filtered = fold_app_events_to_assistant_text(events)
    assert text == "分析结果"


@pytest.mark.unit
def test_fold_content_delta_skips_empty() -> None:
    """空字符串或纯空白负载不应追加到文本。"""
    events = [
        _make_event(AppEventType.CONTENT_DELTA, "   "),
        _make_event(AppEventType.CONTENT_DELTA, "有效内容"),
    ]
    text, _sides, _filtered = fold_app_events_to_assistant_text(events)
    assert text == "有效内容"


@pytest.mark.unit
def test_fold_warning_events_collected() -> None:
    """WARNING 事件应收集到 side_messages。"""
    events = [
        _make_event(AppEventType.WARNING, {"message": "过滤警告"}),
    ]
    text, sides, _filtered = fold_app_events_to_assistant_text(events)
    assert text == ""
    assert sides == ["过滤警告"]


@pytest.mark.unit
def test_fold_error_events_collected() -> None:
    """ERROR 事件应收集到 side_messages。"""
    events = [
        _make_event(AppEventType.ERROR, "服务端错误"),
    ]
    _text, sides, _filtered = fold_app_events_to_assistant_text(events)
    assert "服务端错误" in sides


@pytest.mark.unit
def test_fold_cancelled_event_default_message() -> None:
    """CANCELLED 事件无原因时应使用默认消息。"""
    events = [
        _make_event(AppEventType.CANCELLED, {}),
    ]
    _text, sides, _filtered = fold_app_events_to_assistant_text(events)
    assert "执行已取消" in sides


@pytest.mark.unit
def test_fold_cancelled_event_with_reason() -> None:
    """CANCELLED 事件带原因时应包含原因信息。"""
    events = [
        _make_event(AppEventType.CANCELLED, {"cancel_reason": "超时"}),
    ]
    _text, sides, _filtered = fold_app_events_to_assistant_text(events)
    assert any("超时" in msg for msg in sides)


@pytest.mark.unit
def test_fold_final_answer_with_filtered_flag() -> None:
    """FINAL_ANSWER 事件携带 filtered 标记时应返回 True。"""
    events = [
        _make_event(AppEventType.FINAL_ANSWER, {"filtered": True, "content": "结果"}),
    ]
    text, _sides, filtered = fold_app_events_to_assistant_text(events)
    assert text == "结果"
    assert filtered is True


@pytest.mark.unit
def test_fold_final_answer_fills_text_when_no_deltas() -> None:
    """无增量事件时 FINAL_ANSWER 的文本应作为主文本。"""
    events = [
        _make_event(AppEventType.FINAL_ANSWER, "整段回答"),
    ]
    text, _sides, _filtered = fold_app_events_to_assistant_text(events)
    assert text == "整段回答"


@pytest.mark.unit
def test_fold_final_answer_dict_payload_fills_text() -> None:
    """FINAL_ANSWER 使用字典负载且无增量时应提取文本。"""
    events = [
        _make_event(AppEventType.FINAL_ANSWER, {"content": "完整回复", "filtered": False}),
    ]
    text, _sides, filtered = fold_app_events_to_assistant_text(events)
    assert text == "完整回复"
    assert filtered is False


@pytest.mark.unit
def test_fold_mixed_events() -> None:
    """混合事件应正确分类：内容=主文、warning/error=侧边、cancelled=侧边。"""
    events = [
        _make_event(AppEventType.CONTENT_DELTA, "第一部分"),
        _make_event(AppEventType.WARNING, {"message": "警告信息"}),
        _make_event(AppEventType.CONTENT_DELTA, "第二部分"),
        _make_event(AppEventType.ERROR, {"error": "错误详情"}),
        _make_event(AppEventType.CANCELLED, {"cancel_reason": "手动取消"}),
    ]
    text, sides, filtered = fold_app_events_to_assistant_text(events)
    assert text == "第一部分第二部分"
    assert any("警告信息" in msg for msg in sides)
    assert any("错误详情" in msg for msg in sides)
    assert any("执行已取消" in msg for msg in sides)
    assert filtered is False


@pytest.mark.unit
def test_fold_non_str_dict_payload_handled() -> None:
    """非字符串、非字典负载不应导致异常。"""
    events = [
        _make_event(AppEventType.WARNING, 123),  # type: ignore[arg-type]
    ]
    _text, sides, _filtered = fold_app_events_to_assistant_text(events)
    assert isinstance(sides, list)


@pytest.mark.unit
def test_fold_empty_dict_payload_warning_produces_str_representation() -> None:
    """WARNING 事件的空字典负载应转换为字符串表示。"""
    events = [
        _make_event(AppEventType.WARNING, {}),
    ]
    _text, sides, _filtered = fold_app_events_to_assistant_text(events)
    assert len(sides) == 1
    assert sides[0] != ""


# ── stream runtime polling ────────────────────────────────────────────


@pytest.mark.unit
def test_start_chat_stream_runtime_initializes_session_state(fake_st: _FakeStreamlit) -> None:
    """启动流式 runtime 后应写入会话状态并注册句柄。"""

    ticker = "AAPL"
    stream_state_key = _build_state_key(ticker, "stream_state")
    chat_service = _FakeChatService(
        session_id="session-new",
        events=[_make_event(AppEventType.CONTENT_DELTA, "你好")],
    )
    _start_chat_stream_runtime(
        chat_service=cast(ChatServiceProtocol, chat_service),
        ticker=ticker,
        user_text="问题",
        session_id="session-initial",
        trace_id="trace-1",
        stream_state_key=stream_state_key,
    )
    raw_state = fake_st.session_state.get(stream_state_key)
    assert raw_state is not None
    assert ticker in _CHAT_STREAM_RUNTIME_HANDLES
    state = _poll_chat_stream_events(ticker=ticker, stream_state_key=stream_state_key)
    assert state is not None
    assert state.trace_id == "trace-1"
    _clear_chat_stream_runtime(ticker=ticker, stream_state_key=stream_state_key)


@pytest.mark.unit
def test_poll_chat_stream_events_consumes_chunks_and_done(fake_st: _FakeStreamlit) -> None:
    """轮询应累计 reasoning/content 文本并在 done 后结束。"""

    ticker = "MSFT"
    stream_state_key = _build_state_key(ticker, "stream_state")
    fake_st.session_state[stream_state_key] = _new_stream_frame_state(trace_id="trace-2", session_id="session-x")

    event_queue: Queue[StreamQueueItem] = Queue()
    event_queue.put(StreamQueueItem(done=False, event_type="chunk", kind="reasoning", chunk="思考"))
    event_queue.put(StreamQueueItem(done=False, event_type="chunk", kind="content", chunk="结论"))
    event_queue.put(StreamQueueItem(done=False, event_type="side_message", chunk="提示"))
    event_queue.put(StreamQueueItem(done=False, event_type="filtered", flag=True))
    event_queue.put(StreamQueueItem(done=True, event_type="done"))

    _CHAT_STREAM_RUNTIME_HANDLES[ticker] = _ChatStreamRuntimeHandle(
        worker=_build_finished_worker(),
        event_queue=event_queue,
        cancel_event=threading.Event(),
        started_at=time.perf_counter(),
        last_chunk_at=time.perf_counter(),
        has_received_chunk=False,
    )

    state = _poll_chat_stream_events(ticker=ticker, stream_state_key=stream_state_key)
    assert state is not None
    assert state.reasoning_text == "思考"
    assert state.answer_text == "结论"
    assert state.side_messages == ["提示"]
    assert state.filtered_flags == [True]
    assert state.done is True


@pytest.mark.unit
def test_poll_chat_stream_events_sets_error_on_error_event(fake_st: _FakeStreamlit) -> None:
    """收到 error 事件时应标记失败并记录错误文案。"""

    ticker = "NVDA"
    stream_state_key = _build_state_key(ticker, "stream_state")
    fake_st.session_state[stream_state_key] = _new_stream_frame_state(trace_id="trace-3", session_id="session-y")

    event_queue: Queue[StreamQueueItem] = Queue()
    event_queue.put(StreamQueueItem(done=False, event_type="error", chunk="boom"))

    _CHAT_STREAM_RUNTIME_HANDLES[ticker] = _ChatStreamRuntimeHandle(
        worker=_build_finished_worker(),
        event_queue=event_queue,
        cancel_event=threading.Event(),
        started_at=time.perf_counter(),
        last_chunk_at=time.perf_counter(),
        has_received_chunk=False,
    )

    state = _poll_chat_stream_events(ticker=ticker, stream_state_key=stream_state_key)
    assert state is not None
    assert state.done is True
    assert state.error_message == "boom"


@pytest.mark.unit
def test_poll_chat_stream_events_marks_timeout_when_first_chunk_missing(fake_st: _FakeStreamlit) -> None:
    """首包超时后应写入统一超时错误并触发取消信号。"""

    ticker = "TSLA"
    stream_state_key = _build_state_key(ticker, "stream_state")
    fake_st.session_state[stream_state_key] = _new_stream_frame_state(trace_id="trace-4", session_id="session-z")

    event_queue: Queue[StreamQueueItem] = Queue()
    cancel_event = threading.Event()
    worker = threading.Thread(target=lambda: time.sleep(0.3))
    worker.start()
    _CHAT_STREAM_RUNTIME_HANDLES[ticker] = _ChatStreamRuntimeHandle(
        worker=worker,
        event_queue=event_queue,
        cancel_event=cancel_event,
        started_at=time.perf_counter() - (_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS + 1.0),
        last_chunk_at=time.perf_counter() - (_STREAM_FIRST_CHUNK_TIMEOUT_SECONDS + 1.0),
        has_received_chunk=False,
    )

    state = _poll_chat_stream_events(ticker=ticker, stream_state_key=stream_state_key)
    assert state is not None
    assert state.done is True
    assert state.error_message == _STREAM_TIMEOUT_MESSAGE
    assert cancel_event.is_set() is True
    worker.join(timeout=1.0)


# ── clear_session ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_fake_chat_service_clear_session_records_call() -> None:
    """_FakeChatService.clear_session 应记录被清空的 session_id。"""

    service = _FakeChatService(
        session_id="session-x",
        events=[_make_event(AppEventType.CONTENT_DELTA, "你好")],
    )
    assert service.cleared_sessions == []
    service.clear_session("session-x")
    assert service.cleared_sessions == ["session-x"]
    service.clear_session("session-y")
    assert service.cleared_sessions == ["session-x", "session-y"]
