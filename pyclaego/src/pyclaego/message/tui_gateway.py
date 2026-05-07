"""基于 Textual 的 TUI 界面"""

from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog


class ChatInterface(Vertical):
    """聊天界面容器"""
    
    def compose(self) -> ComposeResult:
        """组件布局"""
        yield RichLog(id="chat_log", wrap=True, highlight=True, markup=True)
        yield Input(placeholder="输入消息...", id="message_input")


class PyClaegoCLI(App):
    """PyClaego TUI 应用 - MVP 版本"""
    
    CSS = """
    #chat_log {
        height: 1fr;
        border: solid $primary;
        background: $surface;
    }
    
    #message_input {
        dock: bottom;
        height: 3;
    }
    """
    
    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("ctrl+d", "quit", "退出"),
    ]
    
    def __init__(self, scheduler_callback: Callable):
        super().__init__()
        self.scheduler_callback = scheduler_callback
        self.log_widget: RichLog | None = None
        
    def compose(self) -> ComposeResult:
        """组件构建"""
        yield Header()
        yield ChatInterface()
        yield Footer()
        
    def on_mount(self) -> None:
        """应用启动时的初始化"""
        self.log_widget = self.query_one("#chat_log", RichLog)
        self.log_widget.write("[bold green]PyClaego TUI 已启动[/bold green]")
        self.log_widget.write("[dim]输入消息并按 Enter 发送，Ctrl+C 退出[/dim]\n")
        
        # 聚焦输入框
        self.query_one("#message_input", Input).focus()
        
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """处理用户输入"""
        message = event.value.strip()
        
        if not message:
            return
            
        # 清空输入框
        event.input.value = ""
        
        # 显示用户消息
        self.log_widget.write(f"[bold cyan]你:[/bold cyan] {message}")
        
        # 发送到核心调度器
        try:
            response = await self.scheduler_callback({
                "type": "user_message",
                "content": message
            })
            
            # 显示响应
            if response:
                self.log_widget.write(
                    f"[bold yellow]系统:[/bold yellow] {response.get('content', '')}\n"
                )
        except Exception as e:
            self.log_widget.write(f"[bold red]错误:[/bold red] {e!s}\n")


class TUIGateway:
    """TUI 消息网关 - MVP 版本"""
    
    def __init__(self, scheduler_callback: Callable):
        self.app = PyClaegoCLI(scheduler_callback)
        
    async def start(self) -> None:
        """启动 TUI 应用"""
        await self.app.run_async()
        
    def run(self) -> None:
        """同步方式启动（阻塞）"""
        self.app.run()
