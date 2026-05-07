/**
 * TaskDashboardClient - 任务详情仪表盘前端客户端
 *
 * 功能：
 * - WebSocket 实时连接任务推送
 * - 任务树渲染（含详情面板：LLM / 工具 / 子Agent 信息）
 * - 可折叠日志面板（实时追加）
 * - Session / 状态 / 类型过滤
 */

class TaskDashboardClient {
    constructor() {
        this.ws = null;
        this.taskTree = {};          // session_id -> [task nodes]
        this.taskLogs = {};          // task_id -> [{timestamp, level, message}]
        this.expandedLogs = new Set(); // task_ids whose log panel is open
        this.reconnectTimeout = null;
        this.reconnectDelay = 3000;

        this.init();
    }

    // ================================================================
    // Initialization
    // ================================================================

    init() {
        this.connectWebSocket();
        this.setupEventListeners();
    }

    // ================================================================
    // WebSocket
    // ================================================================

    connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/tasks`;
        console.log('[Dashboard] Connecting:', wsUrl);

        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            this.updateStatus('connected');
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
                console.error('[Dashboard] Parse error:', e);
            }
        };

        this.ws.onerror = () => this.updateStatus('error');

        this.ws.onclose = () => {
            this.updateStatus('disconnected');
            this.reconnectTimeout = setTimeout(() => this.connectWebSocket(), this.reconnectDelay);
        };
    }

    handleMessage(data) {
        const type = data.type;

        if (type === 'initial_snapshot') {
            this.taskTree = data.task_tree || {};
            // Merge initial logs
            const logs = data.task_logs || {};
            for (const [tid, entries] of Object.entries(logs)) {
                this.taskLogs[tid] = entries;
            }
            this.fullRender();
            return;
        }

        if (type === 'task_log') {
            // Incremental log append
            const tid = data.task_id;
            if (!this.taskLogs[tid]) this.taskLogs[tid] = [];
            this.taskLogs[tid].push(data.log);
            this._appendLogEntry(tid, data.log);
            this._updateLogToggleCount(tid);
            return;
        }

        if (type === 'task_update') {
            this.taskTree = data.task_tree || {};
            this.fullRender();
            return;
        }
    }

    // ================================================================
    // Rendering
    // ================================================================

    fullRender() {
        this.updateSessionSelector();
        this.updateStats();
        this.renderTaskTree();
    }

    updateSessionSelector() {
        const select = document.getElementById('session-filter');
        const cur = select.value;
        select.innerHTML = '<option value="">全部</option>';
        for (const sid of Object.keys(this.taskTree).sort()) {
            const opt = document.createElement('option');
            opt.value = sid;
            opt.textContent = sid;
            select.appendChild(opt);
        }
        if (cur && Object.keys(this.taskTree).includes(cur)) select.value = cur;
    }

    updateStats() {
        const sessionCount = Object.keys(this.taskTree).length;
        let taskCount = 0;
        const count = (tasks) => {
            taskCount += tasks.length;
            tasks.forEach(t => { if (t.children) count(t.children); });
        };
        Object.values(this.taskTree).forEach(count);
        document.getElementById('stat-sessions').textContent = sessionCount;
        document.getElementById('stat-tasks').textContent = taskCount;
    }

    renderTaskTree() {
        const container = document.getElementById('task-tree');
        const selSession = document.getElementById('session-filter').value;
        const selStatus = document.getElementById('status-filter').value;
        const selType = document.getElementById('type-filter').value;

        let sessions = selSession
            ? { [selSession]: this.taskTree[selSession] }
            : this.taskTree;

        container.innerHTML = '';

        if (Object.keys(sessions).length === 0) {
            container.innerHTML = '<div class="empty-message">暂无任务数据</div>';
            return;
        }

        for (const [sid, tasks] of Object.entries(sessions)) {
            if (!tasks || tasks.length === 0) continue;
            const filtered = this._filterTasks(tasks, selStatus, selType);
            if (filtered.length === 0) continue;

            const block = document.createElement('div');
            block.className = 'session-block';

            const header = document.createElement('div');
            header.className = 'session-header';
            const cnt = this._countTasks(filtered);
            header.innerHTML = `
                <h2>📂 Session: <span class="session-id">${this._esc(sid)}</span></h2>
                <span class="task-count">${cnt} 个任务</span>
            `;
            block.appendChild(header);

            const list = document.createElement('div');
            list.className = 'task-list';
            filtered.forEach(t => list.appendChild(this._renderTaskCard(t, 0, selStatus, selType)));
            block.appendChild(list);

            container.appendChild(block);
        }

        if (container.children.length === 0) {
            container.innerHTML = '<div class="empty-message">没有符合条件的任务</div>';
        }
    }

    // ================================================================
    // Task Card Rendering
    // ================================================================

    _renderTaskCard(task, level, statusFilter, typeFilter) {
        const card = document.createElement('div');
        card.className = `task-card level-${Math.min(level, 5)} status-${task.status}`;
        card.dataset.taskId = task.task_id;

        // ---- Header row ----
        const hdr = document.createElement('div');
        hdr.className = 'task-card-header';
        hdr.innerHTML = `
            <span class="status-icon">${this._statusIcon(task.status)}</span>
            <span class="task-type-badge type-${task.task_type}">${this._esc(task.task_type)}</span>
            <span class="task-name" title="${this._esc(task.name)}">${this._esc(task.name)}</span>
            <span class="task-time">${this._fmtTime(task)}</span>
        `;
        card.appendChild(hdr);

        // ---- Progress bar ----
        if (task.status === 'running' && task.progress > 0) {
            const pct = Math.round(task.progress * 100);
            const bar = document.createElement('div');
            bar.className = 'progress-bar';
            bar.innerHTML = `<div class="progress-fill" style="width:${pct}%"></div>`;
            card.appendChild(bar);
        }

        // ---- Detail panel (type-specific) ----
        const detail = this._renderDetail(task);
        if (detail) card.appendChild(detail);

        // ---- Error ----
        if (task.error) {
            const err = document.createElement('div');
            err.className = 'task-error';
            err.textContent = task.error;
            card.appendChild(err);
        }

        // ---- Log toggle + panel ----
        const logs = this.taskLogs[task.task_id] || [];
        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'log-toggle';
        toggleBtn.dataset.taskId = task.task_id;
        const isExpanded = this.expandedLogs.has(task.task_id);
        if (isExpanded) toggleBtn.classList.add('expanded');
        toggleBtn.innerHTML = `<span class="arrow">▶</span> Logs (<span class="log-count">${logs.length}</span>)`;
        toggleBtn.addEventListener('click', () => this._toggleLog(task.task_id));
        card.appendChild(toggleBtn);

        const panel = document.createElement('div');
        panel.className = 'log-panel' + (isExpanded ? ' visible' : '');
        panel.id = `log-panel-${task.task_id}`;
        logs.forEach(entry => panel.appendChild(this._createLogEntryEl(entry)));
        card.appendChild(panel);

        // Auto-scroll if expanded
        if (isExpanded) {
            requestAnimationFrame(() => { panel.scrollTop = panel.scrollHeight; });
        }

        // ---- Children ----
        if (task.children && task.children.length > 0) {
            const childrenDiv = document.createElement('div');
            childrenDiv.className = 'task-children';
            const childFiltered = this._filterTasks(task.children, statusFilter, typeFilter);
            childFiltered.forEach(c => childrenDiv.appendChild(this._renderTaskCard(c, level + 1, statusFilter, typeFilter)));
            card.appendChild(childrenDiv);
        }

        return card;
    }

    // ================================================================
    // Detail Panel (type-specific)
    // ================================================================

    _renderDetail(task) {
        const meta = task.metadata || {};
        const tt = task.task_type;
        let items = [];

        if (tt === 'agent_loop' || tt === 'subagent_loop') {
            if (meta.round != null) items.push(['Round', meta.round]);
            if (meta.agent_type) items.push(['Agent', meta.agent_type]);
        }

        if (tt === 'tool_execution') {
            if (meta.tool_names) items.push(['Tools', Array.isArray(meta.tool_names) ? meta.tool_names.join(', ') : meta.tool_names]);
            if (meta.tool_count != null) items.push(['Count', meta.tool_count]);
        }

        if (tt === 'subagent_spawn') {
            if (meta.subagent_type) items.push(['Type', meta.subagent_type]);
            if (meta.subagent_id) items.push(['ID', meta.subagent_id]);
            if (meta.memory_mode) items.push(['Memory', meta.memory_mode]);
            if (meta.context_strategy) items.push(['Strategy', meta.context_strategy]);
            if (meta.llm_id) items.push(['LLM', meta.llm_id]);
            if (meta.initial_message) items.push(['Message', this._truncate(meta.initial_message, 200)]);
        }

        if (tt === 'llm_call') {
            if (meta.llm_id) items.push(['Model', meta.llm_id]);
            if (meta.token_estimate) items.push(['Tokens', JSON.stringify(meta.token_estimate)]);
            if (meta.stop_reason) items.push(['Stop', meta.stop_reason]);
            if (meta.response_preview) items.push(['Response', this._truncate(meta.response_preview, 200)]);
        }

        if (task.description) {
            items.push(['Desc', this._truncate(task.description, 200)]);
        }

        if (items.length === 0) return null;

        const div = document.createElement('div');
        div.className = 'task-detail';
        const grid = document.createElement('div');
        grid.className = 'detail-grid';
        items.forEach(([label, value]) => {
            const item = document.createElement('span');
            item.className = 'detail-item';
            item.innerHTML = `<span class="detail-label">${this._esc(label)}:</span> <span class="detail-value truncated" title="${this._esc(String(value))}">${this._esc(String(value))}</span>`;
            grid.appendChild(item);
        });
        div.appendChild(grid);
        return div;
    }

    // ================================================================
    // Log Panel
    // ================================================================

    _toggleLog(taskId) {
        const btn = document.querySelector(`.log-toggle[data-task-id="${taskId}"]`);
        const panel = document.getElementById(`log-panel-${taskId}`);
        if (!btn || !panel) return;

        if (this.expandedLogs.has(taskId)) {
            this.expandedLogs.delete(taskId);
            btn.classList.remove('expanded');
            panel.classList.remove('visible');
        } else {
            this.expandedLogs.add(taskId);
            btn.classList.add('expanded');
            panel.classList.add('visible');
            panel.scrollTop = panel.scrollHeight;
        }
    }

    _appendLogEntry(taskId, entry) {
        const panel = document.getElementById(`log-panel-${taskId}`);
        if (!panel) return; // panel not rendered yet; fullRender will pick it up
        const el = this._createLogEntryEl(entry);
        panel.appendChild(el);

        // Auto-scroll only if expanded
        if (this.expandedLogs.has(taskId)) {
            panel.scrollTop = panel.scrollHeight;
        }
    }

    _updateLogToggleCount(taskId) {
        const btn = document.querySelector(`.log-toggle[data-task-id="${taskId}"] .log-count`);
        if (btn) {
            btn.textContent = (this.taskLogs[taskId] || []).length;
        }
    }

    _createLogEntryEl(entry) {
        const div = document.createElement('div');
        div.className = `log-entry level-${entry.level || 'info'}`;

        const ts = document.createElement('span');
        ts.className = 'log-ts';
        ts.textContent = this._fmtLogTs(entry.timestamp);

        const msg = document.createElement('span');
        msg.className = 'log-msg';
        msg.textContent = entry.message || '';

        div.appendChild(ts);
        div.appendChild(msg);
        return div;
    }

    // ================================================================
    // Filtering
    // ================================================================

    _filterTasks(tasks, status, type) {
        if (!status && !type) return tasks;
        const result = [];
        for (const t of tasks) {
            const matchStatus = !status || t.status === status;
            const matchType = !type || t.task_type === type;
            const childFiltered = t.children ? this._filterTasks(t.children, status, type) : [];
            if ((matchStatus && matchType) || childFiltered.length > 0) {
                result.push({ ...t, children: childFiltered.length > 0 ? childFiltered : t.children });
            }
        }
        return result;
    }

    // ================================================================
    // Helpers
    // ================================================================

    _statusIcon(s) {
        return { pending: '⏳', running: '🔄', completed: '✅', failed: '❌', cancelled: '🚫' }[s] || '❓';
    }

    _fmtTime(task) {
        const ts = task.finished_at || task.started_at || task.created_at;
        if (!ts) return '';
        try {
            return new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        } catch { return ts; }
    }

    _fmtLogTs(ts) {
        if (!ts) return '';
        try {
            return new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 });
        } catch { return ts; }
    }

    _truncate(s, max) {
        if (!s) return '';
        s = String(s);
        return s.length > max ? s.slice(0, max) + '…' : s;
    }

    _countTasks(tasks) {
        let c = tasks.length;
        tasks.forEach(t => { if (t.children) c += this._countTasks(t.children); });
        return c;
    }

    _esc(text) {
        const d = document.createElement('div');
        d.textContent = text;
        return d.innerHTML;
    }

    updateStatus(status) {
        const el = document.getElementById('ws-status');
        el.className = `status status-${status}`;
        el.textContent = { connected: '已连接', disconnected: '未连接', error: '错误' }[status] || status;
    }

    setupEventListeners() {
        document.getElementById('session-filter').addEventListener('change', () => this.renderTaskTree());
        document.getElementById('status-filter').addEventListener('change', () => this.renderTaskTree());
        document.getElementById('type-filter').addEventListener('change', () => this.renderTaskTree());
        document.getElementById('refresh-btn').addEventListener('click', () => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.close();
                setTimeout(() => this.connectWebSocket(), 100);
            } else {
                this.connectWebSocket();
            }
        });
    }
}

// Start
document.addEventListener('DOMContentLoaded', () => {
    window.taskDashboard = new TaskDashboardClient();
});
