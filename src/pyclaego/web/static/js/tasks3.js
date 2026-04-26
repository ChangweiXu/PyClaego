/**
 * Task Dashboard V3 — collapsible session ▸ turn ▸ graph
 *
 * Phase A scope:
 * - Connect to /ws/tasks (existing endpoint).
 * - 3-pane layout: sessions list, turns list, execution graph + detail.
 * - A "turn" = a top-level (parent_id is null) task; root task subtree = the graph.
 * - Render mechanical digest (from task.metadata.digest) in detail panel.
 * - No artifact fetch yet; that arrives in Phase B.
 */

const ICON_BY_TYPE = {
    user_message: '💬',
    agent_loop: '🔁',
    tool_execution: '🔧',
    subagent_spawn: '🧬',
    subagent_loop: '🔄',
    llm_call: '🤖',
    memory_compress: '🗜️',
    memory_recall: '🔍',
    memory_budget: '💰',
    memory_brief: '📝',
    memory_write_review: '🛡️',
    memory_evict: '🚮',
};

const STATUS_LABEL = {
    pending: '⏳ pending',
    running: '🔄 running',
    completed: '✅ completed',
    failed: '❌ failed',
    cancelled: '🚫 cancelled',
};

class TaskDashboardV3 {
    constructor() {
        this.ws = null;
        this.taskTree = {};                 // session_id -> [root task nodes]
        this.activeSession = null;          // session_id
        this.activeTurnRootId = null;       // root task id of the active turn
        this.selectedNodeId = null;         // currently clicked task id
        this.collapsed = new Set();         // task ids whose children are collapsed
        this.reconnectTimer = null;

        this._bindUI();
        this._connect();
    }

    // ───────────── UI ─────────────
    _bindUI() {
        document.getElementById('refresh-btn').onclick = () => {
            if (this.ws) try { this.ws.close(); } catch {}
        };
    }

    _setStatus(state) {
        const el = document.getElementById('ws-status');
        if (state === 'connected') {
            el.className = 'status status-connected';
            el.textContent = '已连接';
        } else if (state === 'error') {
            el.className = 'status status-error';
            el.textContent = '错误';
        } else {
            el.className = 'status status-disconnected';
            el.textContent = '未连接';
        }
    }

    // ───────────── WebSocket ─────────────
    _connect() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}/ws/tasks`;
        try { this.ws = new WebSocket(url); } catch (e) {
            console.error('[tasks3] ws ctor error', e);
            this._scheduleReconnect();
            return;
        }

        this.ws.onopen = () => {
            this._setStatus('connected');
            if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
        };
        this.ws.onerror = () => this._setStatus('error');
        this.ws.onclose = () => {
            this._setStatus('disconnected');
            this._scheduleReconnect();
        };
        this.ws.onmessage = (ev) => {
            try { this._onMessage(JSON.parse(ev.data)); }
            catch (e) { console.error('[tasks3] parse', e); }
        };
    }

    _scheduleReconnect() {
        if (this.reconnectTimer) return;
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this._connect();
        }, 3000);
    }

    _onMessage(data) {
        const t = data.type;
        if (t === 'initial_snapshot' || t === 'task_update') {
            this.taskTree = data.task_tree || {};
            this._renderSessions();
            this._renderTurns();
            this._renderGraph();
        }
        // task_log ignored in Phase A (logs are still in tasks2)
    }

    // ───────────── render: sessions ─────────────
    _renderSessions() {
        const ul = document.getElementById('session-list');
        const sessions = Object.keys(this.taskTree).sort();
        document.getElementById('sessions-count').textContent = sessions.length;
        ul.innerHTML = '';

        if (sessions.length === 0) {
            const li = document.createElement('li');
            li.className = 'empty';
            li.textContent = '暂无任务';
            ul.appendChild(li);
            return;
        }

        for (const sid of sessions) {
            const turns = this.taskTree[sid] || [];
            const li = document.createElement('li');
            if (sid === this.activeSession) li.classList.add('active');
            li.innerHTML = `
                <div class="session-id">${this._esc(sid)}</div>
                <div class="session-meta">
                    <span>turns: ${turns.length}</span>
                    <span>${this._summarizeStatuses(turns)}</span>
                </div>
            `;
            li.onclick = () => {
                this.activeSession = sid;
                this.activeTurnRootId = null;
                this.selectedNodeId = null;
                this._renderSessions();
                this._renderTurns();
                this._renderGraph();
                this._renderDetail(null);
            };
            ul.appendChild(li);
        }

        // Auto-pick first session
        if (!this.activeSession && sessions.length > 0) {
            this.activeSession = sessions[0];
            this._renderSessions();
        }
    }

    _summarizeStatuses(turns) {
        const counts = {};
        for (const t of turns) counts[t.status] = (counts[t.status] || 0) + 1;
        return Object.entries(counts).map(([s, n]) => `${this._statusEmoji(s)}${n}`).join(' ');
    }

    _statusEmoji(s) {
        return ({pending:'⏳',running:'🔄',completed:'✅',failed:'❌',cancelled:'🚫'})[s] || '•';
    }

    // ───────────── render: turns ─────────────
    _renderTurns() {
        const ul = document.getElementById('turn-list');
        const titleEl = document.getElementById('turns-title');
        ul.innerHTML = '';

        if (!this.activeSession) {
            titleEl.textContent = 'Turns';
            document.getElementById('turns-count').textContent = '0';
            ul.innerHTML = '<li class="empty">选择左侧 session</li>';
            return;
        }

        titleEl.textContent = `Turns · ${this.activeSession}`;
        const turns = (this.taskTree[this.activeSession] || []).slice().reverse(); // newest first
        document.getElementById('turns-count').textContent = turns.length;

        if (turns.length === 0) {
            ul.innerHTML = '<li class="empty">该 session 下暂无 turn</li>';
            return;
        }

        for (const turn of turns) {
            const li = document.createElement('li');
            if (turn.task_id === this.activeTurnRootId) li.classList.add('active');
            const created = (turn.created_at || '').slice(11, 19);
            const icon = ICON_BY_TYPE[turn.task_type] || '•';
            li.innerHTML = `
                <div class="turn-name">${icon} ${this._esc(turn.name)}</div>
                <div class="turn-meta">
                    <span class="s-${turn.status}">${this._statusEmoji(turn.status)} ${turn.status}</span>
                    <span>${created}</span>
                </div>
            `;
            li.onclick = () => {
                this.activeTurnRootId = turn.task_id;
                this.selectedNodeId = null;
                this._renderTurns();
                this._renderGraph();
                this._renderDetail(null);
            };
            ul.appendChild(li);
        }

        // Auto-pick newest turn
        if (!this.activeTurnRootId && turns.length > 0) {
            this.activeTurnRootId = turns[0].task_id;
            this._renderTurns();
        }
    }

    // ───────────── render: graph ─────────────
    _renderGraph() {
        const area = document.getElementById('graph-area');
        const titleEl = document.getElementById('graph-title');
        if (!this.activeSession || !this.activeTurnRootId) {
            titleEl.textContent = 'Execution Graph';
            area.innerHTML = '<div class="empty">选择中间一个 turn</div>';
            return;
        }
        const turn = this._findTurn();
        if (!turn) {
            area.innerHTML = '<div class="empty">未找到 turn</div>';
            return;
        }
        titleEl.textContent = `Graph · ${turn.name}`;
        area.innerHTML = '';
        const tree = document.createElement('div');
        tree.className = 'tree';
        tree.appendChild(this._renderNode(turn));
        area.appendChild(tree);
    }

    _renderNode(node) {
        const wrap = document.createElement('div');
        wrap.className = 'node';

        const row = document.createElement('div');
        row.className = 'node-row';
        if (node.task_id === this.selectedNodeId) row.classList.add('selected');

        const hasChildren = (node.children && node.children.length > 0);
        const collapsed = this.collapsed.has(node.task_id);

        const tog = document.createElement('span');
        tog.className = 'toggler';
        tog.textContent = hasChildren ? (collapsed ? '▶' : '▼') : '·';
        if (hasChildren) {
            tog.onclick = (e) => {
                e.stopPropagation();
                if (collapsed) this.collapsed.delete(node.task_id);
                else this.collapsed.add(node.task_id);
                this._renderGraph();
            };
        }
        row.appendChild(tog);

        const ico = document.createElement('span');
        ico.className = 'node-icon';
        ico.textContent = ICON_BY_TYPE[node.task_type] || '•';
        row.appendChild(ico);

        const name = document.createElement('span');
        name.className = 'node-name';
        name.textContent = node.name;
        row.appendChild(name);

        const tag = document.createElement('span');
        tag.className = `node-tag tag-${node.status}`;
        tag.textContent = STATUS_LABEL[node.status] || node.status;
        row.appendChild(tag);

        row.onclick = () => {
            this.selectedNodeId = node.task_id;
            this._renderGraph();
            this._renderDetail(node);
        };

        wrap.appendChild(row);

        if (hasChildren) {
            const ch = document.createElement('div');
            ch.className = 'children' + (collapsed ? ' collapsed' : '');
            for (const c of node.children) ch.appendChild(this._renderNode(c));
            wrap.appendChild(ch);
        }
        return wrap;
    }

    _findTurn() {
        const turns = this.taskTree[this.activeSession] || [];
        return turns.find(t => t.task_id === this.activeTurnRootId);
    }

    // ───────────── render: detail panel ─────────────
    _renderDetail(node) {
        const el = document.getElementById('detail-panel');
        if (!node) {
            el.innerHTML = '<div class="empty">点击图中节点查看详情</div>';
            return;
        }
        const meta = node.metadata || {};
        const digest = meta.digest || {};

        const rows = [];
        rows.push(this._row('task_id', node.task_id));
        rows.push(this._row('type', node.task_type));
        rows.push(this._row('status', `<span class="s-${node.status}">${STATUS_LABEL[node.status] || node.status}</span>`));
        if (node.created_at) rows.push(this._row('created', node.created_at));
        if (node.started_at) rows.push(this._row('started', node.started_at));
        if (node.finished_at || node.completed_at) rows.push(this._row('finished', node.finished_at || node.completed_at));
        if (digest.duration_ms != null) rows.push(this._row('duration', `${digest.duration_ms} ms`));
        if (node.description) rows.push(this._row('description', node.description));
        if (node.error) rows.push(this._row('error', `<span class="s-failed">${this._esc(node.error)}</span>`));

        let html = `<div class="rows">${rows.join('')}</div>`;

        if (Object.keys(digest).length) {
            html += `<div style="margin-top:8px;color:var(--text-secondary);font-size:12px;">digest</div>`;
            html += `<pre>${this._esc(JSON.stringify(digest, null, 2))}</pre>`;
        }

        const restMeta = { ...meta };
        delete restMeta.digest;
        delete restMeta.result;
        if (Object.keys(restMeta).length) {
            html += `<div style="margin-top:8px;color:var(--text-secondary);font-size:12px;">metadata</div>`;
            html += `<pre>${this._esc(JSON.stringify(restMeta, null, 2))}</pre>`;
        }

        // Phase B: artifacts (lazy)
        html += `<div style="margin-top:10px;display:flex;align-items:center;gap:8px;">
            <span style="color:var(--text-secondary);font-size:12px;">artifacts</span>
            <span id="artifacts-status" style="color:var(--text-secondary);font-size:11px;">loading…</span>
        </div>`;
        html += `<div id="artifacts-list" style="margin-top:4px;"></div>`;
        html += `<div id="artifact-viewer" style="margin-top:6px;"></div>`;

        el.innerHTML = html;
        this._loadArtifacts(node.task_id);
    }

    async _loadArtifacts(taskId) {
        const statusEl = document.getElementById('artifacts-status');
        const listEl = document.getElementById('artifacts-list');
        if (!listEl) return;
        try {
            const resp = await fetch(`/api/tasks/tasks/${encodeURIComponent(taskId)}/artifacts`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const arts = data.artifacts || [];
            if (statusEl) statusEl.textContent = `${arts.length} item(s)`;
            if (arts.length === 0) {
                listEl.innerHTML = '<div style="font-size:12px;color:var(--text-secondary);">(无)</div>';
                return;
            }
            listEl.innerHTML = '';
            for (const a of arts) {
                const row = document.createElement('div');
                row.style.cssText = 'display:flex;gap:6px;align-items:center;padding:3px 0;font-size:12px;';
                const kindTag = document.createElement('span');
                kindTag.className = 'node-tag';
                kindTag.textContent = a.kind;
                row.appendChild(kindTag);
                const nameSpan = document.createElement('span');
                nameSpan.textContent = a.name;
                nameSpan.style.flex = '1';
                row.appendChild(nameSpan);
                const sizeSpan = document.createElement('span');
                sizeSpan.style.color = 'var(--text-secondary)';
                sizeSpan.textContent = `${a.size}B`;
                row.appendChild(sizeSpan);
                const btn = document.createElement('button');
                btn.textContent = 'view';
                btn.style.cssText = 'font-size:11px;padding:1px 6px;cursor:pointer;border:1px solid var(--border);background:white;border-radius:3px;';
                btn.onclick = () => this._viewArtifact(taskId, a);
                row.appendChild(btn);
                listEl.appendChild(row);
            }
        } catch (e) {
            if (statusEl) statusEl.textContent = `error: ${e.message}`;
        }
    }

    async _viewArtifact(taskId, ref) {
        const viewer = document.getElementById('artifact-viewer');
        if (!viewer) return;
        viewer.innerHTML = `<div style="font-size:11px;color:var(--text-secondary);">loading ${ref.name}…</div>`;
        try {
            const resp = await fetch(`/api/tasks/artifacts/${encodeURIComponent(taskId)}/${encodeURIComponent(ref.artifact_id)}`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const txt = await resp.text();
            let body = txt;
            if ((ref.mime || '').includes('json')) {
                try { body = JSON.stringify(JSON.parse(txt), null, 2); } catch {}
            }
            const trimmed = body.length > 20000 ? body.slice(0, 20000) + '\n…(truncated)' : body;
            viewer.innerHTML = `
                <div style="font-size:11px;color:var(--text-secondary);margin:6px 0 2px;">${this._esc(ref.name)} · ${this._esc(ref.mime)}</div>
                <pre>${this._esc(trimmed)}</pre>
            `;
        } catch (e) {
            viewer.innerHTML = `<div style="color:var(--error);font-size:12px;">load failed: ${this._esc(e.message)}</div>`;
        }
    }

    _row(label, value) {
        return `<div class="row"><span class="label">${label}</span><span class="value">${value}</span></div>`;
    }

    _esc(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g,'&amp;')
            .replace(/</g,'&lt;')
            .replace(/>/g,'&gt;');
    }
}

window.addEventListener('DOMContentLoaded', () => new TaskDashboardV3());
