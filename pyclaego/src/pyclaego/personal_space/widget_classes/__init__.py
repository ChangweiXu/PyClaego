"""WidgetClass 子模块。"""

from .hook import WidgetHook
from .registry import WidgetClassRegistry
from .spec import WidgetClassSpec

__all__ = ["WidgetClassRegistry", "WidgetClassSpec", "WidgetHook"]
