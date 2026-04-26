
from dataclasses import dataclass, field, asdict
import json
from pathlib import Path
from typing import Dict, List, Optional, Any

from filelock import FileLock

from .base_cache import CacheEntry, BaseCacheState
from ....logging import get_running_log

_rlog = get_running_log()


class JsonCacheState(BaseCacheState):
    """JSON 格式的缓存状态管理"""
    
    STATE_FILENAME = "cache_state.json"
    LOCK_FILENAME = "cache_state.lock"
    
    def __init__(self, cache_dir: Path):
        super().__init__(cache_dir)
        self.state_file = self.cache_dir / self.STATE_FILENAME
        self.lock_file = self.cache_dir / self.LOCK_FILENAME
        self._entries: Dict[str, CacheEntry] = {}
        self.load()
    
    def _acquire_lock(self) -> FileLock:
        """获取文件锁"""
        return FileLock(str(self.lock_file), timeout=10)
    
    def get_entry(self, url: str) -> Optional[CacheEntry]:
        """获取 URL 对应的缓存条目"""
        return self._entries.get(url)
    
    def set_entry(self, url: str, entry: CacheEntry):
        """设置 URL 对应的缓存条目"""
        self._entries[url] = entry
    
    def remove_entry(self, url: str):
        """移除 URL 对应的缓存条目"""
        if url in self._entries:
            del self._entries[url]
    
    def get_all_entries(self) -> Dict[str, CacheEntry]:
        """获取所有缓存条目"""
        return self._entries.copy()
    
    def save(self):
        """持久化状态到 JSON 文件"""
        with self._acquire_lock():
            data = {
                url: asdict(entry) 
                for url, entry in self._entries.items()
            }
            try:
                # 先写入临时文件，再原子性重命名
                temp_file = self.state_file.with_suffix('.tmp')
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                temp_file.replace(self.state_file)
            except Exception as e:
                _rlog.error("core_service", f"保存缓存状态失败: {e}")
                raise
    
    def load(self):
        """从 JSON 文件加载状态"""
        with self._acquire_lock():
            if not self.state_file.exists():
                self._entries = {}
                return
            
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                self._entries = {
                    url: CacheEntry(**entry_data)
                    for url, entry_data in data.items()
                }
            except json.JSONDecodeError as e:
                _rlog.error("core_service", f"缓存状态文件损坏，将重置: {e}")
                self._entries = {}
            except Exception as e:
                _rlog.error("core_service", f"加载缓存状态失败: {e}")
                self._entries = {}
