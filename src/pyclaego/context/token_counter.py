"""Token Counter - 精确的 Token 计数器

使用 tiktoken 库进行精确的 token 计数，支持多种 OpenAI 模型
"""

from typing import Dict, List, Optional
import tiktoken
from ..logging import get_running_log

_rlog = get_running_log()


class TokenCounter:
    """Token 计数器
    
    使用 tiktoken 进行精确的 token 计数
    支持 GPT-4, GPT-3.5, 以及其他 OpenAI 格式的模型
    """
    
    # 模型到编码器的映射
    MODEL_TO_ENCODING = tiktoken.model.MODEL_TO_ENCODING
    
    def __init__(self, model: str = "gpt-5"):
        """初始化 Token 计数器
        
        Args:
            model: 模型名称，用于选择正确的编码器
        """
        self.model = model
        self.encoding = self._get_encoding(model)
        
        # Token 计数的固定开销（OpenAI 消息格式）
        self.tokens_per_message = 3  # 每条消息的固定开销
        self.tokens_per_name = 1     # 有 name 字段时的额外开销
    
    def _get_encoding(self, model: str) -> tiktoken.Encoding:
        """获取模型对应的编码器
        
        Args:
            model: 模型名称
        
        Returns:
            tiktoken.Encoding 实例
        """
        try:
            # 1. 尝试直接从模型名获取编码器
            return tiktoken.encoding_for_model(model)
        except KeyError:
            # 2. 尝试从映射表获取
            encoding_name = self.MODEL_TO_ENCODING.get(model)
            if encoding_name:
                return tiktoken.get_encoding(encoding_name)
            
            # 3. 默认使用 cl100k_base（GPT-4 的编码器）
            _rlog.warning("core_service", f"[TokenCounter] 模型 '{model}' 未识别，使用默认编码器 cl100k_base")
            return tiktoken.get_encoding("cl100k_base")
    
    def count_tokens(self, text: str) -> int:
        """统计文本的 token 数量
        
        Args:
            text: 需要统计的文本
        
        Returns:
            token 数量
        """
        if not text:
            return 0
        
        try:
            tokens = self.encoding.encode(text)
            return len(tokens)
        except Exception as e:
            # 回退到简单估算
            _rlog.error("core_service", f"[TokenCounter] Token 计数失败: {e}，使用估算值")
            return max(1, len(text) // 4)
    
    def count_messages_tokens(self, messages: List[Dict]) -> int:
        """统计消息列表的总 token 数（按照 OpenAI 的计数规则）
        
        Args:
            messages: 消息列表，格式为 [{"role": "...", "content": "..."}, ...]
        
        Returns:
            总 token 数
        """
        if not messages:
            return 0
        
        total_tokens = 0
        
        try:
            for message in messages:
                # 每条消息的基础开销
                total_tokens += self.tokens_per_message
                
                # 统计各字段的 token
                for key, value in message.items():
                    if value:
                        total_tokens += self.count_tokens(str(value))
                        
                        # name 字段有额外开销
                        if key == "name":
                            total_tokens += self.tokens_per_name
            
            # 整体的额外开销（对话开始标记）
            total_tokens += 3
            
            return total_tokens
        
        except Exception as e:
            _rlog.error("core_service", f"[TokenCounter] 消息 token 计数失败: {e}，使用估算值")
            # 回退到简单估算
            total = 0
            for msg in messages:
                total += 4  # 固定开销
                total += len(msg.get("content", "")) // 4
            return total
    
    def estimate_tokens_from_chars(self, char_count: int) -> int:
        """从字符数估算 token 数（快速估算）
        
        Args:
            char_count: 字符数
        
        Returns:
            估算的 token 数
        """
        # 英文: 1 token ≈ 4 characters
        # 中文: 1 token ≈ 1.5-2 characters
        # 使用平均值 3 作为估算
        return max(1, char_count // 3)
    
    def truncate_text_to_tokens(self, text: str, max_tokens: int) -> str:
        """截断文本，保留前 N 个 token 对应的内容
        
        Args:
            text: 原始文本
            max_tokens: 最多保留的 token 数量
        
        Returns:
            截断后的文本字符串。若 max_tokens <= 0 返回空字符串；
            若原文 token 数 <= max_tokens 则返回原文本不做截断。
        """
        if not text:
            return ""
        
        if max_tokens <= 0:
            return ""
        
        try:
            tokens = self.encoding.encode(text)
            
            # token 数未超限，直接返回原文
            if len(tokens) <= max_tokens:
                return text
            
            # 截取前 max_tokens 个 token 并解码
            truncated_tokens = tokens[:max_tokens]
            return self.encoding.decode(truncated_tokens)
        
        except Exception as e:
            _rlog.error("core_service", f"[TokenCounter] 文本截断失败: {e}，按字符比例截断")
            # 回退：按估算比例粗裁（4 chars per token）
            char_limit = max_tokens * 4
            return text[:char_limit]
    
    def get_model_info(self) -> Dict:
        """获取当前模型的信息
        
        Returns:
            包含模型信息的字典
        """
        return {
            "model": self.model,
            "encoding_name": self.encoding.name,
            "tokens_per_message": self.tokens_per_message,
            "tokens_per_name": self.tokens_per_name
        }


# 便捷函数
def count_tokens(text: str, model: str = "gpt-4") -> int:
    """统计文本的 token 数（便捷函数）
    
    Args:
        text: 需要统计的文本
        model: 模型名称
    
    Returns:
        token 数量
    """
    counter = TokenCounter(model)
    return counter.count_tokens(text)


def count_messages_tokens(messages: List[Dict], model: str = "gpt-4") -> int:
    """统计消息列表的 token 数（便捷函数）
    
    Args:
        messages: 消息列表
        model: 模型名称
    
    Returns:
        总 token 数
    """
    counter = TokenCounter(model)
    return counter.count_messages_tokens(messages)


def truncate_text(text: str, max_tokens: int, model: str = "gpt-4") -> str:
    """截断文本，保留前 N 个 token 对应的内容（便捷函数）
    
    Args:
        text: 原始文本
        max_tokens: 最多保留的 token 数量
        model: 模型名称
    
    Returns:
        截断后的文本字符串
    """
    counter = TokenCounter(model)
    return counter.truncate_text_to_tokens(text, max_tokens)
