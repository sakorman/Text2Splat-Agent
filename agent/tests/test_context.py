from copy import deepcopy
import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from text2splat.context import prepare_context, exchanges, size
from text2splat.tools import ToolFailure


def history(count=8, padding=500):
    messages = [HumanMessage('用户原始要求：青瓷花瓶，保留卷草纹。')]
    for i in range(count):
        name = "generate_reference" if i == 0 else "inspect_task"
        messages += [AIMessage(content="", tool_calls=[{"id": str(i), "name": name, "args": {}}]),
                     ToolMessage(tool_call_id=str(i), content=json.dumps({"ok": i != 1,
                         "error": "不能重复生图" if i == 1 else "", "observation": "冗余" * padding}, ensure_ascii=False))]
    return messages


SNAPSHOT = {"reference_ready": True, "artifact_verified": False, "allowed_actions": ["generate_multiview", "stop"],
            "attempted_actions": ["generate_reference"], "remaining_decisions": 3, "generation_retry_allowed": False}


def test_compaction_bounds_context_preserves_request_facts_and_complete_tool_pairs():
    messages = history()
    original = deepcopy(messages)
    result = prepare_context(messages, SNAPSHOT, [], budget=4500)
    assert result.messages[0] == original[0] and messages == original
    assert size(result.messages) <= 4500 < size(messages)
    assert result.stats["compressed"] and result.stats["droppedExchanges"] >= 6
    assert result.summary["current_task"] == SNAPSHOT
    assert any(a["error"] == "不能重复生图" for a in result.summary["earlier_actions"])
    first_ai = next(i for i, m in enumerate(result.messages) if isinstance(m, AIMessage))
    retained = exchanges([result.messages[0], *result.messages[first_ai:]])
    assert len(retained) <= 2 and retained[-1][-1].tool_call_id == "7"


def test_short_context_is_not_compressed_and_memories_are_data_messages():
    messages = history(count=1, padding=10)
    memories = [{"id": "pref:glaze", "kind": "preference", "value": "哑光青瓷"}]
    result = prepare_context(messages, SNAPSHOT, memories)
    assert result.summary == {} and not result.stats["compressed"]
    assert isinstance(result.messages[1], HumanMessage)
    assert "long_term_memory" in result.messages[1].content
    assert result.messages[-2:] == messages[-2:]


def test_oversized_recent_observation_is_summarized_without_truncating_user_or_budget_reset():
    messages = history(count=1, padding=20000)
    result = prepare_context(messages, SNAPSHOT, [], budget=2500)
    assert result.stats["droppedExchanges"] == 1 and size(result.messages) <= 2500
    assert result.messages[0] == messages[0]
    assert not any(isinstance(m, ToolMessage) for m in result.messages)
    assert result.summary["current_task"]["remaining_decisions"] == 3
    assert result.summary["current_task"]["artifact_verified"] is False


def test_memory_selection_cannot_crowd_out_original_request():
    messages = history(count=2, padding=80)
    result = prepare_context(messages, SNAPSHOT, [{"id": "pref:large", "value": "过长历史" * 2000}], budget=2500)
    assert size(result.messages) <= 2500 and result.messages[0] == messages[0]
    assert result.stats["memoryItems"] == 0


def test_memory_is_reconsidered_after_tool_history_is_compacted():
    messages = history(count=4, padding=1000)
    memories = [{"id": "pref:glaze", "value": "哑光青瓷"},
                {"id": "pref:large", "value": "次要记忆" * 1000}]
    result = prepare_context(messages, SNAPSHOT, memories, budget=5000)
    assert size(result.messages) <= 5000 and result.stats["droppedExchanges"] == 3
    assert result.messages[0] == messages[0] and result.messages[-2:] == messages[-2:]
    assert json.loads(result.messages[1].content.split("\n", 1)[1])["long_term_memory"] == memories[:1]
    assert result.stats["memoryItems"] == 1 and result.stats["memorySourceIds"] == ["pref:glaze"]


def test_over_budget_memory_does_not_shorten_tool_history_that_fits():
    messages = history(count=3, padding=400)
    result = prepare_context(messages, SNAPSHOT, [{"id": "pref:large", "value": "次要记忆" * 1000}], budget=4000)
    assert result.messages == messages and not result.summary
    assert result.stats["droppedExchanges"] == 0 and result.stats["memorySourceIds"] == []


def test_summary_that_cannot_fit_does_not_block_a_valid_uncompressed_context():
    messages = history(count=7, padding=10)
    snapshot = {**SNAPSHOT, "spec": {"object_prompt": "大字段" * 2000}}
    result = prepare_context(messages, snapshot, [], budget=4000)
    assert result.messages == messages and not result.summary
    assert size(result.messages) <= 4000 and result.stats["droppedExchanges"] == 0


def test_required_facts_over_budget_fail_closed():
    with pytest.raises(ToolFailure, match="未丢弃用户要求"):
        prepare_context([HumanMessage("描述" * 3000)], SNAPSHOT, [], budget=2000)


def test_multi_call_exchange_stays_whole():
    messages = history(count=1, padding=10)
    messages += [AIMessage(content="", tool_calls=[{"id": "a", "name": "x", "args": {}},
                                                  {"id": "b", "name": "y", "args": {}}]),
                 ToolMessage(tool_call_id="a", content='{"ok":false}'),
                 ToolMessage(tool_call_id="b", content='{"ok":false}')]
    result = prepare_context(messages, SNAPSHOT, [], max_exchanges=1)
    assert result.messages[-2].tool_call_id == "a" and result.messages[-1].tool_call_id == "b"


def test_orphan_tool_results_are_rejected():
    messages = history(count=1)
    messages[-1] = ToolMessage(tool_call_id="wrong", content="anything")
    with pytest.raises(ToolFailure, match="不匹配"):
        prepare_context(messages, SNAPSHOT, [])
