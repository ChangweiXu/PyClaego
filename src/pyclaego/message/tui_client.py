"""基于 Textual 的 TUI 客户端 - 支持 Session 管理和消息广播"""

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, RichLog
from textual.containers import Vertical
import asyncio
import websockets
import json
from typing import Optional


class ChatInterface(Vertical):
    """聊天界面容器"""
    
    def compose(self) -> ComposeResult:
        """组件布局"""
        yield RichLog(id="chat_log", wrap=True, highlight=True, markup=True)
        yield Input(placeholder="输入消息...", id="message_input")


class PyClaegoCLI(App):
    """PyClaw-CC TUI 客户端 - 支持 Session 管理和实时消息广播"""
    
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
    
    def __init__(
        self, 
        server_url: str = "ws://127.0.0.1:8765",
        session_id: Optional[str] = None,
        user_id: str = "default_user"
    ):
        super().__init__()
        self.server_url = server_url
        self.requested_session_id = session_id
        self.user_id = user_id
        self.session_id: Optional[str] = None  # 实际分配的 Session ID
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.log_widget: Optional[RichLog] = None
        self.connected = False
        self.session_joined = False
        self._message_listener_task: Optional[asyncio.Task] = None
        
    def compose(self) -> ComposeResult:
        """组件构建"""
        yield Header()
        yield ChatInterface()
        yield Footer()
        
    async def on_mount(self) -> None:
        """应用启动时的初始化"""
        self.log_widget = self.query_one("#chat_log", RichLog)
        self.log_widget.write("[bold green]PyClaw-CC TUI 客户端已启动[/bold green]")
        self.log_widget.write(f"[dim]正在连接到服务器 {self.server_url}...[/dim]\n")
        
        # 连接到 WebSocket 服务器
        await self.connect_to_server()
        
        # 如果连接成功，加入 Session
        if self.connected:
            await self.join_session()
            # 启动后台消息监听器
            self._message_listener_task = asyncio.create_task(self._message_listener())
        
        # 聚焦输入框
        self.query_one("#message_input", Input).focus()
        
    async def connect_to_server(self) -> None:
        """连接到 Core WebSocket 服务器"""
        try:
            self.websocket = await websockets.connect(self.server_url)
            self.connected = True
            self.log_widget.write("[bold green]✓ 已连接到 Core 服务器[/bold green]\n")
        except Exception as e:
            self.connected = False
            self.log_widget.write(f"[bold red]✗ 连接失败: {e}[/bold red]")
            self.log_widget.write(f"[dim]请确保 Core 服务器已启动: python core_server.py[/dim]\n")
    
    async def join_session(self) -> None:
        """加入 Session"""
        if not self.connected or not self.websocket:
            return
        
        try:
            # 发送加入 Session 请求
            join_message = {
                "type": "join_session",
                "session_id": self.requested_session_id,
                "user_id": self.user_id
            }
            
            if self.requested_session_id:
                self.log_widget.write(f"[dim]正在加入 Session: {self.requested_session_id}...[/dim]")
            else:
                self.log_widget.write(f"[dim]正在创建新 Session...[/dim]")
            
            await self.websocket.send(json.dumps(join_message))
            
            # 接收响应（这是初始化阶段，可以同步等待）
            response_str = await self.websocket.recv()
            response = json.loads(response_str)
            
            if response.get("type") == "session_joined":
                self.session_id = response.get("session_id")
                self.session_joined = True
                is_new = response.get("is_new", False)
                workspace = response.get("workspace_path", "")
                
                if is_new:
                    self.log_widget.write(f"[bold green]✓ 已创建新 Session: {self.session_id}[/bold green]")
                else:
                    self.log_widget.write(f"[bold green]✓ 已加入 Session: {self.session_id}[/bold green]")
                
                self.log_widget.write(f"[dim]工作空间: {workspace}[/dim]")
                self.log_widget.write("[dim]现在可以发送消息了，Ctrl+C 退出[/dim]\n")
            
            elif response.get("type") == "error":
                self.log_widget.write(f"[bold red]✗ 加入失败: {response.get('content')}[/bold red]\n")
                self.session_joined = False
                
        except Exception as e:
            self.log_widget.write(f"[bold red]✗ 加入 Session 失败: {e}[/bold red]\n")
            self.session_joined = False
    
    async def _message_listener(self) -> None:
        """后台消息监听器 - 持续接收来自服务器的消息（包括广播）"""
        if not self.websocket:
            return
        
        try:
            async for message_str in self.websocket:
                try:
                    message = json.loads(message_str)
                    await self._handle_server_message(message)
                except json.JSONDecodeError as e:
                    self.log_widget.write(f"[bold red]消息解析错误:[/bold red] {e}\n")
                except Exception as e:
                    self.log_widget.write(f"[bold red]消息处理错误:[/bold red] {e}\n")
                    
        except websockets.exceptions.ConnectionClosed:
            self.connected = False
            self.session_joined = False
            self.log_widget.write("[bold red]✗ 与服务器的连接已断开[/bold red]\n")
        except Exception as e:
            self.log_widget.write(f"[bold red]监听器错误:[/bold red] {e}\n")
    
    async def _handle_server_message(self, message: dict) -> None:
        """处理来自服务器的消息
        
        Args:
            message: 服务器消息
        """
        msg_type = message.get("type")
        
        if msg_type == "response":
            # 普通响应消息
            content = message.get("content", "")
            self.log_widget.write(f"[bold yellow]Session:[/bold yellow] {content}\n")
            
        elif msg_type == "error":
            # 错误消息
            content = message.get("content", "")
            self.log_widget.write(f"[bold red]错误:[/bold red] {content}\n")
            
        elif msg_type == "broadcast":
            # 广播消息（来自其他用户）
            content = message.get("content", "")
            self.log_widget.write(f"[bold magenta]广播:[/bold magenta] {content}\n")
            
        else:
            # 未知消息类型
            self.log_widget.write(f"[dim]收到消息: {message}[/dim]\n")
        
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """处理用户输入 - 只发送，不等待响应"""
        message = event.value.strip()
        
        if not message:
            return
            
        # 清空输入框
        event.input.value = ""
        
        # 检查连接状态
        if not self.connected or not self.websocket:
            self.log_widget.write("[bold red]错误:[/bold red] 未连接到服务器\n")
            return
        
        # 检查是否已加入 Session
        if not self.session_joined:
            self.log_widget.write("[bold red]错误:[/bold red] 未加入 Session\n")
            return
        
        # 显示用户消息
        self.log_widget.write(f"[bold cyan]你:[/bold cyan] {message}")
        
        # 发送到 Core 服务器（不等待响应，由后台监听器接收）
        try:
            await self.websocket.send(json.dumps({
                "type": "user_message",
                "content": message,
                "user_id": self.user_id
            }))
        except websockets.exceptions.ConnectionClosed:
            self.connected = False
            self.session_joined = False
            self.log_widget.write("[bold red]错误:[/bold red] 与服务器的连接已断开\n")
        except Exception as e:
            self.log_widget.write(f"[bold red]错误:[/bold red] {str(e)}\n")
            
    async def on_unmount(self) -> None:
        """应用关闭时清理"""
        # 取消后台任务
        if self._message_listener_task and not self._message_listener_task.done():
            self._message_listener_task.cancel()
            try:
                await self._message_listener_task
            except asyncio.CancelledError:
                pass
        
        # 关闭 WebSocket 连接
        if self.websocket:
            await self.websocket.close()


class TUIClient:
    """TUI 客户端封装"""
    
    def __init__(
        self, 
        server_url: str = "ws://127.0.0.1:8765",
        session_id: Optional[str] = None,
        user_id: str = "default_user"
    ):
        self.app = PyClaegoCLI(
            server_url=server_url,
            session_id=session_id,
            user_id=user_id
        )
        
    async def start(self) -> None:
        """启动 TUI 客户端"""
        await self.app.run_async()
        
    def run(self) -> None:
        """同步方式启动（阻塞）"""
        self.app.run()


async def main():
    """主函数"""
    import sys
    
    print("\n" + "="*60)
    print("  PyClaw-CC TUI 客户端 (Session Mode)")
    print("="*60 + "\n")
    
    # 解析命令行参数
    session_id = None
    if len(sys.argv) > 1:
        session_id = sys.argv[1]
        print(f"使用指定的 Session ID: {session_id}\n")
    else:
        print("未指定 Session ID，将创建新 Session\n")
    
    client = TUIClient(
        server_url="ws://127.0.0.1:8765",
        session_id=session_id,
        user_id="default_user"
    )
    
    try:
        await client.start()
    except KeyboardInterrupt:
        print("\n\n用户中断...")
    finally:
        print("\nTUI 客户端已退出。\n")


if __name__ == "__main__":
    asyncio.run(main())
