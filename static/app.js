/* TurtleQL Web UI — app.js
   Monaco init, SSE handler, tab/panel logic, UI glue.  */

let editor = null;
let connected = false;
let busy = false;

// ---- Monaco Editor ----
function initMonaco() {
    require.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs' } });
    require(['vs/editor/editor.main'], function () {
        editor = monaco.editor.create(document.getElementById('monaco-container'), {
            value: '-- Write SQL or ask a question in the chat\n',
            language: 'sql',
            theme: 'vs-dark',
            minimap: { enabled: false },
            fontSize: 13,
            fontFamily: "'Cascadia Code', 'Fira Code', Consolas, monospace",
            scrollBeyondLastLine: false,
            automaticLayout: true,
            padding: { top: 8, bottom: 8 },
            lineNumbers: 'on',
            renderLineHighlight: 'line',
            tabSize: 2,
        });

        // Ctrl+Enter / Cmd+Enter = execute SQL from editor
        editor.addAction({
            id: 'run-sql',
            label: 'Run SQL',
            keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter],
            run: function () { runEditorSQL(); },
        });
    });
}

// ---- Panel & Tab management ----
function switchTab(tabName) {
    document.querySelectorAll('.panel-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));

    const tab = document.getElementById('tab-' + tabName);
    const content = document.getElementById('tab-content-' + tabName);
    if (tab) tab.classList.add('active');
    if (content) content.classList.add('active');

    // Ensure the right panel is visible when switching tabs
    const panel = document.getElementById('right-panel');
    if (panel.classList.contains('collapsed')) {
        toggleRightPanel();
    }

    // Relayout Monaco if switching to editor
    if (tabName === 'editor' && editor) {
        setTimeout(() => editor.layout(), 50);
    }
}

function toggleRightPanel() {
    const panel = document.getElementById('right-panel');
    const handle = document.getElementById('resize-h');
    const btn = document.getElementById('btn-toggle-panel');
    const chatPanel = document.querySelector('.chat-panel');

    if (panel.classList.contains('collapsed')) {
        // Expand: restore saved widths if we had them
        panel.classList.remove('collapsed');
        handle.style.display = '';
        btn.classList.add('active');
        if (panel._savedWidth) {
            chatPanel.style.flex = 'none';
            chatPanel.style.width = panel._savedChatWidth;
            panel.style.width = panel._savedWidth;
        }
        if (editor) setTimeout(() => editor.layout(), 220);
    } else {
        // Collapse: save current widths, then let chat fill the space
        panel._savedWidth = panel.style.width || '';
        panel._savedChatWidth = chatPanel.style.width || '';
        panel.classList.add('collapsed');
        handle.style.display = 'none';
        btn.classList.remove('active');
        chatPanel.style.flex = '1';
        chatPanel.style.width = '';
    }
}

function toggleSchemaDrawer() {
    const drawer = document.getElementById('schema-drawer');
    const btn = document.getElementById('btn-toggle-schema');
    if (drawer.classList.contains('hidden')) {
        drawer.classList.remove('hidden');
        btn.classList.add('active');
        document.getElementById('schema-q').focus();
        // Also show the Schema tab in the right panel
        showPanelTab('schema');
    } else {
        drawer.classList.add('hidden');
        btn.classList.remove('active');
    }
}

// ---- API helpers ----
async function api(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin' };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    const data = await r.json();
    if (!r.ok && !data.destructive) throw new Error(data.detail || data.error || r.statusText);
    return data;
}

function toast(msg, type) {
    const el = document.createElement('div');
    el.className = 'toast' + (type ? ' ' + type : '');
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4000);
}

function setBusy(b) {
    busy = b;
    document.getElementById('btn-run').disabled = b;
    document.getElementById('btn-ask').disabled = b;
    document.getElementById('loading-indicator').style.display = b ? 'inline-block' : 'none';
    document.getElementById('btn-cancel').style.display = b ? 'inline-block' : 'none';
    if (!b) {
        document.getElementById('progress-text').textContent = '';
    }
}

function setProgress(msg) {
    document.getElementById('progress-text').textContent = msg;
    // Also show as a dim status line in chat
    const history = document.getElementById('chat-history');
    let el = history.querySelector('.progress-line');
    if (!el) {
        el = document.createElement('div');
        el.className = 'chat-msg progress-line';
        el.style.color = 'var(--text-dim)';
        el.style.fontSize = '12px';
        history.appendChild(el);
    }
    el.textContent = msg;
    history.scrollTop = history.scrollHeight;
}

function clearProgressLine() {
    const el = document.getElementById('chat-history').querySelector('.progress-line');
    if (el) el.remove();
}

async function cancelQuery() {
    try {
        await api('POST', '/query/cancel');
    } catch (_) {}
    setBusy(false);
    toast('Cancelled');
}

// ---- Connect / Disconnect ----
async function showConnectDialog() {
    const overlay = document.getElementById('connect-dialog');
    overlay.classList.remove('hidden');
    const list = document.getElementById('profile-list');
    list.innerHTML = '<div style="padding:8px;color:var(--text-dim)">Loading...</div>';
    try {
        const profiles = await api('GET', '/profiles');
        list.innerHTML = '';
        profiles.forEach((p, i) => {
            const div = document.createElement('div');
            div.className = 'profile-option';
            div.dataset.name = p.name;
            const envTag = p.env ? `<span class="env-tag ${p.env.toLowerCase()}">${p.env}</span>` : '';
            div.innerHTML = `${p.name}${envTag}<span class="db-info">${p.host}:${p.port}/${p.database}</span>`;
            div.onclick = () => {
                list.querySelectorAll('.selected').forEach(el => el.classList.remove('selected'));
                div.classList.add('selected');
            };
            list.appendChild(div);
        });
    } catch (e) {
        list.innerHTML = `<div class="error-msg">${e.message}</div>`;
    }
}

function hideConnectDialog() {
    document.getElementById('connect-dialog').classList.add('hidden');
    document.getElementById('connect-error').textContent = '';
}

async function doConnect() {
    const selected = document.querySelector('#profile-list .selected');
    if (!selected) return;
    const username = document.getElementById('conn-user').value.trim();
    const password = document.getElementById('conn-pass').value;
    if (!username || !password) {
        document.getElementById('connect-error').textContent = 'Username and password required';
        return;
    }
    try {
        const data = await api('POST', '/connect', { profile: selected.dataset.name, username, password });
        connected = true;
        document.getElementById('conn-status').textContent = data.profile;
        document.getElementById('conn-status').className = 'conn-status connected';
        document.getElementById('btn-connect').textContent = 'Disconnect';
        hideConnectDialog();
        toast('Connected to ' + data.database, 'success');
    } catch (e) {
        document.getElementById('connect-error').textContent = e.message;
    }
}

async function doDisconnect() {
    try {
        await api('POST', '/disconnect');
    } catch (_) {}
    connected = false;
    document.getElementById('conn-status').textContent = 'Not connected';
    document.getElementById('conn-status').className = 'conn-status';
    document.getElementById('btn-connect').textContent = 'Connect';
    toast('Disconnected');
}

function toggleConnect() {
    if (connected) doDisconnect();
    else showConnectDialog();
}

// ---- Chat rendering ----
function renderMarkdown(text) {
    if (typeof marked !== 'undefined') {
        return marked.parse(text);
    }
    // Fallback: escape HTML and preserve newlines
    return text.replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
}

function addChat(role, content) {
    const div = document.createElement('div');
    div.className = 'chat-msg';
    const body = role === 'assistant' ? renderMarkdown(content)
        : content.replace(/</g, '&lt;').replace(/>/g, '&gt;');
    div.innerHTML = `<div class="role ${role}">${role}</div><div class="body">${body}</div>`;
    const history = document.getElementById('chat-history');
    history.appendChild(div);
    history.scrollTop = history.scrollHeight;
}

function addChatSQL(sql) {
    const div = document.createElement('div');
    div.className = 'chat-msg';
    const escaped = sql.replace(/</g, '&lt;').replace(/>/g, '&gt;');
    div.innerHTML = `<div class="role assistant">SQL</div><div class="body"><pre>${escaped}</pre></div>`;
    const history = document.getElementById('chat-history');
    history.appendChild(div);
    history.scrollTop = history.scrollHeight;
}

// ---- Results rendering ----
function renderResults(data) {
    const toolbar = document.getElementById('results-info');
    const scroll = document.getElementById('results-scroll');

    if (!data.columns || data.columns.length === 0) {
        toolbar.textContent = data.rowcount != null ? `${data.rowcount} row(s) affected` : '';
        scroll.innerHTML = `<div class="empty-state"><p>${data.note || 'Query executed successfully'}</p></div>`;
        return;
    }

    toolbar.textContent = `${data.rowcount} row${data.rowcount !== 1 ? 's' : ''}`;

    let html = '<table class="results-table"><thead><tr>';
    data.columns.forEach(c => { html += `<th>${c}</th>`; });
    html += '</tr></thead><tbody>';
    const maxRows = Math.min(data.rows.length, 500);
    for (let i = 0; i < maxRows; i++) {
        html += '<tr>';
        data.rows[i].forEach(v => {
            if (v === null || v === undefined) {
                html += '<td class="null">NULL</td>';
            } else {
                const s = String(v).replace(/</g, '&lt;').replace(/>/g, '&gt;');
                html += `<td>${s}</td>`;
            }
        });
        html += '</tr>';
    }
    html += '</tbody></table>';
    if (data.rows.length > 500) {
        html += `<div class="commentary">Showing 500 of ${data.rows.length} rows</div>`;
    }
    scroll.innerHTML = html;
}

// ---- SSE streaming ----
function streamQuery(body) {
    return new Promise((resolve) => {
        setBusy(true);
        let gotTerminal = false;

        fetch('/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body),
        }).then(response => {
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            function drainBuffer() {
                const parts = buffer.split('\n\n');
                buffer = parts.pop();
                for (const part of parts) {
                    if (!part.trim() || part.trim().startsWith(':')) continue;
                    let eventType = 'message';
                    let dataLines = [];
                    for (const line of part.split('\n')) {
                        if (line.startsWith('event: ')) eventType = line.slice(7).trim();
                        else if (line.startsWith('data: ')) dataLines.push(line.slice(6));
                        else if (line.startsWith('data:')) dataLines.push(line.slice(5));
                    }
                    const raw = dataLines.join('\n');
                    if (!raw) continue;
                    try {
                        const parsed = JSON.parse(raw);
                        handleSSE(eventType, parsed);
                        if (['result', 'error', 'cancelled', 'destructive'].includes(eventType)) {
                            gotTerminal = true;
                        }
                    } catch (_) {}
                }
            }

            function pump() {
                reader.read().then(({ done, value }) => {
                    if (done) {
                        if (buffer.trim()) { buffer += '\n\n'; drainBuffer(); }
                        if (!gotTerminal) {
                            addChat('assistant', 'No response received');
                        }
                        clearProgressLine();
                        setBusy(false);
                        resolve();
                        return;
                    }
                    buffer += decoder.decode(value, { stream: true });
                    drainBuffer();
                    pump();
                }).catch(e => {
                    if (!gotTerminal) {
                        addChat('assistant', 'Connection lost: ' + (e.message || ''));
                    }
                    clearProgressLine();
                    setBusy(false);
                    resolve();
                });
            }
            pump();
        }).catch(e => {
            addChat('assistant', 'Error: ' + e.message);
            toast(e.message, 'error');
            clearProgressLine();
            setBusy(false);
            resolve();
        });
    });
}

function handleSSE(eventType, data) {
    switch (eventType) {
        case 'progress':
            setProgress(data.message);
            break;
        case 'sql':
            clearProgressLine();
            if (data.sql) {
                addChatSQL(data.sql);
                if (editor) editor.setValue(data.sql);
                // Auto-open right panel and switch to editor tab
                showPanelTab('editor');
            }
            if (data.commentary) addChat('assistant', data.commentary);
            break;
        case 'result':
            clearProgressLine();
            renderResults(data);
            // Auto-switch to results tab
            if (data.columns && data.columns.length > 0) {
                showPanelTab('results');
            }
            break;
        case 'destructive':
            clearProgressLine();
            showDestructiveDialog(data);
            setBusy(false);
            break;
        case 'error':
            clearProgressLine();
            addChat('assistant', 'Error: ' + (data.error || 'Unknown error'));
            toast(data.error || 'Error', 'error');
            break;
        case 'cancelled':
            clearProgressLine();
            break;
    }
}

/** Show the right panel (if collapsed) and switch to the given tab. */
function showPanelTab(tabName) {
    const panel = document.getElementById('right-panel');
    if (panel.classList.contains('collapsed')) {
        toggleRightPanel();
    }
    switchTab(tabName);
}

async function sendQuery(nl) {
    if (busy || !nl.trim()) return;
    addChat('user', nl);
    await streamQuery({ nl });
}

async function runEditorSQL() {
    if (busy || !editor) return;
    const sql = editor.getValue().trim();
    if (!sql || sql.startsWith('--')) return;
    addChat('user', 'sql: ' + (sql.length > 120 ? sql.slice(0, 120) + '...' : sql));
    await streamQuery({ sql });
}

// ---- Destructive operation confirmation ----
function showDestructiveDialog(data) {
    const overlay = document.getElementById('destructive-dialog');
    overlay.classList.remove('hidden');
    document.getElementById('destructive-info').textContent =
        `${data.operation}: ${data.sql.slice(0, 200)}`;
    document.getElementById('destructive-pass').value = '';
    document.getElementById('destructive-error').textContent = '';
    overlay._pendingSQL = data.sql;
}

async function confirmDestructive() {
    const overlay = document.getElementById('destructive-dialog');
    const password = document.getElementById('destructive-pass').value;
    if (!password) {
        document.getElementById('destructive-error').textContent = 'Password required';
        return;
    }
    setBusy(true);
    try {
        const data = await api('POST', '/query', { sql: overlay._pendingSQL, password });
        renderResults(data);
        overlay.classList.add('hidden');
        toast('Operation completed', 'success');
        showPanelTab('results');
    } catch (e) {
        document.getElementById('destructive-error').textContent = e.message;
    }
    setBusy(false);
}

// ---- Schema browser ----
async function searchSchema() {
    const q = document.getElementById('schema-q').value.trim();
    if (!q) return;
    const container = document.getElementById('schema-results');
    container.innerHTML = '<div style="padding:8px;color:var(--text-dim)">Searching...</div>';
    try {
        const data = await api('GET', `/schema/search?q=${encodeURIComponent(q)}`);
        container.innerHTML = '';
        (data.results || []).forEach(r => {
            const div = document.createElement('div');
            div.className = 'schema-item';
            div.innerHTML = `<span class="tname">${r.table}</span><span class="tdesc">${r.summary || ''}</span>`;
            div.onclick = () => loadTableDetails(r.table);
            container.appendChild(div);
        });
        if (!data.results || data.results.length === 0) {
            container.innerHTML = '<div style="padding:8px;color:var(--text-dim)">No tables found</div>';
        }
    } catch (e) {
        container.innerHTML = `<div class="error-msg" style="padding:8px">${e.message}</div>`;
    }
}

async function loadTableDetails(name) {
    const container = document.getElementById('schema-detail');
    container.innerHTML = '<div style="padding:16px;color:var(--text-dim)">Loading...</div>';
    showPanelTab('schema');

    try {
        const data = await api('GET', `/schema/table/${encodeURIComponent(name)}`);
        renderSchemaDetail(data, name);
    } catch (e) {
        container.innerHTML = `<div class="empty-state"><p style="color:var(--red)">${e.message}</p></div>`;
    }
}

function renderSchemaDetail(data, fallbackName) {
    const container = document.getElementById('schema-detail');
    const tableName = data.TABLE_NAME || fallbackName;
    const cols = data.COLUMNS || {};
    const joins = data.POSSIBLE_JOINS || {};

    // Build set of indexed columns from INDEXES array (if present)
    const indexedCols = new Set();
    const indexes = data.INDEXES || data.indexes || [];
    for (const idx of indexes) {
        const idxCols = idx.columns || idx.COLUMNS || [];
        for (const c of idxCols) indexedCols.add(c);
    }

    let html = '';

    // Header
    html += `<div class="schema-header">`;
    html += `<span class="schema-table-name">${tableName}</span>`;
    html += `<span class="schema-col-count">${Object.keys(cols).length} columns</span>`;
    html += `</div>`;

    // Scrollable body: columns + joins together
    html += '<div class="schema-body-scroll">';

    // Columns table
    html += '<table class="schema-columns">';
    html += '<thead><tr><th>Column</th><th>Type</th><th>Len</th><th>Null</th><th>Keys</th></tr></thead>';
    html += '<tbody>';
    for (const [col, info] of Object.entries(cols)) {
        const type = info.DATA_TYPE || info.data_type || '';
        const len = info.COLUMN_LENGTH || '';
        const nullable = info.NULLABLE === false || info.NULLABLE === 'N' ? 'NO' : '';

        let keys = '';
        const isPK = info.PK || info.pk;
        const isFK = info.FK_TARGET || info.fk_target || info.fk;
        const fkTarget = info.FK_TARGET || info.fk_target || '';
        const isIndexed = indexedCols.has(col) || info.INDEX || info.indexed;
        if (isPK) keys += '<span class="key-badge pk">PK</span>';
        if (isIndexed && !isPK) keys += '<span class="key-badge idx">IDX</span>';
        if (fkTarget) {
            const fkTable = fkTarget.split('.')[0];
            keys += `<a class="key-badge fk" href="#" onclick="loadTableDetails('${fkTable}');return false;" title="${fkTarget}">FK &rarr; ${fkTable}</a>`;
        } else if (isFK) {
            keys += '<span class="key-badge fk" style="cursor:default">FK</span>';
        }

        // Domain values
        let domainHtml = '';
        if (info.DOMAIN && info.DOMAIN.VALUES) {
            const vals = info.DOMAIN.VALUES;
            const entries = Object.entries(vals).slice(0, 8);
            domainHtml = '<div class="col-domain">' +
                entries.map(([k, v]) => `<span class="domain-val" title="${v}">${k}</span>`).join('') +
                (Object.keys(vals).length > 8 ? `<span class="domain-more">+${Object.keys(vals).length - 8}</span>` : '') +
                '</div>';
        }

        html += `<tr>`;
        html += `<td class="col-name">${col}${domainHtml}</td>`;
        html += `<td class="col-type">${type}</td>`;
        html += `<td class="col-len">${len}</td>`;
        html += `<td class="col-null">${nullable}</td>`;
        html += `<td class="col-keys">${keys}</td>`;
        html += `</tr>`;
    }
    html += '</tbody></table>';

    // Possible joins (inside same scroll container)
    const joinEntries = Object.entries(joins);
    if (joinEntries.length > 0) {
        html += '<div class="schema-joins">';
        html += '<div class="schema-joins-title">Possible Joins</div>';
        for (const [target, condition] of joinEntries) {
            html += `<div class="schema-join-item">`;
            html += `<a href="#" onclick="loadTableDetails('${target}');return false;" class="join-target">${target}</a>`;
            html += `<span class="join-condition">${condition}</span>`;
            html += `</div>`;
        }
        html += '</div>';
    }

    html += '</div>'; // close schema-body-scroll

    container.innerHTML = html;
}

// ---- Resize handle ----
function initResize() {
    const hHandle = document.getElementById('resize-h');
    const chatPanel = document.querySelector('.chat-panel');
    const rightPanel = document.getElementById('right-panel');

    hHandle.addEventListener('mousedown', function (e) {
        e.preventDefault();
        hHandle.classList.add('active');
        const startX = e.clientX;
        const totalW = chatPanel.offsetWidth + rightPanel.offsetWidth;
        const startChatW = chatPanel.offsetWidth;

        function onMove(e) {
            const chatW = startChatW + (e.clientX - startX);
            const rightW = totalW - chatW;
            if (chatW >= 320 && rightW >= 300) {
                chatPanel.style.flex = 'none';
                chatPanel.style.width = chatW + 'px';
                rightPanel.style.width = rightW + 'px';
            }
        }
        function onUp() {
            hHandle.classList.remove('active');
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
            if (editor) editor.layout();
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
}

// ---- Event wiring ----
document.addEventListener('DOMContentLoaded', function () {
    // Configure marked for chat rendering
    if (typeof marked !== 'undefined') {
        marked.setOptions({ breaks: true, gfm: true });
    }

    initMonaco();
    initResize();

    // Right panel starts visible with editor tab
    document.getElementById('btn-toggle-panel').classList.add('active');

    // Chat input: Enter submits, Shift+Enter newline
    const input = document.getElementById('chat-input');
    input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const val = input.value.trim();
            if (val) { sendQuery(val); input.value = ''; }
        }
    });

    // Ask button
    document.getElementById('btn-ask').onclick = function () {
        const val = input.value.trim();
        if (val) { sendQuery(val); input.value = ''; }
    };

    // Run SQL from editor
    document.getElementById('btn-run').onclick = runEditorSQL;

    // Cancel button
    document.getElementById('btn-cancel').onclick = cancelQuery;

    // Panel toggles
    document.getElementById('btn-toggle-panel').onclick = toggleRightPanel;
    document.getElementById('btn-toggle-schema').onclick = toggleSchemaDrawer;

    // Tab switching
    document.querySelectorAll('.panel-tab').forEach(tab => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    // Connect button
    document.getElementById('btn-connect').onclick = toggleConnect;
    document.getElementById('btn-connect-confirm').onclick = doConnect;
    document.getElementById('btn-connect-cancel').onclick = hideConnectDialog;

    // Destructive dialog
    document.getElementById('btn-destructive-confirm').onclick = confirmDestructive;
    document.getElementById('btn-destructive-cancel').onclick = function () {
        document.getElementById('destructive-dialog').classList.add('hidden');
    };

    // Schema search
    document.getElementById('btn-schema-search').onclick = searchSchema;
    document.getElementById('schema-q').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') searchSchema();
    });

    // Enter in password fields
    document.getElementById('conn-pass').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') doConnect();
    });
    document.getElementById('destructive-pass').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') confirmDestructive();
    });
});
