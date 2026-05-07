"""WebFetchToolV3 集成测试 - 通过 ToolManager 测试"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pyclaego.tool import get_tool_manager


async def test_v3_via_tool_manager():
    """通过 ToolManager 测试 WebFetchToolV3"""
    print("\n" + "="*60)
    print("WebFetchToolV3 集成测试 - 通过 ToolManager")
    print("="*60)
    
    # 获取工具管理器
    tool_manager = get_tool_manager()
    
    # 检查工具是否已加载
    loaded_tools = tool_manager.list_loaded_tools()
    print(f"\n✓ 已加载工具数量：{len(loaded_tools)}")
    
    if "web_fetcher" not in loaded_tools:
        print("✗ web_fetcher 工具未加载")
        return False
    
    print("✓ web_fetcher 工具已加载")
    
    # 获取工具实例
    tool = tool_manager.get_tool("web_fetcher")
    print(f"✓ 工具类型：{type(tool).__name__}")
    
    # 验证是 V3
    if type(tool).__name__ != "WebFetchToolV3":
        print("✗ 工具类型不是 WebFetchToolV3")
        return False
    
    print("✓ 确认使用 WebFetchToolV3")
    
    # 获取工具描述
    desc = tool.get_description()
    print("\n✓ 工具描述:")
    print(f"  - 名称：{desc['name']}")
    print(f"  - 版本：{desc.get('version', 'N/A')}")
    print(f"  - 描述：{desc['description'][:80]}...")
    
    # 测试执行
    print("\n" + "="*60)
    print("执行测试：抓取 example.com")
    print("="*60)
    
    result = await tool_manager.execute_tool(
        "web_fetcher",
        url="https://example.com",
        output_format="md",
        extract_outline=True,
        preview_length=300,
    )
    
    if result.is_success():
        print("\n✓ 工具执行成功")
        print(f"  - URL: {result.output['url']}")
        print(f"  - 标题：{result.output['title']}")
        print(f"  - 内容长度：{result.output['content_length']} 字符")
        print(f"  - 章节数：{len(result.output['outline'])}")
        print(f"  - 输出文件：{result.output['output_file']}")
        print(f"  - 耗时：{result.output['fetch_duration_ms']}ms")
        
        # 验证输出文件存在
        output_file = Path(result.output['output_file'])
        if output_file.exists():
            print("✓ 输出文件已创建")
        else:
            print("✗ 输出文件不存在")
            return False
        
        print("\n" + "="*60)
        print("✓ 所有集成测试通过")
        print("="*60)
        return True
    else:
        print(f"\n✗ 工具执行失败：{result.error}")
        return False


async def main():
    """运行集成测试"""
    success = await test_v3_via_tool_manager()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
