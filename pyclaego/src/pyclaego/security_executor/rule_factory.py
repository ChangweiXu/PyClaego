"""安全规则工厂 - 根据配置创建安全规则实例"""

from typing import Any

from ..logging import get_running_log
from .base_rule import BaseSecurityRule
from .rules import RULE_REGISTRY

_rlog = get_running_log()


class SecurityRuleFactory:
    """安全规则工厂类
    
    功能：
    - 根据配置动态创建安全规则
    - 支持注册自定义规则类型
    - 提供统一的创建接口
    """
    
    # 规则类型注册表
    _rule_registry: dict[str, type[BaseSecurityRule]] = RULE_REGISTRY.copy()
    
    @classmethod
    def create_rule(cls, rule_config: dict[str, Any]) -> BaseSecurityRule:
        """创建安全规则实例
        
        Args:
            rule_config: 规则配置字典，必须包含 "rule_type" 字段
            
        Returns:
            BaseSecurityRule: 安全规则实例
            
        Raises:
            ValueError: 如果规则类型未注册或配置无效
        """
        rule_type = rule_config.get("rule_type")
        
        if not rule_type:
            raise ValueError("规则配置必须包含 'rule_type' 字段")
        
        rule_class = cls._rule_registry.get(rule_type)
        
        if not rule_class:
            raise ValueError(
                f"未知的规则类型: {rule_type}。"
                f"已注册的类型: {list(cls._rule_registry.keys())}"
            )
        
        _rlog.info("core_service", f"[SecurityRuleFactory] 创建规则: {rule_type} (id={rule_config.get('rule_id', 'unnamed')})")
        return rule_class(rule_config)
    
    @classmethod
    def create_rules_from_config(cls, rules_config: list[dict[str, Any]]) -> list[BaseSecurityRule]:
        """从配置列表批量创建规则
        
        Args:
            rules_config: 规则配置列表
            
        Returns:
            List[BaseSecurityRule]: 规则实例列表
        """
        rules = []
        
        for rule_config in rules_config:
            try:
                rule = cls.create_rule(rule_config)
                rules.append(rule)
            except Exception as e:
                rule_id = rule_config.get("rule_id", "unknown")
                _rlog.error("core_service", f"[SecurityRuleFactory] 创建规则失败 (id={rule_id}): {e}")
                # 继续创建其他规则，不因单个规则失败而终止
        
        return rules
    
    @classmethod
    def register_rule(cls, rule_type: str, rule_class: type[BaseSecurityRule]) -> None:
        """注册自定义规则类型
        
        Args:
            rule_type: 规则类型名称
            rule_class: 规则类
        """
        if rule_type in cls._rule_registry:
            _rlog.warning("core_service", f"[SecurityRuleFactory] 规则类型 '{rule_type}' 已存在，将被覆盖")
        
        cls._rule_registry[rule_type] = rule_class
        _rlog.info("core_service", f"[SecurityRuleFactory] 已注册规则类型: {rule_type}")
    
    @classmethod
    def list_available_rules(cls) -> list[str]:
        """列出所有可用的规则类型
        
        Returns:
            List[str]: 规则类型列表
        """
        return list(cls._rule_registry.keys())
    
    @classmethod
    def get_rule_class(cls, rule_type: str) -> type[BaseSecurityRule]:
        """获取规则类
        
        Args:
            rule_type: 规则类型
            
        Returns:
            Type[BaseSecurityRule]: 规则类
            
        Raises:
            ValueError: 如果规则类型未注册
        """
        rule_class = cls._rule_registry.get(rule_type)
        
        if not rule_class:
            raise ValueError(
                f"未知的规则类型: {rule_type}。"
                f"已注册的类型: {list(cls._rule_registry.keys())}"
            )
        
        return rule_class
