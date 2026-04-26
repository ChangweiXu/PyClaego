"""缓存状态管理"""

import hashlib
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

from ....logging import get_running_log

_rlog = get_running_log()


@dataclass
class CacheEntry:
    """单个 URL 的缓存条目"""
    url: str
    file_paths: List[str] = field(default_factory=list)  # 路径列表，最新的在末尾
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    content_hashes: List[str] = field(default_factory=list)  # 与 file_paths 一一对应的内容 hash
    
    def get_latest_path(self) -> Optional[str]:
        """获取最新的缓存文件路径"""
        return self.file_paths[-1] if self.file_paths else None
    
    def get_latest_hash(self) -> Optional[str]:
        """获取最新的内容 hash"""
        return self.content_hashes[-1] if self.content_hashes else None
    
    def add_cache_file(self, file_path: str, content_hash: str):
        """添加新的缓存文件"""
        self.file_paths.append(file_path)
        self.content_hashes.append(content_hash)
        self.last_accessed_at = time.time()
    
    def is_expired(self, ttl: Optional[int]) -> bool:
        """检查缓存是否过期"""
        if ttl is None:
            return False
        return time.time() - self.created_at > ttl
    
    def has_content(self, content_hash: str) -> bool:
        """检查是否已存在相同内容"""
        return content_hash in self.content_hashes


class BaseCacheState(ABC):
    """缓存状态抽象基类"""
    
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    @abstractmethod
    def get_entry(self, url: str) -> Optional[CacheEntry]:
        """获取 URL 对应的缓存条目"""
        pass
    
    @abstractmethod
    def set_entry(self, url: str, entry: CacheEntry):
        """设置 URL 对应的缓存条目"""
        pass
    
    @abstractmethod
    def remove_entry(self, url: str):
        """移除 URL 对应的缓存条目"""
        pass
    
    @abstractmethod
    def get_all_entries(self) -> Dict[str, CacheEntry]:
        """获取所有缓存条目"""
        pass
    
    @abstractmethod
    def save(self):
        """持久化状态"""
        pass
    
    @abstractmethod
    def load(self):
        """加载状态"""
        pass
    
    def cleanup_expired(self, ttl: Optional[int]) -> int:
        """清理过期缓存，返回清理的条目数"""
        if ttl is None:
            return 0
        
        entries = self.get_all_entries()
        removed_count = 0
        
        for url, entry in list(entries.items()):
            if entry.is_expired(ttl):
                # 删除所有关联的缓存文件
                for file_path in entry.file_paths:
                    try:
                        path = Path(file_path)
                        if path.exists():
                            path.unlink()
                            _rlog.debug("core_service", f"删除过期缓存文件: {file_path}")
                    except Exception as e:
                        _rlog.warning("core_service", f"删除缓存文件失败: {file_path}, 错误: {e}")
                
                self.remove_entry(url)
                removed_count += 1
                _rlog.info("core_service", f"清理过期缓存: {url}")
        
        if removed_count > 0:
            self.save()
        
        return removed_count
    
    @staticmethod
    def url_to_key(url: str, method: str = "hash_md5") -> str:
        """将 URL 转换为缓存键/文件名"""
        if method == "hash_md5":
            return hashlib.md5(url.encode('utf-8')).hexdigest()
        elif method == "hash_sha256":
            return hashlib.sha256(url.encode('utf-8')).hexdigest()
        else:
            raise ValueError(f"不支持的 hash 方法: {method}")
    
    @staticmethod
    def content_hash(content: str, method: str = "hash_md5") -> str:
        """计算内容 hash"""
        if method == "hash_md5":
            return hashlib.md5(content.encode('utf-8')).hexdigest()
        elif method == "hash_sha256":
            return hashlib.sha256(content.encode('utf-8')).hexdigest()
        else:
            raise ValueError(f"不支持的 hash 方法: {method}")
