"""测试 WebFetchToolV3 — 网页抓取 + HTML→MD 转换 + 大纲提取"""

import asyncio
import sys
import tempfile
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pyclaego.tool.base_tool import ToolStatus
from pyclaego.tool.tools.web_fetch_tool_v3 import WebFetchToolV3


async def test_v3_initialization():
    """测试 V3 工具初始化"""
    print("\n" + "="*60)
    print("测试 1: WebFetchToolV3 初始化")
    print("="*60)
    
    # 创建临时缓存目录
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config = {
            "cache_dir": tmpdir,
            "md_output_dir": tmpdir,
            "timeout": 30,
            "user_agent": "Mozilla/5.0 (compatible; PyClaego/1.0; WebFetchToolV3)",
            "max_content_length": 1048576,
            "cache_ttl": 3600,
        }
        
        tool = WebFetchToolV3(tool_config)
        
        print("✓ 工具初始化成功")
        print(f"  - 缓存目录：{tool.cache_dir}")
        print(f"  - MD 输出目录：{tool._md_output_dir}")
        
        # 检查健康状态
        health = tool.check_health()
        print("\n✓ 健康检查:")
        print(f"  - 状态：{health['status']}")
        print(f"  - 版本：{health['version']}")
        print(f"  - 内置转换器：{health['converters']['builtin']}")
        print(f"  - 自定义转换器：{health['converters']['custom']}")
        
        if health['issues']:
            print(f"  - 问题：{health['issues']}")
        
        return tool


async def test_v3_fetch_example():
    """测试 V3 抓取 example.com"""
    print("\n" + "="*60)
    print("测试 2: 抓取 example.com (通用模式)")
    print("="*60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config = {
            "cache_dir": tmpdir,
            "md_output_dir": tmpdir,
            "timeout": 30,
            "user_agent": "Mozilla/5.0 (compatible; PyClaego/1.0)",
        }
        
        tool = WebFetchToolV3(tool_config)
        
        # 测试抓取
        result = await tool.execute(
            url="https://example.com",
            output_format="md",
            extract_outline=True,
            preview_length=300,
            mode="auto",
        )
        
        if result.status == ToolStatus.SUCCESS:
            print("✓ 网页抓取成功")
            print(f"  - URL: {result.output['url']}")
            print(f"  - 标题：{result.output['title']}")
            print(f"  - 内容长度：{result.output['content_length']} 字符")
            print(f"  - 内容哈希：{result.output['content_hash']}")
            print(f"  - 输出文件：{result.output['output_file']}")
            print(f"  - 使用模式：{result.output['mode_used']}")
            print(f"  - 来自缓存：{result.output['from_cache']}")
            print(f"  - 耗时：{result.output['fetch_duration_ms']}ms")
            
            # 检查大纲
            outline = result.output['outline']
            print(f"\n✓ 章节大纲：{len(outline)} 个章节")
            for i, section in enumerate(outline[:5], 1):
                print(f"  {i}. {section['level']} {section['title']} "
                      f"(行 {section['line_start']}-{section['line_end']})")
            
            # 检查预览
            preview = result.output['preview']
            print(f"\n✓ 预览内容 ({len(preview)} 字符):")
            print(f"  {preview[:200]}...")
            
            # 验证输出文件存在
            output_file = Path(result.output['output_file'])
            if output_file.exists():
                print("\n✓ 输出文件已创建")
                content = output_file.read_text(encoding='utf-8')
                print(f"  - 文件内容长度：{len(content)} 字符")
            else:
                print(f"\n✗ 输出文件不存在：{output_file}")
            
            return result
        else:
            print(f"✗ 网页抓取失败：{result.error}")
            return None


async def test_v3_cache_behavior():
    """测试 V3 缓存行为"""
    print("\n" + "="*60)
    print("测试 3: 缓存行为测试")
    print("="*60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config = {
            "cache_dir": tmpdir,
            "md_output_dir": tmpdir,
            "timeout": 30,
        }
        
        tool = WebFetchToolV3(tool_config)
        url = "https://example.com"
        
        # 第一次抓取（应该从网络获取）
        print("\n第一次抓取（无缓存）...")
        result1 = await tool.execute(url=url, use_cache=True)
        
        if result1.status != ToolStatus.SUCCESS:
            print(f"✗ 第一次抓取失败：{result1.error}")
            return
        
        print("✓ 第一次抓取成功")
        print(f"  - 来自缓存：{result1.output['from_cache']}")
        print(f"  - 耗时：{result1.output['fetch_duration_ms']}ms")
        
        # 第二次抓取（应该使用缓存）
        print("\n第二次抓取（使用缓存）...")
        result2 = await tool.execute(url=url, use_cache=True)
        
        if result2.status == ToolStatus.SUCCESS:
            print("✓ 第二次抓取成功")
            print(f"  - 来自缓存：{result2.output['from_cache']}")
            print(f"  - 耗时：{result2.output['fetch_duration_ms']}ms")
            
            if result2.output['from_cache']:
                print("✓ 缓存命中（符合预期）")
            else:
                print("⚠️  缓存未命中（可能 TTL 过期）")
        
        # 第三次抓取（强制不使用缓存）
        print("\n第三次抓取（强制刷新）...")
        result3 = await tool.execute(url=url, use_cache=False)
        
        if result3.status == ToolStatus.SUCCESS:
            print("✓ 强制刷新成功")
            print(f"  - 来自缓存：{result3.output['from_cache']}")
            print(f"  - 耗时：{result3.output['fetch_duration_ms']}ms")


async def test_v3_text_mode():
    """测试 V3 纯文本模式"""
    print("\n" + "="*60)
    print("测试 4: 纯文本模式 (output_format='text')")
    print("="*60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config = {
            "cache_dir": tmpdir,
            "md_output_dir": tmpdir,
            "timeout": 30,
        }
        
        tool = WebFetchToolV3(tool_config)
        
        # 测试纯文本模式
        result = await tool.execute(
            url="https://example.com",
            output_format="text",
            extract_outline=False,  # 文本模式不提取大纲
            preview_length=200,
        )
        
        if result.status == ToolStatus.SUCCESS:
            print("✓ 纯文本转换成功")
            print(f"  - 输出格式：{result.output['output_format']}")
            print(f"  - 内容长度：{result.output['content_length']} 字符")
            print(f"  - 输出文件：{result.output['output_file']}")
            
            # 验证文件扩展名为 .txt
            if result.output['output_file'].endswith('.txt'):
                print("✓ 文件扩展名正确 (.txt)")
            else:
                print(f"⚠️  文件扩展名异常：{result.output['output_file']}")
            
            # 检查预览
            print("\n✓ 预览内容:")
            print(f"  {result.output['preview']}...")
        else:
            print(f"✗ 纯文本转换失败：{result.error}")


async def test_v3_arxiv_mode():
    """测试 V3 arXiv 专用模式"""
    print("\n" + "="*60)
    print("测试 5: arXiv 专用模式")
    print("="*60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config = {
            "cache_dir": tmpdir,
            "md_output_dir": tmpdir,
            "timeout": 60,  # arXiv 可能较慢
        }
        
        tool = WebFetchToolV3(tool_config)
        
        # 测试 arXiv 论文（使用 HTML 版本）
        arxiv_url = "https://arxiv.org/html/2401.00001"
        
        print(f"\n抓取 arXiv 论文：{arxiv_url}")
        result = await tool.execute(
            url=arxiv_url,
            output_format="md",
            extract_outline=True,
            preview_length=300,
            mode="arxiv",  # 显式指定 arXiv 模式
        )
        
        if result.status == ToolStatus.SUCCESS:
            print("✓ arXiv 论文抓取成功")
            print(f"  - 标题：{result.output['title']}")
            print(f"  - 内容长度：{result.output['content_length']} 字符")
            print(f"  - 使用模式：{result.output['mode_used']}")
            
            # 检查大纲
            outline = result.output['outline']
            print(f"\n✓ 章节大纲：{len(outline)} 个章节")
            for i, section in enumerate(outline[:10], 1):
                indent = "  " * (section['level'] - 1)
                print(f"  {i}. {indent}{section['title']}")
            
            # 验证输出文件
            output_file = Path(result.output['output_file'])
            if output_file.exists():
                content = output_file.read_text(encoding='utf-8')
                print(f"\n✓ 输出文件已创建 ({len(content)} 字符)")
                
                # 检查是否包含 Markdown 格式
                if '**' in content or '#' in content:
                    print("✓ 内容包含 Markdown 格式标记")
            else:
                print("✗ 输出文件不存在")
        else:
            print(f"✗ arXiv 论文抓取失败：{result.error}")
            print("   这可能是网络问题或 arXiv HTML 页面结构变更")


async def test_v3_custom_converter():
    """测试 V3 自定义转换器注册"""
    print("\n" + "="*60)
    print("测试 6: 自定义转换器注册")
    print("="*60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config = {
            "cache_dir": tmpdir,
            "md_output_dir": tmpdir,
            "timeout": 30,
        }
        
        tool = WebFetchToolV3(tool_config)
        
        # 注册一个简单的自定义转换器
        def custom_converter(html_raw: str, soup) -> str:
            """自定义转换器：只返回标题"""
            title = soup.find('title')
            return f"# {title.get_text() if title else '无标题'}\n\n[自定义转换器：仅提取标题]"
        
        # 注册转换器
        WebFetchToolV3.register_converter("title_only", custom_converter)
        print("✓ 注册自定义转换器：title_only")
        
        # 测试自定义转换器
        result = await tool.execute(
            url="https://example.com",
            output_format="md",
            extract_outline=False,
            mode="title_only",  # 使用自定义模式
        )
        
        if result.status == ToolStatus.SUCCESS:
            print("✓ 自定义转换器执行成功")
            print(f"  - 使用模式：{result.output['mode_used']}")
            print(f"  - 可用模式：{result.output['available_modes']}")
            
            # 读取输出文件验证
            output_file = Path(result.output['output_file'])
            content = output_file.read_text(encoding='utf-8')
            print("\n✓ 输出内容:")
            print(f"  {content}")
        else:
            print(f"✗ 自定义转换器执行失败：{result.error}")


async def test_v3_error_handling():
    """测试 V3 错误处理"""
    print("\n" + "="*60)
    print("测试 7: 错误处理")
    print("="*60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tool_config = {
            "cache_dir": tmpdir,
            "md_output_dir": tmpdir,
            "timeout": 10,
        }
        
        tool = WebFetchToolV3(tool_config)
        
        # 测试无效 URL
        print("\n测试 1: 无效 URL")
        result = await tool.execute(url="not-a-valid-url")
        print(f"  结果：{result.status.value}")
        if result.status == ToolStatus.FAILED:
            print(f"  ✓ 正确捕获错误：{result.error}")
        
        # 测试无法访问的 URL
        print("\n测试 2: 无法访问的 URL")
        result = await tool.execute(url="https://this-domain-definitely-does-not-exist-12345.com")
        print(f"  结果：{result.status.value}")
        if result.status == ToolStatus.FAILED:
            print(f"  ✓ 正确捕获错误：{result.error}")
        
        # 测试未知模式（应该降级到 generic）
        print("\n测试 3: 未知模式（应该降级）")
        result = await tool.execute(
            url="https://example.com",
            mode="unknown_mode_xyz",
        )
        if result.status == ToolStatus.SUCCESS:
            print("  ✓ 成功降级到 generic 模式")
            print(f"  - 实际使用模式：{result.output['mode_used']}")


async def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("WebFetchToolV3 完整测试套件")
    print("="*60)
    
    tests = [
        ("初始化测试", test_v3_initialization),
        ("抓取 example.com", test_v3_fetch_example),
        ("缓存行为测试", test_v3_cache_behavior),
        ("纯文本模式", test_v3_text_mode),
        ("arXiv 模式", test_v3_arxiv_mode),
        ("自定义转换器", test_v3_custom_converter),
        ("错误处理", test_v3_error_handling),
    ]
    
    results = []
    
    for name, test_func in tests:
        try:
            result = await test_func()
            results.append((name, "PASS" if result is not None else "FAIL"))
        except Exception as e:
            print(f"\n✗ {name} 测试异常：{e}")
            import traceback
            traceback.print_exc()
            results.append((name, f"ERROR: {e}"))
    
    # 汇总结果
    print("\n" + "="*60)
    print("测试结果汇总")
    print("="*60)
    
    for name, status in results:
        symbol = "✓" if status == "PASS" else "✗"
        print(f"{symbol} {name}: {status}")
    
    passed = sum(1 for _, s in results if s == "PASS")
    total = len(results)
    print(f"\n总计：{passed}/{total} 测试通过")


if __name__ == "__main__":
    asyncio.run(main())
