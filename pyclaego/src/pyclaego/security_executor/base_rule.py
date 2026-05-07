"""安全规则基类 - 定义安全规则的抽象接口"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from ..logging import get_running_log

_rlog = get_running_log()


class SecurityDecision(Enum):
    """安全审查决策"""
    ALLOW = "allow"
    DENY = "deny"
    WARN = "warn"
    QUERY = "query"  # suspend and ask the user for confirmation


class RequestType(Enum):
    """请求类型"""
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"


class BaseSecurityRule(ABC):
    """安全规则抽象基类
    
    所有安全规则实现都必须继承此类并实现以下方法：
    - matches(): 判断请求是否匹配该规则
    - get_decision(): 返回规则的决策结果

    可选覆盖：
    - get_match_reason(): 返回最近一次匹配的人类可读理由
    - on_request_completed(): 请求完成后回调，用于状态化规则更新计数器
    - applies_to_tool(): 配合 tool_names 过滤器使用
    """
    
    def __init__(self, rule_config: dict[str, Any]):
        """初始化安全规则
        
        Args:
            rule_config: 规则配置字典，包含规则的所有配置参数
        """
        self.config = rule_config
        self.rule_type = rule_config.get("rule_type", "unknown")
        self.rule_id = rule_config.get("rule_id", f"{self.rule_type}_unnamed")
        self.enabled = rule_config.get("enabled", False)
        self.request_types = rule_config.get("request_types", ["llm_call", "tool_call"])
        self.action = rule_config.get("action", "warn")  # allow, deny, warn

        # 工具名称过滤（可选）
        self.tool_names: list[str] = rule_config.get("tool_names", []) or []
        
        _rlog.info("core_service", f"[SecurityRule] 初始化规则: {self.rule_id} (type={self.rule_type}, enabled={self.enabled})")
    
    @abstractmethod
    async def matches(self, request: dict[str, Any]) -> bool:
        """判断请求是否匹配该规则
        
        Args:
            request: 请求信息字典，包含：
                - type: 请求类型 (llm_call / tool_call)
                - session_id: 会话 ID
                - timestamp: 时间戳
                - 其他请求特定的数据
            
        Returns:
            bool: True 表示匹配，False 表示不匹配
        """
        pass
    
    def get_decision(self) -> SecurityDecision:
        """获取规则的决策结果
        
        Returns:
            SecurityDecision: 安全决策（ALLOW / DENY / WARN / QUERY）
        """
        action_map = {
            "allow": SecurityDecision.ALLOW,
            "deny": SecurityDecision.DENY,
            "warn": SecurityDecision.WARN,
            "query": SecurityDecision.QUERY,
        }
        return action_map.get(self.action, SecurityDecision.WARN)

    def get_query_spec(self) -> dict[str, Any] | None:
        """Return query specification when action=='query'.

        Subclasses that set ``action: query`` should override this to return a
        dict with at least ``prompt`` and ``choices`` keys.  The base
        implementation reads ``query_spec`` from the rule config directly.

        Returns:
            dict with keys:
              - ``prompt`` (str): Message shown to the user.
              - ``choices`` (list[dict]): Each item has ``value`` (str) and
                ``label`` (str), optionally ``description`` (str).
              - ``deny_values`` (list[str]): Choice values that block execution.
              - ``default`` (str|None): Value used on timeout.
              - ``timeout_s`` (int|None): Seconds before auto-resolving with default.
            None if not applicable.
        """
        return self.config.get("query_spec") or None

    def get_match_reason(self) -> str:
        """返回最近一次匹配的可读原因（默认返回 rule_id）

        子类可覆盖以提供更丰富的解释（例如 LLM 审查报告摘要、违反的命中项）。
        """
        return f"matched rule {self.rule_id}"
    
    def is_enabled(self) -> bool:
        """检查规则是否启用
        
        Returns:
            bool: 是否启用
        """
        return self.enabled
    
    def applies_to_request_type(self, request_type: str) -> bool:
        """检查规则是否适用于指定的请求类型
        
        Args:
            request_type: 请求类型
            
        Returns:
            bool: 是否适用
        """
        return request_type in self.request_types

    def applies_to_tool(self, tool_name: str | None) -> bool:
        """配合 tool_names 配置使用：若未配置则全部命中；否则按列表匹配。"""
        if not self.tool_names:
            return True
        if not tool_name:
            return False
        return tool_name in self.tool_names

    async def on_request_completed(
        self,
        request: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> None:
        """请求完成后的回调（默认无操作）

        状态化规则（rate_limit / cost_budget / loop_detector / subagent_depth
        等）可覆盖此方法以更新内部计数器。

        Args:
            request: 与 matches() 相同的请求字典
            result:  可能包含 hook(before/after_*)、success、usage（LLM 用量）、
                     output、error 等字段；前置阶段可能仅含 decision 信息。
        """
        return None
    
    def get_info(self) -> dict[str, Any]:
        """获取规则信息
        
        Returns:
            Dict: 规则信息字典
        """
        return {
            "rule_id": self.rule_id,
            "rule_type": self.rule_type,
            "enabled": self.enabled,
            "request_types": self.request_types,
            "action": self.action,
            "tool_names": self.tool_names,
        }
