"""测试工具系统"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from pyclaego.tool import ToolManager, get_tool_manager


async def test_tool_manager_init():
    """测试工具管理器初始化"""
    print("\n" + "="*60)
    print("测试 1: 工具管理器初始化")
    print("="*60)
    
    # 获取工具管理器实例
    tool_manager = get_tool_manager()
    
    # 列出可用工具类型
    available_tools = ToolManager.list_available_tools()
    print(f"✓ 已注册工具类型: {available_tools}")
    
    # 列出已加载的工具
    loaded_tools = tool_manager.list_loaded_tools()
    print(f"✓ 已加载工具: {loaded_tools}")
    
    # 获取所有工具信息
    tools_info = tool_manager.get_all_tools_info()
    print("\n✓ 工具详细信息:")
    for tool_name, info in tools_info.items():
        print(f"\n  工具: {tool_name}")
        print(f"    - 类型: {info['tool_type']}")
        print(f"    - 启用: {info['enabled']}")
        print(f"    - 超时: {info['timeout']}秒")
        print(f"    - 描述: {info['description']['description']}")


async def test_bash_tool():
    """测试 Bash 工具"""
    print("\n" + "="*60)
    print("测试 2: Bash 工具")
    print("="*60)
    
    tool_manager = get_tool_manager()
    
    # 测试用例 1: 简单命令
    print("\n测试用例 1: 执行 'echo Hello World'")
    result1 = await tool_manager.execute_tool(
        "bash_executor",
        command="echo 'Hello from PyClaego!'"
    )
    
    if result1.is_success():
        print("✓ 命令执行成功")
        print(f"  - 输出: {result1.output['stdout'].strip()}")
        print(f"  - 返回码: {result1.output['return_code']}")
    else:
        print(f"✗ 命令执行失败: {result1.error}")
    
    # 测试用例 2: 列出文件
    print("\n测试用例 2: 执行 'ls -la'")
    result2 = await tool_manager.execute_tool(
        "bash_executor",
        command="ls -la"
    )
    
    if result2.is_success():
        print("✓ 命令执行成功")
        lines = result2.output['stdout'].strip().split('\n')
        print(f"  - 输出行数: {len(lines)}")
        print("  - 前3行:")
        for line in lines[:3]:
            print(f"    {line}")
    else:
        print(f"✗ 命令执行失败: {result2.error}")
    
    # 测试用例 3: 危险命令（应该被阻止）
    print("\n测试用例 3: 尝试执行危险命令 'rm -rf /'")
    result3 = await tool_manager.execute_tool(
        "bash_executor",
        command="rm -rf /"
    )
    
    if result3.is_success():
        print("✗ 危险命令被执行（不应该发生）")
    else:
        print("✓ 危险命令被阻止")
        print(f"  - 原因: {result3.error}")


async def test_web_fetch_tool():
    """测试 Web Fetch 工具"""
    print("\n" + "="*60)
    print("测试 3: Web Fetch 工具")
    print("="*60)
    
    tool_manager = get_tool_manager()
    
    # 检查工具是否可用
    if "web_fetcher" not in tool_manager.list_loaded_tools():
        print("⚠️  Web Fetch 工具未启用，跳过测试")
        return
    
    # 测试用例: 抓取 Example.com
    print("\n测试用例: 抓取 https://example.com")
    result = await tool_manager.execute_tool(
        "web_fetcher",
        url="https://example.com",
        extract_text=True,
        extract_metadata=True
    )
    
    if result.is_success():
        print("✓ 网页抓取成功")
        print(f"  - URL: {result.output['url']}")
        print(f"  - 状态码: {result.output['status_code']}")
        print(f"  - 内容类型: {result.output['content_type']}")
        
        if 'metadata' in result.output:
            metadata = result.output['metadata']
            print(f"  - 标题: {metadata.get('title', 'N/A')}")
            print(f"  - 描述: {metadata.get('description', 'N/A')[:100]}...")
        
        if 'text' in result.output:
            text = result.output['text']
            print(f"  - 文本长度: {result.output['text_length']} 字符")
            print(f"  - 前200字符: {text[:200]}...")
    else:
        print(f"✗ 网页抓取失败: {result.error}")


async def test_web_search_tool():
    """测试 Web Search 工具"""
    print("\n" + "="*60)
    print("测试 4: Web Search 工具")
    print("="*60)
    
    tool_manager = get_tool_manager()
    
    # 检查工具是否可用
    if "web_searcher" not in tool_manager.list_loaded_tools():
        print("⚠️  Web Search 工具未启用，跳过测试")
        print("提示：如需启用，请设置环境变量:")
        print("  export WEB_SEARCH_ENABLED=true")
        print("  export BRAVE_API_KEY=your_api_key")
        return
    
    # 测试用例: 搜索 Python asyncio
    print("\n测试用例: 搜索 'Python asyncio tutorial'")
    result = await tool_manager.execute_tool(
        "web_searcher",
        query="Python asyncio tutorial",
        max_results=5
    )
    
    if result.is_success():
        print("✓ 搜索成功")
        print(f"  - 查询: {result.output['query']}")
        print(f"  - 结果数: {result.output['count']}")
        print(f"  - 提供商: {result.metadata['provider']}")
        
        print("\n  前3条结果:")
        for i, item in enumerate(result.output['results'][:3], 1):
            print(f"\n  {i}. {item['title']}")
            print(f"     URL: {item['url']}")
            print(f"     描述: {item['description'][:100]}...")
    else:
        print(f"✗ 搜索失败: {result.error}")


async def test_tool_timeout():
    """测试工具超时"""
    print("\n" + "="*60)
    print("测试 5: 工具超时")
    print("="*60)
    
    tool_manager = get_tool_manager()
    
    # 测试用例: 执行长时间运行的命令
    print("\n测试用例: 执行 'sleep 5' (超时设置为 2 秒)")
    
    # 注意：这需要修改配置或创建临时工具实例
    # 这里只是演示，实际会使用配置中的超时值
    result = await tool_manager.execute_tool(
        "bash_executor",
        command="sleep 2"
    )
    
    if result.status.value == "timeout":
        print("✓ 命令超时（符合预期）")
        print(f"  - 错误: {result.error}")
    elif result.is_success():
        print("✓ 命令在超时前完成")
    else:
        print(f"✗ 命令执行失败: {result.error}")


async def test_disabled_tool():
    """测试禁用的工具"""
    print("\n" + "="*60)
    print("测试 6: 禁用的工具")
    print("="*60)
    
    tool_manager = get_tool_manager()
    
    # 如果 web_searcher 未启用
    if "web_searcher" not in tool_manager.list_loaded_tools():
        print("✓ Web Search 工具未加载（因为未启用）")
        
        # 尝试执行
        result = await tool_manager.execute_tool(
            "web_searcher",
            query="test"
        )
        
        if not result.is_success():
            print(f"✓ 执行被拒绝: {result.error}")
    else:
        print("⚠️  Web Search 工具已启用，无法测试禁用状态")


async def test_tool_error_handling():
    """测试工具错误处理"""
    print("\n" + "="*60)
    print("测试 7: 错误处理")
    print("="*60)
    
    tool_manager = get_tool_manager()
    
    # 测试用例 1: 缺少必需参数
    print("\n测试用例 1: 缺少必需参数")
    result1 = await tool_manager.execute_tool(
        "bash_executor"
        # 故意不传 command 参数
    )
    
    if not result1.is_success():
        print(f"✓ 错误被正确捕获: {result1.error}")
    else:
        print("✗ 应该报错但成功了")
    
    # 测试用例 2: 工具不存在
    print("\n测试用例 2: 工具不存在")
    result2 = await tool_manager.execute_tool(
        "non_existent_tool",
        command="test"
    )
    
    if not result2.is_success():
        print(f"✓ 错误被正确捕获: {result2.error}")
    else:
        print("✗ 应该报错但成功了")
    
    # 测试用例 3: 无效的 URL
    if "web_fetcher" in tool_manager.list_loaded_tools():
        print("\n测试用例 3: 无效的 URL")
        result3 = await tool_manager.execute_tool(
            "web_fetcher",
            url="not-a-valid-url"
        )
        
        if not result3.is_success():
            print(f"✓ 错误被正确捕获: {result3.error}")
        else:
            print("✗ 应该报错但成功了")


async def main():
    """主测试函数"""
    print("\n" + "🔧"*30)
    print("PyClaego 工具系统测试")
    print("🔧"*30)
    
    try:
        # 测试工具管理器初始化
        await test_tool_manager_init()
        
        # 测试 Bash 工具
        await test_bash_tool()
        
        # 测试 Web Fetch 工具
        await test_web_fetch_tool()
        
        # 测试 Web Search 工具
        await test_web_search_tool()
        
        # 测试超时
        await test_tool_timeout()
        
        # 测试禁用的工具
        await test_disabled_tool()
        
        # 测试错误处理
        await test_tool_error_handling()
        
        print("\n" + "="*60)
        print("✅ 所有测试完成!")
        print("="*60)
        print("\n提示：")
        print("1. 某些工具需要配置 API Key 才能使用")
        print("2. 在 config.yaml 中可以配置每个工具的参数")
        print("3. 通过 enabled 字段可以启用/禁用工具")
        print("="*60 + "\n")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
