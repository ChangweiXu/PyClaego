
from pathlib import Path

from .base_cache import BaseCacheState, CacheEntry
from .json_cache import JsonCacheState


def create_cache_state(cache_format: str, cache_dir: Path) -> BaseCacheState:
    """工厂函数：根据格式创建缓存状态管理器"""
    if cache_format == "json":
        return JsonCacheState(cache_dir)
    # 未来可以添加其他格式
    # elif cache_format == "sqlite":
    #     return SqliteCacheState(cache_dir)
    else:
        raise ValueError(f"不支持的缓存格式: {cache_format}")


__all__ = [
    "BaseCacheState",
    "CacheEntry",
    "create_cache_state",
    "JsonCacheState",
]
