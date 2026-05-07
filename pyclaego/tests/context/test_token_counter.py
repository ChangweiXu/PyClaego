"""测试 TokenCounter - Token 计数器

测试内容：
1. TokenCounter 初始化（已知模型 / 未知模型）
2. count_tokens：文本 token 计数
3. count_messages_tokens：消息列表 token 计数
4. estimate_tokens_from_chars：字符数快速估算 token
5. get_model_info：获取模型信息
6. 多种模型的编码器差异
7. 模块级便捷函数
"""

import sys
from pathlib import Path

# 添加 pyclaego 目录到路径（与其他测试文件保持一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyclaego.context.token_counter import (
    TokenCounter,
    count_messages_tokens,
    count_tokens,
    truncate_text,
)


def print_section(title: str):
    """打印分隔线"""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


# ─────────────────────────────────────────────────────────────
# 测试 1：初始化与模型信息
# ─────────────────────────────────────────────────────────────

def test_token_counter_init():
    """测试 TokenCounter 初始化和模型信息"""
    print_section("测试 1：TokenCounter 初始化与 get_model_info()")

    # 1-a: 已知模型 gpt-4
    counter_gpt4 = TokenCounter("gpt-4")
    info = counter_gpt4.get_model_info()
    print("✓ gpt-4 初始化成功")
    print(f"  - model: {info['model']}")
    print(f"  - encoding_name: {info['encoding_name']}")
    print(f"  - tokens_per_message: {info['tokens_per_message']}")
    print(f"  - tokens_per_name: {info['tokens_per_name']}")

    assert info["model"] == "gpt-4"
    assert info["encoding_name"] == "cl100k_base"
    assert info["tokens_per_message"] == 3
    assert info["tokens_per_name"] == 1
    print("  ✓ 断言全部通过")

    # 1-b: 未知模型（如 claude-3-5-sonnet）→ 退回 cl100k_base
    counter_unknown = TokenCounter("claude-3-5-sonnet")
    info_unknown = counter_unknown.get_model_info()
    print("\n✓ 未知模型 'claude-3-5-sonnet' 初始化成功（使用默认编码器）")
    print(f"  - model: {info_unknown['model']}")
    print(f"  - encoding_name: {info_unknown['encoding_name']}")

    assert info_unknown["model"] == "claude-3-5-sonnet"
    assert info_unknown["encoding_name"] == "cl100k_base", "未知模型应退回 cl100k_base"
    print("  ✓ 断言全部通过")


# ─────────────────────────────────────────────────────────────
# 测试 2：count_tokens
# ─────────────────────────────────────────────────────────────

def test_count_tokens():
    """测试 count_tokens 文本 token 计数"""
    print_section("测试 2：count_tokens(text)")

    counter = TokenCounter("gpt-4")

    # 2-a: 空字符串
    result_empty = counter.count_tokens("")
    print(f"✓ 空字符串 token 数: {result_empty}")
    assert result_empty == 0, "空字符串应返回 0"

    # 2-b: 简单英文
    text_en = "Hello, world!"
    result_en = counter.count_tokens(text_en)
    print(f"✓ 英文 '{text_en}' token 数: {result_en}")
    assert result_en > 0, "英文文本应有 token"
    assert result_en < len(text_en), "英文 token 数应小于字符数"

    # 2-c: 中文文本
    text_zh = "你好，世界！这是一段中文测试文本。"
    result_zh = counter.count_tokens(text_zh)
    print(f"✓ 中文 '{text_zh}' token 数: {result_zh}")
    assert result_zh > 0, "中文文本应有 token"

    # 2-d: 较长英文段落
    text_long = (
        "The quick brown fox jumps over the lazy dog. "
        "This is a longer piece of text to verify that token counting "
        "works correctly for multi-sentence English content."
    )
    result_long = counter.count_tokens(text_long)
    print(f"✓ 长英文 ({len(text_long)} 字符) token 数: {result_long}")
    assert result_long > 20, "长文本应有足够多 token"

    # 2-e: 单个数字 / 标点
    result_num = counter.count_tokens("42")
    print(f"✓ 数字 '42' token 数: {result_num}")
    assert result_num >= 1

    print("\n  ✓ 所有断言通过")


# ─────────────────────────────────────────────────────────────
# 测试 3：count_messages_tokens
# ─────────────────────────────────────────────────────────────

def test_count_messages_tokens():
    """测试 count_messages_tokens 消息列表 token 计数"""
    print_section("测试 3：count_messages_tokens(messages)")

    counter = TokenCounter("gpt-4")

    # 3-a: 空列表
    result_empty = counter.count_messages_tokens([])
    print(f"✓ 空消息列表 token 数: {result_empty}")
    assert result_empty == 0, "空消息列表应返回 0"

    # 3-b: 单条用户消息
    single_msg = [{"role": "user", "content": "Hello"}]
    result_single = counter.count_messages_tokens(single_msg)
    print(f"✓ 单条消息 {single_msg} token 数: {result_single}")
    # 预期: 3 (message overhead) + tokens("user") + tokens("Hello") + 3 (conv overhead)
    assert result_single > 0

    # 3-c: 多轮对话
    conversation = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "The capital of France is Paris."},
        {"role": "user", "content": "Thank you!"},
    ]
    result_conv = counter.count_messages_tokens(conversation)
    print(f"✓ 四轮对话 token 数: {result_conv}")
    assert result_conv > 20, "多轮对话应有足够 token"

    # 3-d: 带 name 字段的消息（有额外 +1 开销）
    msg_with_name = [{"role": "user", "name": "alice", "content": "Hi there!"}]
    result_name = counter.count_messages_tokens(msg_with_name)
    msg_without_name = [{"role": "user", "content": "Hi there!"}]
    result_no_name = counter.count_messages_tokens(msg_without_name)
    print(f"✓ 带 name 字段消息 token 数: {result_name}  (无 name: {result_no_name})")
    # name 字段本身也会被计算，因此 result_name > result_no_name
    assert result_name > result_no_name, "带 name 字段的消息 token 应更多"

    # 3-e: 中文对话
    zh_conversation = [
        {"role": "user", "content": "你好，请问你是谁？"},
        {"role": "assistant", "content": "我是 PyClaego 的智能助手，很高兴认识你！"},
    ]
    result_zh = counter.count_messages_tokens(zh_conversation)
    print(f"✓ 中文对话 token 数: {result_zh}")
    assert result_zh > 5

    print("\n  ✓ 所有断言通过")


# ─────────────────────────────────────────────────────────────
# 测试 4：estimate_tokens_from_chars
# ─────────────────────────────────────────────────────────────

def test_estimate_tokens_from_chars():
    """测试 estimate_tokens_from_chars 快速估算"""
    print_section("测试 4：estimate_tokens_from_chars(char_count)")

    counter = TokenCounter("gpt-4")

    # 4-a: char_count = 0 → max(1, 0//3) = 1（保底值）
    result_zero = counter.estimate_tokens_from_chars(0)
    print(f"✓ estimate_tokens_from_chars(0) = {result_zero}")
    assert result_zero == 1, "char_count=0 应返回保底值 1"

    # 4-b: char_count = 300 → 100
    result_300 = counter.estimate_tokens_from_chars(300)
    print(f"✓ estimate_tokens_from_chars(300) = {result_300}")
    assert result_300 == 100, "300 // 3 = 100"

    # 4-c: char_count = 1 → max(1, 0) = 1
    result_one = counter.estimate_tokens_from_chars(1)
    print(f"✓ estimate_tokens_from_chars(1) = {result_one}")
    assert result_one == 1

    # 4-d: 典型中文句子长度估算
    sample_text = "这是一段典型的中文文本，用于测试估算函数。"
    char_count = len(sample_text)
    estimated = counter.estimate_tokens_from_chars(char_count)
    actual = counter.count_tokens(sample_text)
    print(f"✓ 中文文本 ({char_count} 字符): 估算={estimated}，实际={actual}")
    # 估算值不一定精确，但不应为 0
    assert estimated >= 1

    print("\n  ✓ 所有断言通过")


# ─────────────────────────────────────────────────────────────
# 测试 5：多种模型的编码器
# ─────────────────────────────────────────────────────────────

def test_multiple_models():
    """测试不同模型的编码器初始化"""
    print_section("测试 5：多种模型的编码器差异")

    models_and_expected_encodings = [
        ("gpt-4",          "cl100k_base"),
        ("gpt-3.5-turbo",  "cl100k_base"),
        ("gpt-4o",         "o200k_base"),
        ("claude-sonnet",  "cl100k_base"),  # 未知 → 默认
        ("unknown-model",  "cl100k_base"),  # 未知 → 默认
    ]

    for model, expected_enc in models_and_expected_encodings:
        counter = TokenCounter(model)
        info = counter.get_model_info()
        print(f"✓ 模型 '{model}': encoding = {info['encoding_name']}")
        assert info["encoding_name"] == expected_enc, (
            f"模型 {model} 预期编码器 {expected_enc}，实际 {info['encoding_name']}"
        )

    print("\n  ✓ 所有断言通过")


# ─────────────────────────────────────────────────────────────
# 测试 6：模块级便捷函数
# ─────────────────────────────────────────────────────────────

def test_convenience_functions():
    """测试模块级便捷函数与实例方法结果一致"""
    print_section("测试 6：模块级便捷函数")

    text = "The capital of France is Paris."
    model = "gpt-4"

    # 实例方法
    counter = TokenCounter(model)
    instance_result = counter.count_tokens(text)

    # 便捷函数
    func_result = count_tokens(text, model)

    print(f"✓ count_tokens('{text}', model='{model}')")
    print(f"  - 实例方法结果: {instance_result}")
    print(f"  - 便捷函数结果: {func_result}")
    assert instance_result == func_result, "便捷函数与实例方法结果应一致"

    # 消息便捷函数
    messages = [
        {"role": "user", "content": "What is 2 + 2?"},
        {"role": "assistant", "content": "2 + 2 equals 4."},
    ]
    instance_msg_result = counter.count_messages_tokens(messages)
    func_msg_result = count_messages_tokens(messages, model)

    print(f"\n✓ count_messages_tokens(messages, model='{model}')")
    print(f"  - 实例方法结果: {instance_msg_result}")
    print(f"  - 便捷函数结果: {func_msg_result}")
    assert instance_msg_result == func_msg_result, "便捷函数与实例方法结果应一致"

    print("\n  ✓ 所有断言通过")


# ─────────────────────────────────────────────────────────────
# 测试 7：计数精度对比（真实 tiktoken vs 估算）
# ─────────────────────────────────────────────────────────────

def test_accuracy_comparison():
    """对比精确计数与快速估算的差异，了解两者的适用场景"""
    print_section("测试 7：精确计数 vs 快速估算对比")

    counter = TokenCounter("gpt-4")

    samples = [
        "Hello",
        "The quick brown fox jumps over the lazy dog.",
        "你好，这是一段中文文本，包含标点符号。",
        "import sys\nfrom pathlib import Path\nprint('hello world')",
        "a" * 100,  # 100 个 'a'
    ]

    print(f"{'文本（前30字符）':<32} {'精确':<8} {'估算':<8} {'误差%':<10}")
    print("-" * 60)

    for text in samples:
        precise = counter.count_tokens(text)
        estimated = counter.estimate_tokens_from_chars(len(text))
        if precise > 0:
            error_pct = abs(estimated - precise) / precise * 100
        else:
            error_pct = 0.0
        preview = (text[:29] + "…") if len(text) > 30 else text
        print(f"{preview:<32} {precise:<8} {estimated:<8} {error_pct:<10.1f}")

    print("\n  ✓ 对比完成（此测试仅展示数据，无硬性断言）")


# ─────────────────────────────────────────────────────────────
# 测试 8：truncate_text_to_tokens
# ─────────────────────────────────────────────────────────────

def test_truncate_text_to_tokens():
    """测试 truncate_text_to_tokens 文本截断"""
    print_section("测试 8：truncate_text_to_tokens(text, max_tokens)")

    counter = TokenCounter("gpt-4")

    # 8-a: 空字符串 → ""
    result_empty = counter.truncate_text_to_tokens("", 10)
    print(f"✓ 空字符串截断结果: '{result_empty}'")
    assert result_empty == "", "空字符串应返回空字符串"

    # 8-b: max_tokens=0 → ""
    result_zero = counter.truncate_text_to_tokens("Hello world", 0)
    print(f"✓ max_tokens=0 截断结果: '{result_zero}'")
    assert result_zero == "", "max_tokens=0 应返回空字符串"

    # 8-c: max_tokens 超过实际 token 数 → 返回原文
    short_text = "Hi!"
    original_tokens = counter.count_tokens(short_text)
    result_no_trunc = counter.truncate_text_to_tokens(short_text, original_tokens + 100)
    print(f"✓ 不需截断（原文 {original_tokens} tokens，限制 {original_tokens + 100}）: '{result_no_trunc}'")
    assert result_no_trunc == short_text, "未超限时应返回原文"

    # 8-d: 英文长句截断验证
    long_en = "The quick brown fox jumps over the lazy dog. " * 5
    original_tokens_en = counter.count_tokens(long_en)
    max_tok = 10
    truncated_en = counter.truncate_text_to_tokens(long_en, max_tok)
    actual_tokens_en = counter.count_tokens(truncated_en)
    print(f"✓ 英文截断: 原 {original_tokens_en} tokens → 限 {max_tok} → 实际 {actual_tokens_en} tokens")
    print(f"  截断结果: '{truncated_en}'")
    assert actual_tokens_en == max_tok, f"截断后 token 数应恰好为 {max_tok}，实际 {actual_tokens_en}"
    assert len(truncated_en) < len(long_en), "截断后文本应更短"

    # 8-e: 中文长文本截断验证
    long_zh = "这是一段较长的中文测试文本，用来验证 token 截断功能是否正常工作。" * 3
    original_tokens_zh = counter.count_tokens(long_zh)
    max_tok_zh = 15
    truncated_zh = counter.truncate_text_to_tokens(long_zh, max_tok_zh)
    actual_tokens_zh = counter.count_tokens(truncated_zh)
    print(f"✓ 中文截断: 原 {original_tokens_zh} tokens → 限 {max_tok_zh} → 实际 {actual_tokens_zh} tokens")
    print(f"  截断结果: '{truncated_zh}'")
    assert actual_tokens_zh == max_tok_zh, f"中文截断后 token 数应恰好为 {max_tok_zh}，实际 {actual_tokens_zh}"

    # 8-f: max_tokens=1 → 最小截断
    result_one = counter.truncate_text_to_tokens("Hello world, how are you?", 1)
    tokens_one = counter.count_tokens(result_one)
    print(f"✓ max_tokens=1 截断结果: '{result_one}'（{tokens_one} token）")
    assert tokens_one == 1, "max_tokens=1 时结果应恰好为 1 个 token"

    # 8-g: 便捷函数与实例方法结果一致
    sample = "Artificial intelligence is transforming the world rapidly."
    instance_result = counter.truncate_text_to_tokens(sample, 5)
    func_result = truncate_text(sample, 5, "gpt-4")
    print(f"✓ 便捷函数一致性: instance='{instance_result}', func='{func_result}'")
    assert instance_result == func_result, "便捷函数与实例方法结果应一致"

    print("\n  ✓ 所有断言通过")


# ─────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────

def main():
    """运行所有测试"""
    print("=" * 60)
    print("  PyClaego TokenCounter 测试")
    print("=" * 60)

    try:
        test_token_counter_init()
        test_count_tokens()
        test_count_messages_tokens()
        test_estimate_tokens_from_chars()
        test_multiple_models()
        test_convenience_functions()
        test_accuracy_comparison()
        test_truncate_text_to_tokens()

        print_section("✅ 所有测试通过！")

    except AssertionError as e:
        print(f"\n❌ 断言失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
