"""PersonalSpace 模块（取代 ``src/session/``）。

公开 API：
- ``PersonalSpace`` — PS 运行时
- ``PersonalSpaceManager`` — 进程级单例 + LRU
- ``PSManifest`` / ``WidgetManifest`` / ``WidgetLayout`` — 磁盘数据类
- ``Widget`` — Widget 运行时（Phase 1.1 骨架）
- ``DEFAULT_WIDGET_ID`` / ``DEFAULT_WIDGET_CLASS`` — 自动配置常量
"""

from .manager import PersonalSpaceManager
from .models import PSManifest, WidgetLayout, WidgetManifest
from .personal_space import (
    DEFAULT_WIDGET_CLASS,
    DEFAULT_WIDGET_ID,
    PersonalSpace,
)
from .stream_state import StreamStateManager
from .view_schema import (
    CommandResult,
    ViewSchema,
    WidgetCommand,
)
from .widget import Widget
from .widget_classes import WidgetClassRegistry, WidgetClassSpec, WidgetHook

__all__ = [
    "DEFAULT_WIDGET_CLASS",
    "DEFAULT_WIDGET_ID",
    "CommandResult",
    "PSManifest",
    "PersonalSpace",
    "PersonalSpaceManager",
    "StreamStateManager",
    "ViewSchema",
    "Widget",
    "WidgetClassRegistry",
    "WidgetClassSpec",
    "WidgetCommand",
    "WidgetHook",
    "WidgetLayout",
    "WidgetManifest",
]
