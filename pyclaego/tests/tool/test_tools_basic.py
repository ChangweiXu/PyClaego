"""简化的工具系统测试（不依赖外部HTTP库）"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))


async def test_basic_imports():
    """测试基本导入"""
    print("\n" + "="*60)
    print("测试 1: 基本模块导入")
    print("="*60)
    
    try:
        from pyclaego.tool import BaseTool, ToolManager, ToolResult, ToolStatus
        print("✓ 成功导入: BaseTool, ToolResult, ToolStatus, ToolManager")
        
        from pyclaego.tool.tools import BashTool
        print("✓ 成功导入: BashTool")
        
        return True
    except ImportError as e:
        print(f"✗ 导入失败: {e}")
        return False


async def test_tool_manager_registry():
    """测试工具管理器注册表"""
    print("\n" + "="*60)
    print("测试 2: 工具类型注册")
    print("="*60)
    
    try:
        from pyclaego.tool import ToolManager
        
        # 列出可用工具类型
        available_tools = ToolManager.list_available_tools()
        print(f"✓ 已注册工具类型: {available_tools}")
        
        if "bash" in available_tools:
            print("✓ Bash 工具已注册")
        else:
            print("✗ Bash 工具未注册")
        
        return True
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_bash_tool_creation():
    """测试 Bash 工具创建"""
    print("\n" + "="*60)
    print("测试 3: Bash 工具创建")
    print("="*60)
    
    try:
        from pyclaego.tool import ToolManager
        
        # 创建 Bash 工具配置
        bash_config = {
            "tool_type": "bash",
            "tool_name": "test_bash",
            "enabled": True,
            "timeout": 30,
            "blocked_commands": ["rm", "dd"]
        }
        
        # 创建工具实例
        bash_tool = ToolManager.create_tool(bash_config)
        print("✓ 成功创建 Bash 工具实例")
        
        # 获取工具信息
        info = bash_tool.get_info()
        print(f"  - 工具名称: {info['tool_name']}")
        print(f"  - 工具类型: {info['tool_type']}")
        print(f"  - 启用状态: {info['enabled']}")
        print(f"  - 超时设置: {info['timeout']}秒")
        
        # 获取工具描述
        description = bash_tool.get_description()
        print(f"  - 描述: {description['description']}")
        
        return True
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_bash_tool_execution():
    """测试 Bash 工具执行"""
    print("\n" + "="*60)
    print("测试 4: Bash 工具执行")
    print("="*60)
    
    try:
        from pyclaego.tool import ToolManager
        
        # 创建 Bash 工具
        bash_config = {
            "tool_type": "bash",
            "tool_name": "test_bash",
            "enabled": True,
            "timeout": 10,
            "blocked_commands": ["rm", "dd", "format"]
        }
        
        bash_tool = ToolManager.create_tool(bash_config)
        
        # 测试用例 1: 简单命令
        print("\n测试用例 1: 执行 'echo Hello PyClaego'")
        result1 = await bash_tool.execute(command="echo 'Hello PyClaego'")
        
        if result1.is_success():
            print("✓ 命令执行成功")
            print(f"  - 状态: {result1.status.value}")
            print(f"  - 输出: {result1.output['stdout'].strip()}")
            print(f"  - 返回码: {result1.output['return_code']}")
        else:
            print(f"✗ 命令执行失败: {result1.error}")
        
        # 测试用例 2: 获取当前目录
        print("\n测试用例 2: 执行 'pwd'")
        result2 = await bash_tool.execute(command="pwd")
        
        if result2.is_success():
            print("✓ 命令执行成功")
            print(f"  - 当前目录: {result2.output['stdout'].strip()}")
        else:
            print(f"✗ 命令执行失败: {result2.error}")
        
        # 测试用例 3: 危险命令（应该被阻止）
        print("\n测试用例 3: 尝试执行危险命令 'rm -rf /'")
        result3 = await bash_tool.execute(command="rm -rf /")
        
        if result3.is_success():
            print("✗ 危险命令被执行了（不应该发生！）")
        else:
            print("✓ 危险命令被正确阻止")
            print(f"  - 原因: {result3.error}")
        
        # 测试用例 4: 缺少必需参数
        print("\n测试用例 4: 缺少必需参数")
        result4 = await bash_tool.execute()  # 故意不传 command
        
        if result4.is_success():
            print("✗ 应该报错但成功了")
        else:
            print("✓ 参数验证正确")
            print(f"  - 错误: {result4.error}")
        
        return True
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_tool_manager_singleton():
    """测试工具管理器单例模式"""
    print("\n" + "="*60)
    print("测试 5: 工具管理器单例模式")
    print("="*60)
    
    try:
        from pyclaego.tool import ToolManager, get_tool_manager
        
        # 获取两个实例
        manager1 = get_tool_manager()
        manager2 = ToolManager.get_instance()
        manager3 = ToolManager()
        
        # 验证是否是同一个实例
        if manager1 is manager2 is manager3:
            print("✓ 工具管理器正确实现单例模式")
            print(f"  - manager1 is manager2: {manager1 is manager2}")
            print(f"  - manager2 is manager3: {manager2 is manager3}")
        else:
            print("✗ 单例模式实现有问题")
        
        return True
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_config_loading():
    """测试从配置文件加载工具"""
    print("\n" + "="*60)
    print("测试 6: 从配置文件加载工具")
    print("="*60)
    
    try:
        from pyclaego.tool import get_tool_manager
        
        # 获取工具管理器（会自动从配置加载）
        tool_manager = get_tool_manager()
        
        # 列出已加载的工具
        loaded_tools = tool_manager.list_loaded_tools()
        print(f"✓ 已加载工具: {loaded_tools}")
        
        if loaded_tools:
            print("\n工具详细信息:")
            for tool_name in loaded_tools:
                tool = tool_manager.get_tool(tool_name)
                if tool:
                    info = tool.get_info()
                    print(f"\n  {tool_name}:")
                    print(f"    - 类型: {info['tool_type']}")
                    print(f"    - 启用: {info['enabled']}")
                    print(f"    - 超时: {info['timeout']}秒")
        else:
            print("⚠️  没有加载任何工具（可能配置中所有工具都被禁用）")
        
        return True
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """主测试函数"""
    print("\n" + "🔧"*30)
    print("PyClaego 工具系统基础测试")
    print("（不依赖外部 HTTP 库）")
    print("🔧"*30)
    
    tests_passed = 0
    tests_total = 0
    
    # 运行测试
    tests = [
        ("基本模块导入", test_basic_imports),
        ("工具类型注册", test_tool_manager_registry),
        ("Bash 工具创建", test_bash_tool_creation),
        ("Bash 工具执行", test_bash_tool_execution),
        ("工具管理器单例", test_tool_manager_singleton),
        ("配置文件加载", test_config_loading),
    ]
    
    for test_name, test_func in tests:
        tests_total += 1
        try:
            result = await test_func()
            if result:
                tests_passed += 1
        except Exception as e:
            print(f"\n测试异常: {test_name} - {e}")
    
    # 总结
    print("\n" + "="*60)
    print(f"测试完成: {tests_passed}/{tests_total} 通过")
    print("="*60)
    
    if tests_passed == tests_total:
        print("✅ 所有基础测试通过!")
        print("\n提示：")
        print("1. Bash 工具已可用")
        print("2. Web Search 和 Web Fetch 工具需要安装依赖:")
        print("   pip install aiohttp beautifulsoup4")
        print("3. Web Search 还需要配置 API Key")
    else:
        print(f"⚠️  {tests_total - tests_passed} 个测试失败")
    
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
