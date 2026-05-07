"""WidgetHook —— 每个 WidgetClass 可选的 Python 扩展点（Phase 11）。

约定：在 ``widget_classes/<class_id>/widget_class.py`` 中定义一个名为
``WidgetHook`` 的 *类*（不是实例），继承 :class:`WidgetHook`。
``WidgetClassRegistry`` 在加载阶段会把它读出来挂到对应 ``WidgetClassSpec``
的 ``hook_class`` 字段；``Widget.load()`` 会实例化并依次调用
``on_create`` 等生命周期方法。

MVP：没有任何 builtin class 提供 hook —— 所有方法默认 no-op。
本类的存在只是把扩展点钉死，未来可在不改框架代码的前提下扩展 widget 行为。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import APIRouter

from ...logging import get_running_log

_rlog = get_running_log()


class WidgetHook:
    """所有 WidgetClass 自定义扩展点的基类（默认全 no-op）。

    生命周期顺序（典型）：

    1. ``on_create(widget)`` —— Widget.load() 完成基本组件构造后
    2. ``on_chat(widget, message, response)`` —— 每条 chat 处理完之后
    3. ``on_cron(widget, trigger, message, response)`` —— 每次 cron 触发后
    4. ``on_destroy(widget)`` —— Widget.unload() 早期

    ``compute_highlight(widget) -> dict`` 用于 Dashboard 卡片摘要展示，
    返回任何可 JSON 序列化的小字典（默认空）。
    """

    def __init__(self, widget: Any) -> None:
        self.widget = widget

    # ------------------------------------------------------------------
    # 生命周期 hook —— 默认全部 no-op
    # ------------------------------------------------------------------

    async def on_create(self) -> None:  # pragma: no cover - default no-op
        return None

    async def on_chat(
        self, message: dict[str, Any], response: dict[str, Any] | None = None
    ) -> None:  # pragma: no cover - default no-op
        return None

    async def on_cron(
        self,
        trigger_id: str,
        message: dict[str, Any],
        response: dict[str, Any] | None = None,
    ) -> None:  # pragma: no cover - default no-op
        return None

    async def on_destroy(self) -> None:  # pragma: no cover - default no-op
        return None

    def compute_highlight(self) -> dict[str, Any]:
        """供 Dashboard 卡片渲染的摘要数据（默认空）。"""
        return {}

    # ------------------------------------------------------------------
    # 路由注册 —— 类方法，让 WidgetClass 向全局 router 注册自定义端点
    # ------------------------------------------------------------------

    @classmethod
    def register_routes(cls, router: APIRouter) -> None:
        """向 FastAPI router 注册本 WidgetClass 的自定义端点（默认 no-op）。

        子类覆盖此方法，把自己的 APIRouter include 进来：

        .. code-block:: python

            @classmethod
            def register_routes(cls, router):
                from .routes import my_router
                router.include_router(my_router)

        此方法在 web_server 启动阶段由 ``register_all_widget_routes(app)``
        调用一次，传入已挂在 ``/api/v2`` 前缀下的 APIRouter。
        """
        return None


__all__ = ["WidgetHook"]
