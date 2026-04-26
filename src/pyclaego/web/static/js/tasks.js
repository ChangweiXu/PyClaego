/**
 * TaskManagerClient - 任务管理器前端客户端
 * 
 * 功能：
 * - WebSocket 实时连接任务推送
 * - 任务树渲染和实时更新
 * - Session 和状态过滤
 * - 统计信息展示
 */

class TaskManagerClient {
    constructor() {
        this.ws = null;
        this.taskTree = {};
        this.reconnectTimeout = null;
        this.reconnectDelay = 3000;
        
        this.init();
    }
    
    init() {
        this.connectWebSocket();
        this.setupEventListeners();
    }
    
    connectWebSocket() {
        // 构建 WebSocket URL
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/tasks`;
        
        console.log('[TaskManager] 正在连接:', wsUrl);
        
        this.ws = new WebSocket(wsUrl);
        
        this.ws.onopen = () => {
            this.updateStatus('connected');
            console.log('[TaskManager] WebSocket 已连接');
            
            // 清除重连定时器
            if (this.reconnectTimeout) {
                clearTimeout(this.reconnectTimeout);
                this.reconnectTimeout = null;
            }
        };
        
        this.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.handleMessage(data);
            } catch (e) {
                console.error('[TaskManager] 解析消息失败:', e);
            }
        };
        
        this.ws.onerror = (error) => {
            console.error('[TaskManager] WebSocket 错误:', error);
            this.updateStatus('error');
        };
        
        this.ws.onclose = () => {
            this.updateStatus('disconnected');
            console.log('[TaskManager] WebSocket 已断开');
            
            // 自动重连
            this.reconnectTimeout = setTimeout(() => {
                console.log('[TaskManager] 尝试重新连接...');
                this.connectWebSocket();
            }, this.reconnectDelay);
        };
    }
    
    handleMessage(data) {
        console.log('[TaskManager] 收到消息:', data.type);
        
        if (data.type === 'initial_snapshot') {
            console.log('[TaskManager] 收到初始快照');
            this.taskTree = data.task_tree || {};
            this.updateSessionSelector();
            this.updateStats();
            this.renderTaskTree();
        } else if (data.type === 'task_update') {
            const eventType = data.event?.event_type || 'UNKNOWN';
            console.log('[TaskManager] 任务更新:', eventType);
            this.taskTree = data.task_tree || {};
            this.updateSessionSelector();
            this.updateStats();
            this.renderTaskTree();
        }
    }
    
    updateSessionSelector() {
        const select = document.getElementById('session-filter');
        const currentValue = select.value;
        
        // 清空选项（保留"全部"）
        select.innerHTML = '<option value="">全部 Session</option>';
        
        // 添加所有 Session
        const sessionIds = Object.keys(this.taskTree).sort();
        for (const sessionId of sessionIds) {
            const option = document.createElement('option');
            option.value = sessionId;
            option.textContent = sessionId;
            select.appendChild(option);
        }
        
        // 恢复选中状态
        if (currentValue && sessionIds.includes(currentValue)) {
            select.value = currentValue;
        }
    }
    
    updateStats() {
        const sessionCount = Object.keys(this.taskTree).length;
        let taskCount = 0;
        
        // 递归计算任务总数
        const countTasks = (tasks) => {
            let count = tasks.length;
            for (const task of tasks) {
                if (task.children && task.children.length > 0) {
                    count += countTasks(task.children);
                }
            }
            return count;
        };
        
        for (const tasks of Object.values(this.taskTree)) {
            taskCount += countTasks(tasks);
        }
        
        document.getElementById('stat-sessions').textContent = sessionCount;
        document.getElementById('stat-tasks').textContent = taskCount;
    }
    
    renderTaskTree() {
        const container = document.getElementById('task-tree');
        const selectedSession = document.getElementById('session-filter').value;
        const selectedStatus = document.getElementById('status-filter').value;
        
        // 过滤 Session
        let sessions = selectedSession 
            ? { [selectedSession]: this.taskTree[selectedSession] }
            : this.taskTree;
        
        // 清空容器
        container.innerHTML = '';
        
        // 检查是否有任务
        if (Object.keys(sessions).length === 0) {
            container.innerHTML = '<div class="empty-message">暂无任务数据</div>';
            return;
        }
        
        // 渲染每个 Session
        for (const [sessionId, tasks] of Object.entries(sessions)) {
            if (!tasks || tasks.length === 0) {
                continue;
            }
            
            const sessionDiv = this.createSessionElement(sessionId, tasks, selectedStatus);
            if (sessionDiv) {
                container.appendChild(sessionDiv);
            }
        }
        
        // 检查过滤后是否为空
        if (container.children.length === 0) {
            container.innerHTML = '<div class="empty-message">没有符合条件的任务</div>';
        }
    }
    
    createSessionElement(sessionId, tasks, statusFilter) {
        // 过滤任务（递归）
        const filteredTasks = this.filterTasksByStatus(tasks, statusFilter);
        
        if (filteredTasks.length === 0) {
            return null;
        }
        
        const div = document.createElement('div');
        div.className = 'session-block';
        
        // Session 标题
        const header = document.createElement('div');
        header.className = 'session-header';
        
        const taskCount = this.countTasks(filteredTasks);
        header.innerHTML = `
            <h2>📂 Session: <span class="session-id">${this.escapeHtml(sessionId)}</span></h2>
            <span class="task-count">${taskCount} 个任务</span>
        `;
        div.appendChild(header);
        
        // 任务列表
        const taskList = document.createElement('div');
        taskList.className = 'task-list';
        
        for (const task of filteredTasks) {
            const taskElement = this.createTaskElement(task, 0, statusFilter);
            taskList.appendChild(taskElement);
        }
        
        div.appendChild(taskList);
        return div;
    }
    
    filterTasksByStatus(tasks, status) {
        if (!status) {
            return tasks;
        }
        
        const filtered = [];
        for (const task of tasks) {
            // 检查当前任务或其子任务是否匹配
            const matchesStatus = task.status === status;
            const filteredChildren = task.children 
                ? this.filterTasksByStatus(task.children, status)
                : [];
            
            if (matchesStatus || filteredChildren.length > 0) {
                const taskCopy = { ...task };
                if (filteredChildren.length > 0) {
                    taskCopy.children = filteredChildren;
                }
                filtered.push(taskCopy);
            }
        }
        
        return filtered;
    }
    
    createTaskElement(task, level, statusFilter) {
        const div = document.createElement('div');
        div.className = `task-item level-${level} status-${task.status}`;
        div.dataset.taskId = task.task_id;
        
        // 状态图标
        const statusIcon = this.getStatusIcon(task.status);
        
        // 进度条
        let progressBar = '';
        if (task.status === 'running' && task.progress > 0) {
            const percent = Math.round(task.progress * 100);
            progressBar = `
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${percent}%"></div>
                    <span class="progress-text">${percent}%</span>
                </div>
            `;
        }
        
        // 时间信息
        const timeInfo = this.formatTimeInfo(task);
        
        // 任务信息
        div.innerHTML = `
            <div class="task-header">
                <span class="status-icon">${statusIcon}</span>
                <span class="task-type">[${this.escapeHtml(task.task_type)}]</span>
                <span class="task-name">${this.escapeHtml(task.name)}</span>
                <span class="task-time">${timeInfo}</span>
            </div>
            ${progressBar}
        `;
        
        // 添加描述（如果有）
        if (task.description) {
            const descDiv = document.createElement('div');
            descDiv.className = 'task-description';
            descDiv.textContent = task.description;
            div.appendChild(descDiv);
        }
        
        // 添加错误信息（如果有）
        if (task.error) {
            const errorDiv = document.createElement('div');
            errorDiv.className = 'task-error';
            errorDiv.textContent = `错误: ${task.error}`;
            div.appendChild(errorDiv);
        }
        
        // 添加子任务
        if (task.children && task.children.length > 0) {
            const childrenDiv = document.createElement('div');
            childrenDiv.className = 'task-children';
            
            for (const child of task.children) {
                const childElement = this.createTaskElement(child, level + 1, statusFilter);
                childrenDiv.appendChild(childElement);
            }
            
            div.appendChild(childrenDiv);
        }
        
        return div;
    }
    
    getStatusIcon(status) {
        const icons = {
            'pending': '⏳',
            'running': '🔄',
            'completed': '✅',
            'failed': '❌',
            'cancelled': '🚫'
        };
        return icons[status] || '❓';
    }
    
    formatTimeInfo(task) {
        if (task.finished_at) {
            return this.formatTime(task.finished_at);
        } else if (task.started_at) {
            return `开始于 ${this.formatTime(task.started_at)}`;
        } else if (task.created_at) {
            return `创建于 ${this.formatTime(task.created_at)}`;
        }
        return '';
    }
    
    formatTime(timestamp) {
        if (!timestamp) return '';
        try {
            const date = new Date(timestamp);
            return date.toLocaleTimeString('zh-CN', { 
                hour: '2-digit', 
                minute: '2-digit', 
                second: '2-digit' 
            });
        } catch (e) {
            return timestamp;
        }
    }
    
    countTasks(tasks) {
        let count = tasks.length;
        for (const task of tasks) {
            if (task.children) {
                count += this.countTasks(task.children);
            }
        }
        return count;
    }
    
    updateStatus(status) {
        const statusEl = document.getElementById('ws-status');
        statusEl.className = `status status-${status}`;
        
        const statusText = {
            'connected': '已连接',
            'disconnected': '未连接',
            'error': '错误'
        };
        statusEl.textContent = statusText[status] || status;
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
    
    setupEventListeners() {
        // Session 过滤器
        document.getElementById('session-filter').addEventListener('change', () => {
            this.renderTaskTree();
        });
        
        // 状态过滤器
        document.getElementById('status-filter').addEventListener('change', () => {
            this.renderTaskTree();
        });
        
        // 刷新按钮
        document.getElementById('refresh-btn').addEventListener('click', () => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                // 重新连接以获取最新快照
                console.log('[TaskManager] 手动刷新');
                this.ws.close();
                setTimeout(() => this.connectWebSocket(), 100);
            } else {
                console.log('[TaskManager] 正在重连...');
                this.connectWebSocket();
            }
        });
    }
}

// 启动客户端
document.addEventListener('DOMContentLoaded', () => {
    console.log('[TaskManager] 初始化任务管理器客户端');
    window.taskManager = new TaskManagerClient();
});
