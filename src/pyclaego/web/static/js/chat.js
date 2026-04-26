/**
 * PyClaw-CC WebSocket 客户端 - 增强版
 * 功能：Markdown 渲染、代码高亮、多 Session 管理、消息持久化
 */

class ChatClient {
    constructor() {
        this.ws = null;
        this.sessionId = null;
        this.connected = false;
        
        // DOM 元素引用
        this.sessionIdInput = document.getElementById('session-id');
        this.connectBtn = document.getElementById('connect-btn');
        this.statusSpan = document.getElementById('status');
        this.messagesDiv = document.getElementById('chat-messages');
        this.messageInput = document.getElementById('message-input');
        this.sendBtn = document.getElementById('send-btn');
        this.attachBtn = document.getElementById('attach-btn');
        this.imageFileInput = document.getElementById('image-file-input');
        this.attachPreview = document.getElementById('attach-preview');

        // 待发送的文件附件（ImagePart dict 列表）
        this._pendingAttachments = [];
        
        // 侧边栏元素
        this.sidebar = document.getElementById('sidebar');
        this.sessionList = document.getElementById('session-list');
        this.refreshSessionsBtn = document.getElementById('refresh-sessions-btn');
        this.toggleSidebarBtn = document.getElementById('toggle-sidebar-btn');
        this.showSidebarBtn = document.getElementById('show-sidebar-btn');
        
        // 滚动到底部按钮
        this.scrollBottomBtn = document.getElementById('scroll-bottom-btn');
        
        // 配置 Marked.js
        if (typeof marked !== 'undefined') {
            marked.setOptions({
                breaks: true,
                gfm: true,
                highlight: function(code, lang) {
                    if (typeof hljs !== 'undefined' && lang && hljs.getLanguage(lang)) {
                        try {
                            return hljs.highlight(code, { language: lang }).value;
                        } catch (e) {}
                    }
                    return code;
                }
            });
        }

        // 初始化 EasyMDE Markdown 编辑器
        this.easyMDE = new EasyMDE({
            element: this.messageInput,
            autofocus: false,
            placeholder: '输入消息... (Cmd+Enter 发送)',
            spellChecker: false,
            status: false,
            toolbar: [
                'bold', 'italic', 'heading', '|',
                'quote', 'code', 'link', '|',
                'unordered-list', 'ordered-list', '|',
                'preview', 'side-by-side',
            ],
            initialValue: '',
        });
        // 直接监听 CodeMirror 键盘事件，比 extraKeys 更可靠
        this.easyMDE.codemirror.on('keydown', (cm, e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                e.preventDefault();
                this.sendMessage();
            }
        });
        // 初始状态：未连接时禁用编辑器
        this._setEditorEnabled(false);

        // 初始化输入区顶部拖拽缩放
        this._initResizeHandle();

        // 修正 side-by-side 预览定位（测量工具栏高度）
        this._initSideBySideFix();
        
        // 绑定事件
        this.bindEvents();
        
        // 加载 Session 列表
        this.loadSessions();
        
        // 尝试加载最后使用的 Session
        this.loadLastSession();
    }
    
    /**
     * 绑定 UI 事件
     */
    bindEvents() {
        // 连接按钮
        this.connectBtn.addEventListener('click', () => this.connect());
        
        // 发送按钮
        this.sendBtn.addEventListener('click', () => this.sendMessage());

        // EasyMDE 已通过 extraKeys 处理 Cmd+Enter / Ctrl+Enter，无需额外监听
        
        // 图片附件按钮
        if (this.attachBtn) {
            this.attachBtn.addEventListener('click', () => this.imageFileInput && this.imageFileInput.click());
        }
        if (this.imageFileInput) {
            this.imageFileInput.addEventListener('change', (e) => this._handleImageFiles(e.target.files));
        }
        
        // Session ID 输入框回车
        this.sessionIdInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.connect();
            }
        });
        
        // 侧边栏控制
        this.refreshSessionsBtn.addEventListener('click', () => this.loadSessions());
        this.toggleSidebarBtn.addEventListener('click', () => this.toggleSidebar());
        this.showSidebarBtn.addEventListener('click', () => this.showSidebar());
        
        // 滚动到底部按钮
        this.scrollBottomBtn.addEventListener('click', () => this.scrollToBottom(true));
        this.messagesDiv.addEventListener('scroll', () => this._updateScrollBtnVisibility());
    }
    
    /**
     * 加载 Session 列表（基于 LocalStorage）
     */
    async loadSessions() {
        try {
            this.sessionList.innerHTML = '<div class="session-list-loading">加载中...</div>';
            
            // 从 LocalStorage 获取所有 chat_history_* 键
            const sessions = [];
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                if (key && key.startsWith('chat_history_')) {
                    const sessionId = key.replace('chat_history_', '');
                    
                    // 尝试获取消息数量和最后活动时间
                    try {
                        const historyJson = localStorage.getItem(key);
                        const messages = JSON.parse(historyJson);
                        const messageCount = messages ? messages.length : 0;
                        
                        // 获取最后使用时间（从单独的 timestamp 键）
                        const timestampKey = `session_timestamp_${sessionId}`;
                        const timestamp = localStorage.getItem(timestampKey);
                        
                        sessions.push({
                            session_id: sessionId,
                            message_count: messageCount,
                            last_used: timestamp ? new Date(parseInt(timestamp)) : null,
                            created_at: timestamp ? new Date(parseInt(timestamp)).toISOString() : null
                        });
                    } catch (e) {
                        // 如果解析失败，仍然添加这个 Session
                        sessions.push({
                            session_id: sessionId,
                            message_count: 0,
                            last_used: null,
                            created_at: null
                        });
                    }
                }
            }
            
            // 按最后使用时间倒序排列
            sessions.sort((a, b) => {
                if (!a.last_used) return 1;
                if (!b.last_used) return -1;
                return b.last_used - a.last_used;
            });
            
            if (sessions.length > 0) {
                this.sessionList.innerHTML = '';
                sessions.forEach(session => {
                    this.addSessionItem(session);
                });
            } else {
                this.sessionList.innerHTML = '<div class="session-list-loading">暂无历史会话</div>';
            }
        } catch (error) {
            console.error('Failed to load sessions:', error);
            this.sessionList.innerHTML = '<div class="session-list-loading">加载失败</div>';
        }
    }
    
    /**
     * 添加 Session 列表项
     */
    addSessionItem(session) {
        const item = document.createElement('div');
        item.className = 'session-item';
        if (session.session_id === this.sessionId) {
            item.classList.add('active');
        }
        
        const title = document.createElement('div');
        title.className = 'session-item-title';
        title.textContent = session.session_id;
        
        const info = document.createElement('div');
        info.className = 'session-item-info';
        
        // 格式化时间
        let timeStr = '未知';
        if (session.last_used) {
            const now = new Date();
            const diff = now - session.last_used;
            const minutes = Math.floor(diff / 60000);
            const hours = Math.floor(diff / 3600000);
            const days = Math.floor(diff / 86400000);
            
            if (minutes < 1) {
                timeStr = '刚刚';
            } else if (minutes < 60) {
                timeStr = `${minutes}分钟前`;
            } else if (hours < 24) {
                timeStr = `${hours}小时前`;
            } else if (days < 7) {
                timeStr = `${days}天前`;
            } else {
                timeStr = session.last_used.toLocaleString('zh-CN', { 
                    month: '2-digit', 
                    day: '2-digit'
                });
            }
        } else if (session.created_at) {
            timeStr = new Date(session.created_at).toLocaleString('zh-CN', { 
                month: '2-digit', 
                day: '2-digit', 
                hour: '2-digit', 
                minute: '2-digit' 
            });
        }
        
        // 显示消息数量和时间
        const messageCountStr = session.message_count ? `${session.message_count}条` : '';
        info.innerHTML = `<span>${timeStr}</span><span>${messageCountStr}</span>`;
        
        item.appendChild(title);
        item.appendChild(info);
        
        // 点击切换 Session
        item.addEventListener('click', () => {
            if (this.connected && session.session_id === this.sessionId) {
                return; // 已连接到当前 Session
            }
            
            // 断开当前连接
            if (this.connected) {
                this.ws.close();
            }
            
            // 切换到新 Session
            this.sessionIdInput.value = session.session_id;
            this.connect();
        });
        
        this.sessionList.appendChild(item);
    }
    
    /**
     * 切换侧边栏
     */
    toggleSidebar() {
        this.sidebar.classList.toggle('hidden');
        this.toggleSidebarBtn.textContent = this.sidebar.classList.contains('hidden') 
            ? '▶ 显示' 
            : '◀ 隐藏';
    }
    
    /**
     * 显示侧边栏
     */
    showSidebar() {
        this.sidebar.classList.remove('hidden');
        this.toggleSidebarBtn.textContent = '◀ 隐藏';
    }
    
    /**
     * 加载最后使用的 Session
     */
    loadLastSession() {
        const lastSessionId = localStorage.getItem('last_session_id');
        if (lastSessionId) {
            this.sessionIdInput.value = lastSessionId;
        }
    }
    
    /**
     * 保存最后使用的 Session
     */
    saveLastSession() {
        if (this.sessionId) {
            localStorage.setItem('last_session_id', this.sessionId);
            // 更新时间戳
            localStorage.setItem(`session_timestamp_${this.sessionId}`, Date.now().toString());
        }
    }
    
    /**
     * 连接到 WebSocket 服务器
     */
    connect() {
        // 如果已连接，先断开
        if (this.connected && this.ws) {
            this.ws.close();
            return;
        }
        
        // 获取 Session ID（留空则自动生成）
        this.sessionId = this.sessionIdInput.value.trim() || this.generateSessionId();
        this.sessionIdInput.value = this.sessionId;
        
        // 验证 Session ID 格式
        if (!this.validateSessionId(this.sessionId)) {
            this.addErrorMessage('Session ID 格式错误！只能包含小写字母、数字和下划线，且必须以小写字母或下划线开头。');
            return;
        }
        
        // 保存为最后使用的 Session
        this.saveLastSession();
        
        // 构建 WebSocket URL
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = window.location.host;
        const wsUrl = `${protocol}//${host}/chat/${this.sessionId}`;
        
        this.updateStatus('连接中...', 'connecting');
        this.addSystemMessage(`正在连接到 Session: ${this.sessionId}`);
        
        // 清空欢迎消息
        this.clearWelcomeMessage();
        
        // 加载历史消息
        this.loadMessageHistory();
        
        // 建立 WebSocket 连接
        try {
            this.ws = new WebSocket(wsUrl);
            
            this.ws.onopen = () => this.onOpen();
            this.ws.onmessage = (event) => this.onMessage(event);
            this.ws.onerror = (error) => this.onError(error);
            this.ws.onclose = (event) => this.onClose(event);
        } catch (error) {
            this.addErrorMessage(`连接失败: ${error.message}`);
            this.updateStatus('未连接', 'disconnected');
        }
    }
    
    /**
     * 接管 EasyMDE 内置 side-by-side 按钮，改用自己的实现，
     * 避免 EasyMDE 向 DOM 注入 position:fixed 等内联样式破坏布局。
     */
    _initSideBySideFix() {
        const container = this.easyMDE.codemirror.getWrapperElement()
            .closest('.EasyMDEContainer');
        if (!container) return;
        const toolbar = container.querySelector('.editor-toolbar');
        if (!toolbar) return;

        // 测量工具栏高度写入 CSS 变量，供预览面板定位使用
        requestAnimationFrame(() => {
            const h = toolbar.getBoundingClientRect().height;
            container.style.setProperty('--easymde-toolbar-h', h + 'px');
        });

        // 等 EasyMDE 完成工具栏渲染后再替换按钮
        requestAnimationFrame(() => {
            // EasyMDE 的分栏按钮带有 fa-columns 类
            const origBtn = toolbar.querySelector('button.fa-columns')
                || Array.from(toolbar.querySelectorAll('button'))
                    .find(b => b.className.includes('columns')
                        || (b.title && b.title.toLowerCase().includes('side')));
            if (!origBtn) return;

            const preview = container.querySelector('.editor-preview-side');
            const wrapper = this.easyMDE.codemirror.getWrapperElement();
            if (!preview) return;

            // 克隆按钮以移除 EasyMDE 原有的事件监听
            const newBtn = origBtn.cloneNode(true);
            origBtn.replaceWith(newBtn);

            let changeHandler = null;

            const updatePreview = () => {
                try {
                    const html = marked.parse(this.easyMDE.value() || '');
                    preview.innerHTML = typeof DOMPurify !== 'undefined'
                        ? DOMPurify.sanitize(html) : html;
                } catch (e) { /* ignore */ }
            };

            newBtn.addEventListener('click', () => {
                const isActive = preview.classList.contains('editor-preview-active-side');
                if (isActive) {
                    // 关闭分栏
                    preview.classList.remove('editor-preview-active-side');
                    wrapper.classList.remove('CodeMirror-sided');
                    newBtn.classList.remove('active');
                    if (changeHandler) {
                        this.easyMDE.codemirror.off('change', changeHandler);
                        changeHandler = null;
                    }
                    // 清除 EasyMDE 可能曾经设置过的内联样式
                    preview.style.cssText = '';
                    wrapper.style.height = '';
                } else {
                    // 开启分栏：先渲染内容，再加类（避免空白闪烁）
                    updatePreview();
                    preview.classList.add('editor-preview-active-side');
                    wrapper.classList.add('CodeMirror-sided');
                    newBtn.classList.add('active');
                    changeHandler = () => updatePreview();
                    this.easyMDE.codemirror.on('change', changeHandler);
                }
            });
        });
    }

    /**
     * 初始化输入区顶部拖拽缩放句柄
     */
    _initResizeHandle() {
        const handle = document.querySelector('.input-resize-handle');
        if (!handle) return;
        const inputArea = document.querySelector('.input-area');
        if (!inputArea) return;

        let startY, startHeight;

        const onMouseMove = (e) => {
            const delta = startY - e.clientY; // 向上拖 → 增大高度
            const newHeight = Math.max(140, Math.min(600, startHeight + delta));
            inputArea.style.height = newHeight + 'px';
        };

        const onMouseUp = () => {
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        };

        handle.addEventListener('mousedown', (e) => {
            startY = e.clientY;
            startHeight = inputArea.getBoundingClientRect().height;
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
            e.preventDefault();
        });
    }

    /**
     * 启用或禁用 Markdown 编辑器
     */
    _setEditorEnabled(enabled) {
        const cm = this.easyMDE.codemirror;
        cm.setOption('readOnly', enabled ? false : 'nocursor');
        const container = cm.getWrapperElement().closest('.EasyMDEContainer');
        if (container) {
            container.classList.toggle('editor-disabled', !enabled);
        }
    }

    /**
     * WebSocket 连接成功
     */
    onOpen() {
        this.connected = true;
        this.updateStatus('已连接', 'connected');
        this._setEditorEnabled(true);
        this.sendBtn.disabled = false;
        if (this.attachBtn) this.attachBtn.disabled = false;
        this.connectBtn.textContent = '断开';
        this.sessionIdInput.disabled = true;
        this.addSystemMessage('✓ WebSocket 连接成功，等待 Session 确认...');
        
        // 刷新 Session 列表
        this.loadSessions();
    }
    
    /**
     * 接收 WebSocket 消息
     */
    onMessage(event) {
        try {
            const data = JSON.parse(event.data);
            console.log('Received message:', data);
            
            switch (data.type) {
                case 'session_joined':
                    this.addSystemMessage(`✓ 已加入 Session: ${data.session_id}`);
                    if (data.is_new) {
                        this.addSystemMessage('📝 这是一个新会话');
                    } else {
                        this.addSystemMessage('📂 已加载历史会话');
                    }
                    break;
                    
                case 'response':
                    this.addMessage('agent', data.content);
                    break;

                case 'cron_response': {
                    // Cron 触发的响应：作为普通 agent 消息渲染，但在顶部加一行身份标识
                    const cronName = data.cron_name || 'unknown';
                    const firedAt = data.fired_at
                        ? new Date(data.fired_at).toLocaleString()
                        : '';
                    const header = `📅 **[cron: ${cronName}]** ${firedAt}`;
                    const body = data.content || '';
                    this.addMessage('agent', `${header}\n\n${body}`);
                    break;
                }

                case 'progress_update':
                    this.addProgressLog(data.content);
                    break;

                case 'command_response':
                    this.addMessage('command-response', data.content);
                    break;
                    
                case 'error':
                    this.addErrorMessage(data.content);
                    break;
                    
                case 'broadcast':
                    this.addSystemMessage(`📢 广播: ${data.content}`);
                    break;
                    
                default:
                    console.warn('Unknown message type:', data);
                    this.addSystemMessage(`未知消息类型: ${data.type}`);
            }
        } catch (error) {
            console.error('Failed to parse message:', error);
            this.addErrorMessage(`消息解析失败: ${error.message}`);
        }
    }
    
    /**
     * WebSocket 错误
     */
    onError(error) {
        console.error('WebSocket error:', error);
        this.addErrorMessage('WebSocket 连接错误');
    }
    
    /**
     * WebSocket 连接关闭
     */
    onClose(event) {
        this.connected = false;
        this.updateStatus('已断开', 'disconnected');
        this._setEditorEnabled(false);
        this.sendBtn.disabled = true;
        if (this.attachBtn) this.attachBtn.disabled = true;
        this.connectBtn.textContent = '连接';
        this.sessionIdInput.disabled = false;
        
        if (event.code === 1000) {
            this.addSystemMessage('✗ 连接已正常关闭');
        } else if (event.code === 1003) {
            this.addErrorMessage(`✗ 连接被拒绝: ${event.reason}`);
        } else {
            this.addSystemMessage(`✗ 连接已断开 (code: ${event.code})`);
        }
        
        // 保存消息历史
        this.saveMessageHistory();
        
        // 刷新 Session 列表
        this.loadSessions();
    }
    
    /**
     * 将选中的图片文件转为 base64 ImagePart 并加入待发送队列
     * 不支持的格式（AVIF、BMP 等）自动通过 canvas 转换为 JPEG。
     */
    _handleImageFiles(files) {
        if (!files || files.length === 0) return;
        // LLM 视觉 API 支持的 MIME 类型
        const SUPPORTED = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
        Array.from(files).forEach(file => {
            if (!file.type.startsWith('image/')) return;
            const reader = new FileReader();
            reader.onload = (e) => {
                const originalDataUrl = e.target.result;
                const finalize = (dataUrl, mediaType) => {
                    const [, data] = dataUrl.split(',');
                    this._pendingAttachments.push({
                        type: 'image',
                        source_type: 'base64',
                        data: data,
                        media_type: mediaType,
                        _name: file.name,
                    });
                    this._renderAttachPreview();
                };
                if (SUPPORTED.has(file.type)) {
                    finalize(originalDataUrl, file.type);
                } else {
                    // 通过 canvas 将不支持的格式（AVIF、BMP 等）转换为 JPEG
                    const img = new Image();
                    img.onload = () => {
                        const canvas = document.createElement('canvas');
                        canvas.width = img.naturalWidth;
                        canvas.height = img.naturalHeight;
                        canvas.getContext('2d').drawImage(img, 0, 0);
                        finalize(canvas.toDataURL('image/jpeg', 0.92), 'image/jpeg');
                    };
                    img.onerror = () => {
                        // 浏览器也无法解码时，降级发送原始数据
                        const mediaType = originalDataUrl.split(',')[0].replace('data:', '').replace(';base64', '');
                        finalize(originalDataUrl, mediaType);
                    };
                    img.src = originalDataUrl;
                }
            };
            reader.readAsDataURL(file);
        });
        // 重置 input 允许重复选择相同文件
        if (this.imageFileInput) this.imageFileInput.value = '';
    }

    /**
     * 显示预览区域中的附件列表
     */
    _renderAttachPreview() {
        if (!this.attachPreview) return;
        this.attachPreview.innerHTML = '';
        this._pendingAttachments.forEach((att, idx) => {
            const chip = document.createElement('span');
            chip.className = 'attach-chip';
            const img = document.createElement('img');
            img.src = `data:${att.media_type};base64,${att.data}`;
            img.title = att._name || '';
            const removeBtn = document.createElement('button');
            removeBtn.textContent = '×';
            removeBtn.addEventListener('click', () => {
                this._pendingAttachments.splice(idx, 1);
                this._renderAttachPreview();
            });
            chip.appendChild(img);
            chip.appendChild(removeBtn);
            this.attachPreview.appendChild(chip);
        });
        this.attachPreview.style.display = this._pendingAttachments.length > 0 ? 'flex' : 'none';
    }

    /**
     * 发送消息
     */
    sendMessage() {
        const content = this.easyMDE.value().trim();
        if ((!content && this._pendingAttachments.length === 0) || !this.connected) return;
        
        // 构建消息对象
        const message = {
            type: 'user_message',
            content: content,
            user_id: 'web_user',
            request_id: 'req_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9)
        };

        // 如果有附件，构建 content_parts
        if (this._pendingAttachments.length > 0) {
            const parts = [];
            if (content) {
                parts.push({ type: 'text', text: content });
            }
            this._pendingAttachments.forEach(att => {
                parts.push({
                    type: att.type,
                    source_type: att.source_type,
                    data: att.data,
                    media_type: att.media_type,
                });
            });
            message.content_parts = parts;
        }
        
        try {
            // 发送到 WebSocket
            this.ws.send(JSON.stringify(message));
            
            // 显示用户消息（如有图片附件则显示文字 + 图片预览）
            if (this._pendingAttachments.length > 0) {
                this._addMultimodalMessage(content, this._pendingAttachments);
            } else {
                this.addMessage('user', content);
            }
            this.scrollToBottom(true);
            
            // 清空输入框和附件
            this.easyMDE.value('');
            this._pendingAttachments = [];
            this._renderAttachPreview();
            this.easyMDE.codemirror.focus();
        } catch (error) {
            this.addErrorMessage(`发送失败: ${error.message}`);
        }
    }

    /**
     * 在聊天区显示含图片附件的用户消息
     */
    _addMultimodalMessage(text, attachments) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message user';
        const bubbleDiv = document.createElement('div');
        bubbleDiv.className = 'message-bubble';
        if (text) {
            const textNode = document.createElement('p');
            textNode.textContent = text;
            bubbleDiv.appendChild(textNode);
        }
        attachments.forEach(att => {
            const img = document.createElement('img');
            img.src = `data:${att.media_type};base64,${att.data}`;
            img.className = 'attach-preview-inline';
            bubbleDiv.appendChild(img);
        });
        const wrapperDiv = document.createElement('div');
        wrapperDiv.className = 'message-wrapper';
        wrapperDiv.appendChild(bubbleDiv);

        const copyBtn = document.createElement('button');
        copyBtn.className = 'btn-copy';
        copyBtn.textContent = '复制';
        copyBtn.addEventListener('click', () => {
            navigator.clipboard.writeText(text || '').then(() => {
                copyBtn.textContent = '已复制!';
                setTimeout(() => { copyBtn.textContent = '复制'; }, 1500);
            });
        });
        wrapperDiv.appendChild(copyBtn);

        messageDiv.appendChild(wrapperDiv);
        this.messagesDiv.appendChild(messageDiv);
        this.scrollToBottom();
        this.saveMessageHistory();
    }
    
    /**
     * 添加消息到聊天区（支持 Markdown 渲染）
     */
    addMessage(role, content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;
        
        const bubbleDiv = document.createElement('div');
        bubbleDiv.className = 'message-bubble';
        
        // 保存原始内容用于历史记录持久化
        bubbleDiv.dataset.rawContent = content;

        // Agent 消息使用 Markdown 渲染
        if (role === 'agent' && typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
            bubbleDiv.className += ' markdown-content';
            try {
                const rawHtml = marked.parse(content);
                const cleanHtml = DOMPurify.sanitize(rawHtml);
                bubbleDiv.innerHTML = cleanHtml;
                
                // 高亮代码块
                if (typeof hljs !== 'undefined') {
                    bubbleDiv.querySelectorAll('pre code').forEach((block) => {
                        hljs.highlightElement(block);
                    });
                }
            } catch (error) {
                console.error('Markdown rendering error:', error);
                bubbleDiv.textContent = content;
            }
        } else {
            bubbleDiv.textContent = content;
        }
        
        const wrapperDiv = document.createElement('div');
        wrapperDiv.className = 'message-wrapper';
        wrapperDiv.appendChild(bubbleDiv);

        const copyBtn = document.createElement('button');
        copyBtn.className = 'btn-copy';
        copyBtn.textContent = '复制';
        copyBtn.addEventListener('click', () => {
            navigator.clipboard.writeText(content).then(() => {
                copyBtn.textContent = '已复制!';
                setTimeout(() => { copyBtn.textContent = '复制'; }, 1500);
            });
        });
        wrapperDiv.appendChild(copyBtn);

        messageDiv.appendChild(wrapperDiv);
        this.messagesDiv.appendChild(messageDiv);
        
        // 滚动到底部
        this.scrollToBottom();
        
        // 保存消息历史
        this.saveMessageHistory();
    }
    
    /**
     * 添加系统消息
     */
    addSystemMessage(content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message system';
        
        const contentDiv = document.createElement('div');
        contentDiv.className = 'message-content';
        contentDiv.textContent = content;
        
        messageDiv.appendChild(contentDiv);
        this.messagesDiv.appendChild(messageDiv);
        
        this.scrollToBottom();
    }
    
    /**
     * 添加错误消息
     */
    addErrorMessage(content) {
        const messageDiv = document.createElement('div');
        messageDiv.className = 'message error';
        
        const bubbleDiv = document.createElement('div');
        bubbleDiv.className = 'message-bubble';
        bubbleDiv.textContent = '❌ ' + content;
        
        messageDiv.appendChild(bubbleDiv);
        this.messagesDiv.appendChild(messageDiv);
        
        this.scrollToBottom();
    }
    
    /**
     * 添加进度日志（追加到当前日志块，或创建新日志块）
     */
    addProgressLog(content) {
        // 查找最后一个子元素是否是 progress-log 块
        const lastChild = this.messagesDiv.lastElementChild;
        let logBlock;
        
        if (lastChild && lastChild.classList.contains('progress-log')) {
            logBlock = lastChild;
        } else {
            // 创建新的日志块
            logBlock = document.createElement('div');
            logBlock.className = 'progress-log';
            
            const header = document.createElement('div');
            header.className = 'progress-log-header';
            header.textContent = '📋 运行日志';
            header.addEventListener('click', () => {
                logBlock.classList.toggle('collapsed');
            });
            logBlock.appendChild(header);
            
            const logContent = document.createElement('pre');
            logContent.className = 'progress-log-content';
            logBlock.appendChild(logContent);
            
            this.messagesDiv.appendChild(logBlock);
        }
        
        const logContent = logBlock.querySelector('.progress-log-content');
        if (logContent.textContent) {
            logContent.textContent += '\n' + content;
        } else {
            logContent.textContent = content;
        }
        
        // 更新行数显示
        const lineCount = logContent.textContent.split('\n').length;
        const header = logBlock.querySelector('.progress-log-header');
        header.textContent = `📋 运行日志 (${lineCount} 行)`;
        
        this.scrollToBottom();
        this.saveMessageHistory();
    }
    
    /**
     * 更新连接状态显示
     */
    updateStatus(text, className) {
        this.statusSpan.textContent = text;
        this.statusSpan.className = `status status-${className}`;
    }
    
    /**
     * 清空欢迎消息
     */
    clearWelcomeMessage() {
        const welcomeMsg = this.messagesDiv.querySelector('.welcome-message');
        if (welcomeMsg) {
            welcomeMsg.remove();
        }
    }
    
    /**
     * 判断用户是否在聊天区底部附近
     */
    isNearBottom() {
        const threshold = 150;
        const { scrollTop, scrollHeight, clientHeight } = this.messagesDiv;
        return scrollHeight - scrollTop - clientHeight < threshold;
    }

    /**
     * 滚动到底部（仅当用户在底部附近时）
     */
    scrollToBottom(force = false) {
        if (force || this.isNearBottom()) {
            this.messagesDiv.scrollTop = this.messagesDiv.scrollHeight;
        }
    }
    
    /**
     * 更新滚动到底部按钮的可见性
     */
    _updateScrollBtnVisibility() {
        if (this.scrollBottomBtn) {
            this.scrollBottomBtn.classList.toggle('visible', !this.isNearBottom());
        }
    }
    
    /**
     * 生成 Session ID
     */
    generateSessionId() {
        const timestamp = Date.now().toString(36);
        const random = Math.random().toString(36).substr(2, 5);
        return 'web_' + timestamp + '_' + random;
    }
    
    /**
     * 验证 Session ID 格式
     * 格式: ^[a-z_][a-z0-9_]*$
     */
    validateSessionId(sessionId) {
        const pattern = /^[a-z_][a-z0-9_]*$/;
        return pattern.test(sessionId);
    }
    
    /**
     * 保存消息历史到 LocalStorage
     */
    saveMessageHistory() {
        if (!this.sessionId) return;
        
        try {
            const messages = [];
            const children = this.messagesDiv.querySelectorAll('.message:not(.system), .progress-log');
            
            children.forEach(el => {
                // 处理进度日志块
                if (el.classList.contains('progress-log')) {
                    const logContent = el.querySelector('.progress-log-content');
                    if (logContent && logContent.textContent) {
                        messages.push({
                            role: 'progress-log',
                            content: logContent.textContent
                        });
                    }
                    return;
                }
                
                const role = el.classList.contains('user') ? 'user' 
                           : el.classList.contains('agent') ? 'agent'
                           : el.classList.contains('command-response') ? 'command-response'
                           : el.classList.contains('error') ? 'error'
                           : el.classList.contains('progress') ? 'progress'
                           : 'unknown';
                
                const bubble = el.querySelector('.message-bubble');
                if (bubble) {
                    messages.push({
                        role: role,
                        content: bubble.dataset.rawContent || bubble.textContent
                    });
                }
            });
            
            // 只保留最近 50 条消息
            const recentMessages = messages.slice(-50);
            localStorage.setItem(`chat_history_${this.sessionId}`, JSON.stringify(recentMessages));
            
            // 更新时间戳
            localStorage.setItem(`session_timestamp_${this.sessionId}`, Date.now().toString());
        } catch (error) {
            console.error('Failed to save message history:', error);
        }
    }
    
    /**
     * 从 LocalStorage 加载消息历史
     */
    loadMessageHistory() {
        if (!this.sessionId) return;
        
        try {
            const historyJson = localStorage.getItem(`chat_history_${this.sessionId}`);
            if (historyJson) {
                const messages = JSON.parse(historyJson);
                
                // 添加分隔线
                this.addSystemMessage('─── 历史消息 ───');
                
                messages.forEach(msg => {
                    if (msg.role === 'progress-log') {
                        // 将保存的日志内容逐行还原到日志块
                        msg.content.split('\n').forEach(line => {
                            this.addProgressLog(line);
                        });
                    } else if (msg.role === 'error') {
                        this.addErrorMessage(msg.content.replace('❌ ', ''));
                    } else if (msg.role === 'unknown') {
                        // Back-compat: older builds saved command-response messages
                        // as role='unknown' because the role classifier was missing
                        // a command-response branch. Re-render them as command-response.
                        this.addMessage('command-response', msg.content);
                    } else {
                        this.addMessage(msg.role, msg.content);
                    }
                });
                
                this.addSystemMessage('─── 新消息 ───');
                this.scrollToBottom(true);
            }
        } catch (error) {
            console.error('Failed to load message history:', error);
        }
    }
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', () => {
    console.log('PyClaw-CC Chat Client (Enhanced) initializing...');
    window.chatClient = new ChatClient();
    console.log('Chat client ready with Markdown, code highlighting, and session management!');
});
