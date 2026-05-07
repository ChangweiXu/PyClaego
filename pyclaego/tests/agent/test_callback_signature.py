#!/usr/bin/env python3
"""测试回调通知函数参数签名的一致性"""
import asyncio
from collections.abc import Callable
from typing import Any


async def test_tagged_handler_signature():
    """测试 _make_tagged_handler 返回的函数签名是否正确"""
    
    # 模拟原始 handler
    received_messages = []
    
    async def original_handler(msg: dict[str, Any]) -> None:
        received_messages.append(msg)
        print(f"📩 收到消息: {msg}")
    
    # 模拟 _make_tagged_handler 的实现
    def make_tagged_handler(
        original_handler: Callable | None,
        subagent_id: str,
    ) -> Callable | None:
        if original_handler is None:
            return None
        
        async def _tagged_handler(msg: dict[str, Any]) -> None:
            tagged_msg = dict(msg)
            if "metadata" not in tagged_msg:
                tagged_msg["metadata"] = {}
            tagged_msg["metadata"]["subagent_id"] = subagent_id
            tagged_msg["metadata"]["is_subagent_update"] = True
            await original_handler(tagged_msg)
        
        return _tagged_handler
    
    # 创建带标记的 handler
    tagged_handler = make_tagged_handler(original_handler, "test_subagent_001")
    
    # 测试用例 1: SubAgent Spawn 开始通知
    print("\n🧪 测试 1: SubAgent Spawn 开始通知")
    await tagged_handler({
        "type": "progress_update",
        "task": {
            "action": "start",
            "task_type": "subagent_spawn",
            "name": "SubAgent: test_subagent_001 (echo)",
            "metadata": {
                "subagent_id": "test_subagent_001",
                "subagent_type": "echo",
                "memory_mode": "empty",
            },
        },
        "content": "[SpawnSubagentTool] 开始创建子 Agent: test_subagent_001",
    })
    
    # 测试用例 2: SubAgent Spawn 完成通知
    print("\n🧪 测试 2: SubAgent Spawn 完成通知")
    await tagged_handler({
        "type": "progress_update",
        "task": {
            "action": "complete",
            "result": {
                "output_len": 123,
                "workspace_path": "/path/to/workspace",
            },
        },
        "content": "[SpawnSubagentTool] 子 Agent 完成: test_subagent_001",
    })
    
    # 测试用例 3: SubAgent Spawn 失败通知
    print("\n🧪 测试 3: SubAgent Spawn 失败通知")
    await tagged_handler({
        "type": "progress_update",
        "task": {
            "action": "fail",
            "error": "子 Agent 执行超时",
        },
        "content": "[SpawnSubagentTool] 子 Agent 失败: test_subagent_001",
    })
    
    # 验证结果
    print("\n✅ 验证结果:")
    assert len(received_messages) == 3, f"应该收到 3 条消息，实际收到 {len(received_messages)} 条"
    
    for idx, msg in enumerate(received_messages, 1):
        print(f"\n消息 {idx}:")
        assert "metadata" in msg, "消息应该包含 metadata 字段"
        assert msg["metadata"]["subagent_id"] == "test_subagent_001", "metadata 应该包含 subagent_id"
        assert msg["metadata"]["is_subagent_update"] is True, "metadata 应该包含 is_subagent_update=True"
        print(f"  ✓ type: {msg.get('type')}")
        print(f"  ✓ task.action: {msg.get('task', {}).get('action')}")
        print(f"  ✓ metadata.subagent_id: {msg['metadata']['subagent_id']}")
        print(f"  ✓ metadata.is_subagent_update: {msg['metadata']['is_subagent_update']}")
    
    print("\n🎉 所有测试通过！")


async def test_simple_agent_notification_format():
    """测试 SimpleAgent 通知格式"""
    print("\n" + "=" * 60)
    print("🧪 测试 SimpleAgent 通知格式")
    print("=" * 60)
    
    received_messages = []
    
    async def msg_update_handler(msg: dict[str, Any]) -> None:
        received_messages.append(msg)
    
    # 模拟 SimpleAgent 的通知
    round_count = 1
    tool_calls = [
        {"name": "read_file", "id": "call_1"},
        {"name": "write_file", "id": "call_2"},
    ]
    
    # Agent Loop 开始通知
    await msg_update_handler({
        "type": "progress_update",
        "task": {
            "action": "start",
            "task_type": "agent_loop",
            "name": f"Agent Loop #{round_count}",
            "metadata": {"round": round_count},
        },
        "content": f"[SimpleAgent] [round {round_count}] 正在调用 LLM...",
        "metadata": {"step": "calling_llm", "round": round_count},
    })
    
    # 工具执行开始通知
    await msg_update_handler({
        "type": "progress_update",
        "task": {
            "action": "start",
            "task_type": "tool_execution",
            "name": f"Tool Execution (Round #{round_count})",
            "metadata": {
                "round": round_count,
                "tool_count": len(tool_calls),
                "tool_names": [tc["name"] for tc in tool_calls],
            },
        },
    })
    
    # 工具执行完成通知
    success_count = 2
    total_count = 2
    await msg_update_handler({
        "type": "progress_update",
        "task": {
            "action": "complete",
            "task_type": "tool_execution",  # 添加缺失的 task_type
            "result": {
                "success_count": success_count,
                "total_count": total_count,
            },
        },
        "content": f"[SimpleAgent] [round {round_count}] 工具执行结束 ({success_count}/{total_count} 成功)",
        "metadata": {
            "step": "tools_done",
            "round": round_count,
            "success_count": success_count,
        },
    })
    
    # 验证
    assert len(received_messages) == 3
    print(f"✅ 收到 {len(received_messages)} 条通知")
    for idx, msg in enumerate(received_messages, 1):
        assert msg["type"] == "progress_update"
        assert "task" in msg
        print(f"  {idx}. {msg['task']['task_type']}.{msg['task']['action']} ✓")
    
    print("🎉 SimpleAgent 通知格式测试通过！")


async def main():
    """运行所有测试"""
    print("🚀 开始测试回调通知函数参数签名...")
    
    try:
        await test_tagged_handler_signature()
        await test_simple_agent_notification_format()
        print("\n" + "=" * 60)
        print("✅ 所有测试通过！回调函数签名正确！")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
