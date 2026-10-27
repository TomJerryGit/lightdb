/* LightDB Studio Web 前端
 *
 * 安全约定：后端返回的文本一律用 textContent（或 DOM 节点）渲染，
 * **禁止 innerHTML 拼接未转义数据**；fetch 非 2xx 统一提取后端 detail。
 */
const API = '/api/v1';
let token = localStorage.getItem('lightdb_token') || '';
let currentConn = '';
let currentDB = '';
let currentTable = '';
let connectionsCache = [];
let dataOffset = 0;
let dataTotal = 0;
let dataOrderBy = '';
let dataOrderDesc = false;
let designColumns = [];
let lastQueryResult = null;
let erController = null;
let currentChartKind = 'bar';
let chartSeriesVisible = true;
let activeImportJob = '';
let importPollTimer = null;
let reportTemplates = [];
let selectedReport = '';

// ==================== 工具 ====================

function $id(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clearNode(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

function setStatus(id, text, isError) {
  const node = $id(id);
  if (!node) return;
  node.textContent = text || '';
  node.classList.toggle('status-error', !!isError);
  node.classList.toggle('status-ok', !isError && !!text);
}

function setBusy(button, busy, busyText) {
  if (!button) return false;
  if (busy) {
    if (button.disabled) return false;
    button.dataset.origText = button.textContent;
    button.textContent = busyText || '处理中...';
    button.disabled = true;
    button.classList.add('is-busy');
    return true;
  }
  if (button.dataset.origText !== undefined) {
    button.textContent = button.dataset.origText;
    delete button.dataset.origText;
  }
  button.disabled = false;
  button.classList.remove('is-busy');
  return true;
}

function extractDetail(data, fallback) {
  if (!data) return fallback;
  const d = data.detail;
  if (typeof d === 'string' && d) return d;
  if (d && typeof d === 'object') {
    if (d.detail) return String(d.detail);
    if (d.code) return String(d.code);
    try { return JSON.stringify(d); } catch (e) { return fallback; }
  }
  if (data.error) return String(data.error);
  return fallback;
}

function filenameFromDisposition(cd, fallback) {
  if (!cd) return fallback;
  let m = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (m) {
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }
  m = cd.match(/filename="?([^";]+)"?/i);
  return m ? m[1] : fallback;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || 'download';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  if (resp.status === 401) {
    logout();
    throw new Error('登录已过期，请重新登录');
  }
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (!resp.ok) {
    const err = new Error(extractDetail(data, 'HTTP ' + resp.status));
    err.status = resp.status;
    throw err;
  }
  return data;
}

async function apiBlob(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  if (resp.status === 401) {
    logout();
    throw new Error('登录已过期，请重新登录');
  }
  if (!resp.ok) {
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    throw new Error(extractDetail(data, 'HTTP ' + resp.status));
  }
  const cd = resp.headers.get('Content-Disposition') || '';
  const blob = await resp.blob();
  return { blob, cd, filename: filenameFromDisposition(cd, '') };
}

// ==================== 登录 / 会话 ====================

async function doLogin() {
  const user = $id('login-user').value.trim();
  const pass = $id('login-pass').value;
  const btn = $id('login-btn');
  $id('login-error').textContent = '';
  setBusy(btn, true, '登录中...');
  try {
    const r = await fetch(API + '/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password: pass }),
    });
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok || !data || !data.ok) {
      $id('login-error').textContent = extractDetail(data, '登录失败');
      return;
    }
    token = data.token;
    localStorage.setItem('lightdb_token', token);
    showMain(data.user);
  } catch (e) {
    $id('login-error').textContent = '网络错误: ' + e.message;
  } finally {
    setBusy(btn, false);
  }
}

function logout() {
  token = '';
  localStorage.removeItem('lightdb_token');
  $id('main-page').classList.add('hidden');
  $id('login-page').classList.remove('hidden');
}

function showMain(user) {
  $id('login-page').classList.add('hidden');
  $id('main-page').classList.remove('hidden');
  $id('user-label').textContent = (user && user.username)
    ? `${user.username} (${user.role})` : '';
  loadConnections();
  loadHistory();
}

// ==================== 连接 / 对象树 ====================

async function loadConnections() {
  try {
    const data = await api('/connections');
    const sel = $id('conn-select');
    clearNode(sel);
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '选择连接...';
    sel.appendChild(placeholder);
    (data || []).forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.name;
      opt.textContent = `${c.name} (${c.host}:${c.port})`;
      sel.appendChild(opt);
    });
    renderConnDetail(data || []);
    connectionsCache = data || [];
  } catch (e) {
    setStatus('exec-status', '加载连接失败: ' + e.message, true);
  }
}

async function onConnChange() {
  currentConn = $id('conn-select').value;
  renderConnDetail(connectionsCache);
  if (!currentConn) {
    clearNode($id('object-tree'));
    return;
  }
  await loadDatabases();
}

async function loadDatabases() {
  const tree = $id('object-tree');
  clearNode(tree);
  tree.appendChild(el('div', 'tree-loading', '加载中...'));
  try {
    const data = await api('/databases?connection=' +
      encodeURIComponent(currentConn));
    clearNode(tree);
    (data.databases || []).forEach(db => {
      const item = el('div', 'tree-item');
      item.appendChild(el('span', 'icon', '🗄'));
      item.appendChild(el('span', 'tree-label', db));
      item.onclick = () => toggleDatabase(item, db);
      tree.appendChild(item);
    });
  } catch (e) {
    clearNode(tree);
    tree.appendChild(el('div', 'result-error', '加载失败: ' + e.message));
  }
}

async function toggleDatabase(elNode, db) {
  const next = elNode.nextElementSibling;
  if (next && next.classList.contains('tree-children')) {
    next.remove();
    return;
  }
  const children = el('div', 'tree-children');
  children.appendChild(el('div', 'tree-loading', '加载中...'));
  elNode.after(children);
  try {
    const data = await api('/databases/' + encodeURIComponent(db) +
      '/tables?connection=' + encodeURIComponent(currentConn));
    clearNode(children);
    (data.tables || []).forEach(t => {
      const item = el('div', 'tree-item');
      item.appendChild(el('span', 'icon', '📋'));
      item.appendChild(el('span', 'tree-label', t.name));
      item.appendChild(el('span', 'tree-count', `(${t.rows})`));
      item.onclick = (e) => {
        e.stopPropagation();
        selectTable(t.name, db);
      };
      children.appendChild(item);
    });
  } catch (e) {
    clearNode(children);
    children.appendChild(el('div', 'result-error', '加载失败: ' + e.message));
  }
}

function selectTable(table, db) {
  currentDB = db;
  currentTable = table;
  dataOffset = 0;
  $id('sql-editor').value =
    `SELECT * FROM \`${table}\` LIMIT 100;`;
  runQuery();
}

// ==================== 表数据 / 分页排序 ====================

function connQuery(extra) {
  const parts = [];
  if (currentConn) parts.push('connection=' + encodeURIComponent(currentConn));
  if (extra) parts.push(extra);
  return parts.length ? '?' + parts.join('&') : '';
}

function renderConnDetail(connections) {
  const box = $id('conn-detail');
  if (!box) return;
  clearNode(box);
  if (!connections || !connections.length) return;
  const c = connections.find(x => x.name === currentConn) || connections[0];
  const ssh = c.ssh || {status: 'disabled'};
  const ssl = c.ssl || {status: 'disabled'};
  box.appendChild(el('div', 'conn-line',
    `SSH: ${ssh.status}${ssh.enabled ? ' · ' + (ssh.host || '') + ':' + (ssh.port || '') : ''}`));
  box.appendChild(el('div', 'conn-line',
    `SSL: ${ssl.status}${ssl.enabled ? ' · CA ' + (ssl.has_ca ? '已配置' : '未配置') : ''}`));
}

async function loadTableData(table, db, offset) {
  if (!table || !db) {
    setStatus('exec-status', '请先在对象树选择表', true);
    return;
  }
  currentTable = table;
  currentDB = db;
  dataOffset = Math.max(0, offset || 0);
  const order = dataOrderBy
    ? '&order_by=' + encodeURIComponent(dataOrderBy) +
      '&order_desc=' + (dataOrderDesc ? 'true' : 'false')
    : '';
  setStatus('exec-status', '加载表数据...', false);
  try {
    const q = connQuery('limit=100&offset=' + dataOffset + order);
    const data = await api('/databases/' + encodeURIComponent(db) +
      '/tables/' + encodeURIComponent(table) + '/data' + q);
    renderTable(data.columns || [], data.rows || []);
    lastQueryResult = {columns: data.columns || [], rows: data.rows || []};
    dataTotal = data.total === null || data.total === undefined
      ? (data.rows || []).length : data.total;
    $id('table-controls').classList.remove('hidden');
    $id('table-current').textContent = `表 ${db}.${table}`;
    $id('data-page-info').textContent =
      `第 ${Math.floor(dataOffset / 100) + 1} 页 · 共 ${dataTotal} 行`;
    $id('data-prev-btn').disabled = dataOffset <= 0;
    $id('data-next-btn').disabled = dataOffset + 100 >= dataTotal;
    const sortSel = $id('sort-column-select');
    clearNode(sortSel);
    const first = el('option', '', '默认排序');
    first.value = '';
    sortSel.appendChild(first);
    (data.columns || []).forEach(c => {
      const o = el('option', '', String(c));
      o.value = String(c);
      sortSel.appendChild(o);
    });
    setStatus('exec-status', '表数据已加载', false);
  } catch (e) {
    setStatus('exec-status', '加载失败: ' + e.message, true);
    clearNode($id('result-table'));
    $id('result-table').appendChild(el('div', 'result-error', e.message));
  }
}

function onSortColumnChange() {
  dataOrderBy = $id('sort-column-select').value;
  dataOrderDesc = false;
  loadTableData(currentTable, currentDB, 0);
}

async function explainCurrent() {
  const sql = $id('sql-editor').value.trim();
  if (!sql) { setStatus('exec-status', '请输入 SQL', true); return; }
  const btn = $id('explain-btn');
  if (!setBusy(btn, true, 'EXPLAIN...')) return;
  try {
    const data = await api('/explain', {
      method: 'POST',
      body: JSON.stringify({sql: sql, connection: currentConn,
                            database: currentDB}),
    });
    renderTable(data.columns, data.rows);
    $id('result-info').textContent = `EXPLAIN 返回 ${data.row_count} 行`;
    setStatus('exec-status', 'EXPLAIN 完成', false);
  } catch (e) {
    setStatus('exec-status', 'EXPLAIN 失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

async function exportCurrentQuery() {
  const sql = $id('sql-editor').value.trim();
  if (!sql) { setStatus('exec-status', '请输入 SQL', true); return; }
  const btn = $id('export-query-btn');
  if (!setBusy(btn, true, '导出中...')) return;
  try {
    const res = await apiBlob('/query/export', {
      method: 'POST',
      body: JSON.stringify({sql: sql, connection: currentConn,
                            database: currentDB, format: 'csv'}),
    });
    downloadBlob(res.blob, res.filename || 'query_result.csv');
    setStatus('exec-status', '查询结果已导出 CSV', false);
  } catch (e) {
    setStatus('exec-status', '导出失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

async function exportCurrentTable() {
  if (!currentTable || !currentDB) {
    setStatus('exec-status', '请先加载表数据', true);
    return;
  }
  const btn = $id('export-table-btn');
  if (!setBusy(btn, true, '导出中...')) return;
  try {
    const q = connQuery('');
    const res = await apiBlob('/databases/' + encodeURIComponent(currentDB) +
      '/tables/' + encodeURIComponent(currentTable) + '/export?fmt=csv' + q);
    downloadBlob(res.blob, res.filename || (currentTable + '.csv'));
    setStatus('exec-status', '表 CSV 已导出', false);
  } catch (e) {
    setStatus('exec-status', '导出失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

// ==================== 表设计器 ====================

async function openDesigner() {
  if (!currentTable || !currentDB) {
    setStatus('exec-status', '请先在对象树选择表', true);
    return;
  }
  const btn = $id('design-btn');
  if (!setBusy(btn, true, '读取结构...')) return;
  try {
    const data = await api('/databases/' + encodeURIComponent(currentDB) +
      '/tables/' + encodeURIComponent(currentTable) + '/design' +
      connQuery(''));
    designColumns = data.columns || [];
    $id('design-columns-input').value =
      JSON.stringify(designColumns, null, 2);
    $id('design-panel').classList.remove('hidden');
    $id('design-sql-preview').textContent = '';
    setStatus('design-status', '已读取列/索引；修改 JSON 后预览或保存', false);
  } catch (e) {
    setStatus('exec-status', '读取设计器失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

async function submitDesign(dryRun) {
  const btn = $id(dryRun ? 'design-preview-btn' : 'design-save-btn');
  let columns;
  try {
    columns = JSON.parse($id('design-columns-input').value);
    if (!Array.isArray(columns)) throw new Error('JSON 必须是列数组');
  } catch (e) {
    setStatus('design-status', 'JSON 解析失败: ' + e.message, true);
    return;
  }
  if (!dryRun && !window.confirm('保存 ALTER？DROP COLUMN 不可逆，请确认已备份。')) {
    return;
  }
  if (!setBusy(btn, true, dryRun ? '预览中...' : '保存中...')) return;
  try {
    const data = await api('/databases/' + encodeURIComponent(currentDB) +
      '/tables/' + encodeURIComponent(currentTable) + '/design' +
      connQuery(''), {
      method: 'POST',
      body: JSON.stringify({columns: columns, dry_run: dryRun,
                            connection: currentConn}),
    });
    const stmts = data.statements || [];
    $id('design-sql-preview').textContent = stmts.join('\n') || '（无变更）';
    setStatus('design-status',
      dryRun ? `预览 ${stmts.length} 条 ALTER（未执行）`
             : `已执行 ${data.count} 条 ALTER`, false);
    if (!dryRun) {
      await openDesigner();
      await loadTableData(currentTable, currentDB, 0);
    }
  } catch (e) {
    setStatus('design-status', '保存失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

// ==================== 导入 / 备份 / 结构对比 ====================

function triggerImport() {
  if (!currentTable || !currentDB) {
    setStatus('exec-status', '请先在对象树选择表', true);
    return;
  }
  $id('import-file-input').click();
}

async function onImportFile(ev) {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = '';
  if (!file) return;
  try {
    const text = await file.text();
    const data = await api('/databases/' + encodeURIComponent(currentDB) +
      '/tables/' + encodeURIComponent(currentTable) + '/import/start' +
      connQuery(''), {
      method: 'POST',
      body: JSON.stringify({csv: text, has_header: true}),
    });
    activeImportJob = data.job_id;
    $id('cancel-import-btn').classList.remove('hidden');
    setStatus('import-progress',
      `导入任务已启动：0/${data.total}`, false);
    stopImportPolling();
    importPollTimer = setInterval(() => pollImportJob(activeImportJob), 500);
  } catch (e) {
    setStatus('exec-status', '导入启动失败: ' + e.message, true);
  }
}

async function backupCurrent() {
  if (!currentDB) {
    setStatus('exec-status', '请先切换数据库', true);
    return;
  }
  const btn = $id('backup-btn');
  if (!setBusy(btn, true, '备份中...')) return;
  try {
    const res = await apiBlob('/backup', {
      method: 'POST',
      body: JSON.stringify({database: currentDB, connection: currentConn}),
    });
    downloadBlob(res.blob, res.filename || ('backup_' + currentDB + '.sql'));
    setStatus('exec-status', '逻辑备份已下载', false);
  } catch (e) {
    setStatus('exec-status', '备份失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

async function compareSchema() {
  if (!currentDB) {
    setStatus('exec-status', '请先切换数据库', true);
    return;
  }
  const target = window.prompt('目标数据库名（结构对比，只读）：', currentDB);
  if (!target) return;
  const btn = $id('sync-btn');
  if (!setBusy(btn, true, '对比中...')) return;
  try {
    const data = await api('/schema/compare?source_db=' +
      encodeURIComponent(currentDB) + '&target_db=' +
      encodeURIComponent(target) + connQuery(''));
    const s = data.summary || {};
    setStatus('exec-status',
      `结构对比：缺少表 ${(s.missing_in_target || []).length}，` +
      `新增表 ${(s.missing_in_source || []).length}，` +
      `变化表 ${s.changed_tables || 0}`, false);
    $id('result-table').appendChild(
      el('pre', 'sql-preview', JSON.stringify(data.differences || [], null, 2)));
  } catch (e) {
    setStatus('exec-status', '结构对比失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

// ==================== 查询 ====================

async function runQuery() {
  const sql = $id('sql-editor').value.trim();
  if (!sql) {
    setStatus('exec-status', '请输入 SQL', true);
    return;
  }
  const runBtn = $id('run-btn');
  if (!setBusy(runBtn, true, '执行中...')) return;
  setStatus('exec-status', '执行中...', false);
  $id('result-info').textContent = '执行中...';
  clearNode($id('result-table'));
  try {
    const data = await api('/query', {
      method: 'POST',
      body: JSON.stringify({
        sql: sql,
        connection: currentConn,
        database: currentDB,
      }),
    });
    if (!data.ok) {
      setStatus('exec-status', '失败', true);
      $id('result-info').textContent = '错误';
      $id('result-table').appendChild(
        el('div', 'result-error', data.error || '未知错误'));
      return;
    }
    if (data.columns && data.columns.length) {
      lastQueryResult = {columns: data.columns, rows: data.rows || []};
      renderTable(data.columns, data.rows);
      $id('result-info').textContent =
        `返回 ${data.row_count} 行 · ${data.elapsed_ms.toFixed(1)} ms`;
    } else {
      $id('result-info').textContent =
        `影响 ${data.affected} 行 · ${data.elapsed_ms.toFixed(1)} ms`;
      $id('result-table').appendChild(
        el('div', 'result-ok', `✓ 执行成功，影响 ${data.affected} 行`));
    }
    setStatus('exec-status', '完成', false);
    loadHistory();
  } catch (e) {
    setStatus('exec-status', '错误', true);
    clearNode($id('result-table'));
    $id('result-table').appendChild(el('div', 'result-error', e.message));
  } finally {
    setBusy(runBtn, false);
  }
}

function renderTable(cols, rows, targetId) {
  const target = $id(targetId || 'result-table');
  clearNode(target);
  if (!rows || rows.length === 0) {
    target.appendChild(el('div', 'empty-hint', '空结果'));
    return;
  }
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  cols.forEach(c => headRow.appendChild(el('th', '', c)));
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  rows.forEach(row => {
    const tr = document.createElement('tr');
    row.forEach(v => {
      if (v === null || v === undefined) {
        tr.appendChild(el('td', 'null-value', 'NULL'));
      } else if (typeof v === 'number') {
        tr.appendChild(el('td', 'num-value', String(v)));
      } else {
        tr.appendChild(el('td', '', String(v)));
      }
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  target.appendChild(table);
}

// ==================== SQL 格式化（后端 core.sql_formatter） ====================

async function formatSQL() {
  const sql = $id('sql-editor').value.trim();
  if (!sql) {
    setStatus('exec-status', '请输入 SQL', true);
    return;
  }
  const btn = $id('format-btn');
  if (!setBusy(btn, true, '格式化中...')) return;
  try {
    const data = await api('/sql/format', {
      method: 'POST',
      body: JSON.stringify({ sql: sql, minify: false }),
    });
    $id('sql-editor').value = data.sql;
    setStatus('exec-status',
      `格式化完成（${data.length_in} → ${data.length_out} 字符）`, false);
  } catch (e) {
    setStatus('exec-status', '格式化失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

// ==================== 历史 ====================

async function loadHistory() {
  try {
    const data = await api('/history?limit=30');
    const list = $id('history-list');
    clearNode(list);
    (data.entries || []).forEach(e => {
      const item = el('div', 'history-item');
      item.appendChild(el('div', 'sql', String(e.sql || '').slice(0, 80)));
      item.appendChild(el('div', 'time',
        new Date((e.ts || 0) * 1000).toLocaleTimeString()));
      item.onclick = () => { $id('sql-editor').value = e.sql || ''; };
      list.appendChild(item);
    });
  } catch (e) {
    console.error(e);
  }
}

// ==================== 报表模板 ====================

function switchTool(tool) {
  ['history', 'reports', 'quality', 'er', 'plugins'].forEach(name => {
    const btn = $id('tool-' + name + '-btn');
    if (btn) btn.classList.toggle('active', name === tool);
    const section = $id(name + '-section');
    if (section) section.classList.toggle('hidden', name !== tool);
  });
  if (tool === 'reports') loadReportTemplates();
  if (tool === 'er') loadER();
  if (tool === 'plugins') loadPlugins();
}

function selectedTemplate() {
  return reportTemplates.find(t => t.name === selectedReport) || null;
}

function parseReportParams() {
  const text = $id('report-params-input').value.trim();
  if (!text) return null;
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === 'object') return parsed;
  } catch (e) { /* 继续按 key=value 解析 */ }
  const lines = text.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  if (lines.every(line => line.includes('='))) {
    const obj = {};
    lines.forEach(line => {
      const idx = line.indexOf('=');
      obj[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    });
    return obj;
  }
  if (lines.every(line => !line.includes('='))) return lines;
  throw new Error('参数格式错误：请使用 JSON，或每行 key=value / 每行一个值');
}

async function loadReportTemplates() {
  const btn = $id('report-refresh-btn');
  setBusy(btn, true, '刷新中...');
  try {
    const data = await api('/reports/templates');
    reportTemplates = data.templates || [];
    if (selectedReport && !selectedTemplate()) selectedReport = '';
    renderReportList();
    setStatus('report-status', `已加载 ${reportTemplates.length} 个模板`, false);
  } catch (e) {
    setStatus('report-status', '加载模板失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

function renderReportList() {
  const list = $id('report-list');
  clearNode(list);
  if (!reportTemplates.length) {
    list.appendChild(el('div', 'empty-hint', '暂无报表模板，可先保存当前 SQL'));
    return;
  }
  reportTemplates.forEach(t => {
    const row = el('div', 'report-item');
    if (t.name === selectedReport) row.classList.add('selected');
    const title = el('div', 'report-title', t.name);
    const meta = el('div', 'report-meta',
      `${t.description || '无描述'} · 参数: ` +
      (Object.keys(t.params || {}).join(',') || '无') +
      ` · 预览上限 ${t.preview_limit}`);
    const actions = el('div', 'btn-row');
    const mk = (text, action, fmt) => {
      const btn = document.createElement('button');
      btn.className = 'btn-small';
      btn.textContent = text;
      if (action) btn.dataset.action = action;
      if (fmt) btn.dataset.format = fmt;
      return btn;
    };
    const previewBtn = mk('预览', 'preview-template-item');
    previewBtn.onclick = (e) => { e.stopPropagation(); previewReport(t.name, previewBtn); };
    const deleteBtn = mk('删除', 'delete-template');
    deleteBtn.onclick = (e) => { e.stopPropagation(); deleteReportTemplate(t.name, deleteBtn); };
    actions.appendChild(previewBtn);
    ['html', 'csv', 'xlsx'].forEach(fmt => {
      const b = mk('导出 ' + fmt.toUpperCase(), 'export-template', fmt);
      b.onclick = (e) => { e.stopPropagation(); exportReport(t.name, fmt, b); };
      actions.appendChild(b);
    });
    actions.appendChild(deleteBtn);
    row.appendChild(title);
    row.appendChild(meta);
    row.appendChild(actions);
    row.onclick = () => {
      selectedReport = t.name;
      $id('report-name-input').value = t.name;
      renderReportList();
    };
    list.appendChild(row);
  });
}

async function saveReportTemplate() {
  const name = $id('report-name-input').value.trim();
  const sql = $id('sql-editor').value.trim();
  if (!name) { setStatus('report-status', '请输入模板名称', true); return; }
  if (!sql) { setStatus('report-status', 'SQL 不能为空', true); return; }
  const btn = $id('report-save-btn');
  if (!setBusy(btn, true, '保存中...')) return;
  try {
    const data = await api('/reports/templates', {
      method: 'POST',
      body: JSON.stringify({ name: name, description: '', sql: sql }),
    });
    selectedReport = data.template.name;
    setStatus('report-status', `已保存模板: ${selectedReport}`, false);
    await loadReportTemplates();
  } catch (e) {
    setStatus('report-status', '保存失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

async function previewReport(name, btn) {
  let params;
  try { params = parseReportParams(); }
  catch (e) { setStatus('report-status', e.message, true); return; }
  if (!setBusy(btn, true, '预览中...')) return;
  try {
    const data = await api(
      '/reports/templates/' + encodeURIComponent(name) + '/preview', {
        method: 'POST',
        body: JSON.stringify({ params: params }),
      });
    renderTable(data.columns || [], data.rows || [],
      'report-preview-output');
    setStatus('report-status',
      `模板「${name}」共 ${data.total} 行，预览 ${data.returned} 行` +
      `（上限 ${data.limit || '模板默认'}）`, false);
  } catch (e) {
    setStatus('report-status', '预览失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

async function deleteReportTemplate(name, btn) {
  if (!window.confirm(`确认删除报表模板「${name}」？`)) return;
  if (!setBusy(btn, true, '删除中...')) return;
  try {
    await api('/reports/templates/' + encodeURIComponent(name), {
      method: 'DELETE',
    });
    if (selectedReport === name) selectedReport = '';
    setStatus('report-status', `已删除模板: ${name}`, false);
    await loadReportTemplates();
  } catch (e) {
    setStatus('report-status', '删除失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

async function exportReport(name, fmt, btn) {
  let params;
  try { params = parseReportParams(); }
  catch (e) { setStatus('report-status', e.message, true); return; }
  if (!setBusy(btn, true, '导出中...')) return;
  try {
    const res = await apiBlob(
      '/reports/templates/' + encodeURIComponent(name) +
      '/export?format=' + encodeURIComponent(fmt), {
        method: 'POST',
        body: JSON.stringify({ params: params }),
      });
    const filename = res.filename || (name + '.' + fmt);
    downloadBlob(res.blob, filename);
    setStatus('report-status',
      `已导出「${name}」为 ${fmt.toUpperCase()}（${filename}）`, false);
  } catch (e) {
    setStatus('report-status', '导出失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

// ==================== 数据质量 ====================

async function runQualityCheck() {
  const table = $id('quality-table-input').value.trim();
  if (!table) {
    setStatus('quality-status', '请输入表名', true);
    return;
  }
  const btn = $id('quality-check-btn');
  if (!setBusy(btn, true, '检查中...')) return;
  setStatus('quality-status', '检查中...', false);
  clearNode($id('quality-issues'));
  $id('quality-summary').textContent = '';
  try {
    const data = await api('/quality/check', {
      method: 'POST',
      body: JSON.stringify({ table: table }),
    });
    const s = data.summary || {};
    $id('quality-summary').textContent =
      `表 ${data.table}: 共 ${s.total || 0} 项` +
      `（error ${s.error || 0} / warning ${s.warning || 0} / info ${s.info || 0}）`;
    renderQualityIssues(data.issues || []);
    setStatus('quality-status',
      (data.issues || []).length ? '检查完成' : '未发现问题', false);
  } catch (e) {
    setStatus('quality-status', '检查失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

function renderQualityIssues(issues) {
  const target = $id('quality-issues');
  clearNode(target);
  if (!issues.length) {
    target.appendChild(el('div', 'empty-hint', '未发现问题'));
    return;
  }
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const hr = document.createElement('tr');
  ['级别', '分类', '表', '列', '消息'].forEach(h =>
    hr.appendChild(el('th', '', h)));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  issues.forEach(issue => {
    const tr = document.createElement('tr');
    const level = String(issue.level || 'info');
    tr.appendChild(el('td', 'level-' + level, level));
    tr.appendChild(el('td', '', issue.category || ''));
    tr.appendChild(el('td', '', issue.table || ''));
    tr.appendChild(el('td', '', issue.column || ''));
    tr.appendChild(el('td', '', issue.message || ''));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  target.appendChild(table);
}

// ==================== task-63：图表 / ER / 插件 / 流式 ====================

function tr(key, fallback) {
  return (typeof window.t === 'function') ? window.t(key, fallback) : fallback;
}

function chartTitle(text) {
  const NS = 'http://www.w3.org/2000/svg';
  const title = document.createElementNS(NS, 'title');
  title.textContent = text;
  return title;
}

function currentChartSvg() {
  const out = $id('chart-output');
  return out ? out.querySelector('svg') : null;
}

function exportChartSVG() {
  const svg = currentChartSvg();
  if (!svg) {
    setStatus('exec-status', '当前没有可导出的图表', true);
    return;
  }
  const source = new XMLSerializer().serializeToString(svg);
  const blob = new Blob([source], {type: 'image/svg+xml;charset=utf-8'});
  downloadBlob(blob, 'chart_' + currentChartKind + '.svg');
}

function exportChartPNG() {
  const svg = currentChartSvg();
  if (!svg) {
    setStatus('exec-status', '当前没有可导出的图表', true);
    return;
  }
  const source = new XMLSerializer().serializeToString(svg);
  const blob = new Blob([source], {type: 'image/svg+xml;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => {
    const canvas = document.createElement('canvas');
    canvas.width = 660; canvas.height = 300;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#1e1e1e';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    URL.revokeObjectURL(url);
    canvas.toBlob(b => downloadBlob(b, 'chart_' + currentChartKind + '.png'),
                  'image/png');
  };
  img.onerror = () => {
    URL.revokeObjectURL(url);
    setStatus('exec-status', 'PNG 导出失败（浏览器不支持 SVG 绘制）', true);
  };
  img.src = url;
}

function switchChartKind() {
  renderChart(currentChartKind === 'bar' ? 'line' : 'bar');
}

function showChartMessage(message) {
  const out = $id('chart-output');
  $id('chart-panel').classList.remove('hidden');
  clearNode(out);
  out.appendChild(el('div', 'result-error', message));
}

function renderChart(kind) {
  const out = $id('chart-output');
  currentChartKind = kind;
  $id('chart-panel').classList.remove('hidden');
  clearNode(out);
  clearNode($id('chart-legend'));
  if (!lastQueryResult || !lastQueryResult.rows ||
      !lastQueryResult.rows.length) {
    showChartMessage(tr('chart.empty', '当前查询结果为空，无法绘图'));
    return;
  }
  const cols = lastQueryResult.columns || [];
  const rows = lastQueryResult.rows || [];
  let valueIdx = -1;
  for (let i = 0; i < cols.length; i += 1) {
    if (rows.some(r => typeof r[i] === 'number' && isFinite(r[i]))) {
      valueIdx = i;
      break;
    }
  }
  if (valueIdx < 0) {
    showChartMessage(tr('chart.no_numeric',
      '当前结果没有数值列，无法绘图（柱状/折线需要数值列）'));
    return;
  }
  const data = rows.slice(0, 30).map((r, idx) => ({
    label: String(r[0] !== undefined && r[0] !== null ? r[0] : idx + 1),
    value: Number(r[valueIdx]) || 0,
  }));
  const legend = $id('chart-legend');
  const item = el('button', 'legend-item' + (chartSeriesVisible ? ' active' : ''),
    cols[valueIdx]);
  item.setAttribute('type', 'button');
  item.onclick = () => {
    chartSeriesVisible = !chartSeriesVisible;
    renderChart(currentChartKind);
  };
  legend.appendChild(item);
  if (!chartSeriesVisible) {
    showChartMessage(tr('chart.legend_hidden',
      '图例已隐藏该数据系列（点击图例恢复）'));
    const restored = $id('chart-legend');
    if (restored && !restored.firstChild) restored.appendChild(item);
    return;
  }
  const width = 660, height = 300, padL = 50, padR = 20, padT = 24, padB = 48;
  const maxV = Math.max(...data.map(d => d.value), 0);
  const minV = Math.min(...data.map(d => d.value), 0);
  const span = (maxV - minV) || 1;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(height));
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', cols[valueIdx] + ' ' + kind + ' chart');
  const yFor = v => padT + plotH - ((v - minV) / span) * plotH;
  const xFor = i => padL + (data.length <= 1 ? plotW / 2 :
    (i / (data.length - 1)) * plotW);
  const axis = document.createElementNS(NS, 'line');
  axis.setAttribute('x1', padL); axis.setAttribute('y1', padT + plotH);
  axis.setAttribute('x2', padL + plotW); axis.setAttribute('y2', padT + plotH);
  axis.setAttribute('stroke', '#888'); axis.setAttribute('stroke-width', '1');
  svg.appendChild(axis);
  const yLabel = document.createElementNS(NS, 'text');
  yLabel.setAttribute('x', 8); yLabel.setAttribute('y', padT + 8);
  yLabel.setAttribute('fill', '#9cdcfe'); yLabel.setAttribute('font-size', '11');
  yLabel.textContent = cols[valueIdx] + ' (max ' + maxV + ')';
  svg.appendChild(yLabel);
  if (kind === 'line') {
    const points = data.map((d, i) => xFor(i) + ',' + yFor(d.value)).join(' ');
    const line = document.createElementNS(NS, 'polyline');
    line.setAttribute('points', points);
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', '#4daafc');
    line.setAttribute('stroke-width', '2.5');
    svg.appendChild(line);
    data.forEach((d, i) => {
      const dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', xFor(i)); dot.setAttribute('cy', yFor(d.value));
      dot.setAttribute('r', '3'); dot.setAttribute('fill', '#4daafc');
      dot.appendChild(chartTitle(d.label + ' · ' + cols[valueIdx] + ' = ' + d.value));
      svg.appendChild(dot);
    });
  } else {
    const barW = Math.max(4, plotW / data.length * 0.6);
    data.forEach((d, i) => {
      const rect = document.createElementNS(NS, 'rect');
      rect.setAttribute('x', String(xFor(i) - barW / 2));
      rect.setAttribute('y', String(Math.min(yFor(d.value), yFor(0))));
      rect.setAttribute('width', String(barW));
      rect.setAttribute('height', String(Math.abs(yFor(d.value) - yFor(0)) || 1));
      rect.setAttribute('fill', '#4daafc');
      rect.appendChild(chartTitle(d.label + ' · ' + cols[valueIdx] + ' = ' + d.value));
      svg.appendChild(rect);
    });
  }
  data.forEach((d, i) => {
    if (data.length > 12 && i % 2 !== 0 && i !== data.length - 1) return;
    const tx = document.createElementNS(NS, 'text');
    tx.setAttribute('x', String(xFor(i)));
    tx.setAttribute('y', String(padT + plotH + 18));
    tx.setAttribute('text-anchor', 'middle');
    tx.setAttribute('fill', '#b0b0b0');
    tx.setAttribute('font-size', '10');
    tx.textContent = d.label.slice(0, 12);
    svg.appendChild(tx);
  });
  out.appendChild(svg);
  out.appendChild(el('div', 'status', tr('chart.rendered', '图表已生成（本地 SVG）')));
  setStatus('exec-status', tr('chart.rendered', '图表已生成（本地 SVG）'), false);
}

async function loadER() {
  const out = $id('er-output');
  clearNode(out);
  setStatus('er-status', '加载中...', false);
  if (!currentDB) {
    setStatus('er-status', tr('er.no_conn', '请先选择数据库'), true);
    return;
  }
  try {
    const data = await api('/databases/' + encodeURIComponent(currentDB) +
      '/er' + connQuery(''));
    if (!data.nodes || !data.nodes.length) {
      setStatus('er-status', tr('er.empty', '当前数据库没有表'), true);
      return;
    }
    if (!window.ERInteraction) {
      setStatus('er-status', 'ER 交互模块未加载（er_interaction.js）', true);
      return;
    }
    erController = window.ERInteraction.render(out, data,
      {width: 980, height: Math.max(420, Math.ceil(data.nodes.length / 3) * 175 + 120)});
    let status = data.notice || 'ER 图已生成';
    if (data.nodes.length > 30) {
      status += `（${data.nodes.length} 张表：已启用简化渲染与大库提示）`;
    }
    setStatus('er-status', status, false);
  } catch (e) {
    setStatus('er-status', tr('er.failed', '加载 ER 图失败') + ': ' + e.message, true);
  }
}

async function loadPlugins() {
  const target = $id('plugins-table');
  clearNode(target);
  setStatus('plugins-status', '加载中...', false);
  try {
    const data = await api('/plugins');
    const plugins = data.plugins || [];
    if (!plugins.length) {
      setStatus('plugins-status',
        data.notice || tr('plugins.empty', '插件目录为空：未发现已加载插件'), false);
      return;
    }
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    ['插件名', '版本', '状态', '错误'].forEach(h => hr.appendChild(el('th', '', h)));
    thead.appendChild(hr); table.appendChild(thead);
    const tbody = document.createElement('tbody');
    plugins.forEach(p => {
      const row = document.createElement('tr');
      row.appendChild(el('td', '', p.name || p.key || ''));
      row.appendChild(el('td', '', p.version || ''));
      row.appendChild(el('td', 'level-' + (p.status === 'failed' ? 'error' : 'info'),
        p.status || ''));
      row.appendChild(el('td', '', p.error || ''));
      tbody.appendChild(row);
    });
    table.appendChild(tbody); target.appendChild(table);
    setStatus('plugins-status',
      data.warnings && data.warnings.length ? data.warnings.join('; ') : '已加载',
      false);
  } catch (e) {
    setStatus('plugins-status', tr('plugins.failed', '加载插件失败') + ': ' + e.message, true);
  }
}

async function exportStreaming() {
  if (!currentTable || !currentDB) {
    setStatus('exec-status', '请先加载表数据', true);
    return;
  }
  const btn = $id('export-stream-btn');
  if (!setBusy(btn, true, tr('stream.processing', '流式导出中...'))) return;
  try {
    const res = await apiBlob('/databases/' + encodeURIComponent(currentDB) +
      '/tables/' + encodeURIComponent(currentTable) + '/export/stream' +
      connQuery(''));
    downloadBlob(res.blob, res.filename || (currentTable + '.csv'));
    setStatus('exec-status', tr('stream.done', '流式 CSV 导出完成'), false);
  } catch (e) {
    setStatus('exec-status', '流式导出失败: ' + e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

function stopImportPolling() {
  if (importPollTimer) {
    clearInterval(importPollTimer);
    importPollTimer = null;
  }
}

async function pollImportJob(jobId) {
  try {
    const data = await api('/import/jobs/' + encodeURIComponent(jobId));
    setStatus('import-progress',
      `导入进度 ${data.progress}%（${data.processed}/${data.total}，` +
      `失败 ${data.failed}）`, data.status === 'failed');
    if (['completed', 'completed_with_errors', 'cancelled', 'failed']
        .includes(data.status)) {
      stopImportPolling();
      activeImportJob = '';
      $id('cancel-import-btn').classList.add('hidden');
      setStatus('import-progress',
        `导入结束：${data.status}，成功 ${data.inserted}，失败 ${data.failed}`,
        data.status !== 'completed');
      if (currentTable) await loadTableData(currentTable, currentDB, 0);
    }
  } catch (e) {
    stopImportPolling();
    setStatus('import-progress', '导入任务查询失败: ' + e.message, true);
  }
}

async function cancelImport() {
  if (!activeImportJob) return;
  try {
    const data = await api('/import/jobs/' + encodeURIComponent(activeImportJob) +
      '/cancel', {method: 'POST'});
    setStatus('import-progress', data.ok ? '取消请求已发送' : data.error, !data.ok);
  } catch (e) {
    setStatus('import-progress', '取消失败: ' + e.message, true);
  }
}


window.addEventListener('DOMContentLoaded', async () => {
  $id('login-btn').onclick = doLogin;
  $id('login-pass').onkeydown = (e) => {
    if (e.key === 'Enter') doLogin();
  };
  $id('logout-btn').onclick = logout;
  $id('conn-select').onchange = onConnChange;
  $id('run-btn').onclick = runQuery;
  $id('format-btn').onclick = formatSQL;
  $id('clear-btn').onclick = () => {
    $id('sql-editor').value = '';
    clearNode($id('result-table'));
    $id('result-info').textContent = '就绪';
    setStatus('exec-status', '', false);
  };
  $id('refresh-obj-btn').onclick = loadConnections;
  $id('data-btn').onclick = () => {
    if (!currentTable) {
      setStatus('exec-status', '请先在对象树选择表', true);
      return;
    }
    loadTableData(currentTable, currentDB, 0);
  };
  $id('design-btn').onclick = openDesigner;
  $id('explain-btn').onclick = explainCurrent;
  $id('export-query-btn').onclick = exportCurrentQuery;
  $id('import-btn').onclick = triggerImport;
  $id('import-file-input').onchange = onImportFile;
  $id('backup-btn').onclick = backupCurrent;
  $id('sync-btn').onclick = compareSchema;
  $id('data-prev-btn').onclick = () =>
    loadTableData(currentTable, currentDB, Math.max(0, dataOffset - 100));
  $id('data-next-btn').onclick = () =>
    loadTableData(currentTable, currentDB, dataOffset + 100);
  $id('sort-column-select').onchange = onSortColumnChange;
  $id('export-table-btn').onclick = exportCurrentTable;
  $id('data-close-btn').onclick = () => {
    $id('table-controls').classList.add('hidden');
    clearNode($id('result-table'));
    $id('result-info').textContent = '就绪';
  };
  $id('design-preview-btn').onclick = () => submitDesign(true);
  $id('design-save-btn').onclick = () => submitDesign(false);
  $id('design-close-btn').onclick = () => {
    $id('design-panel').classList.add('hidden');
    setStatus('design-status', '', false);
  };

  $id('tool-history-btn').onclick = () => switchTool('history');
  $id('tool-reports-btn').onclick = () => switchTool('reports');
  $id('tool-quality-btn').onclick = () => switchTool('quality');
  $id('tool-er-btn').onclick = () => switchTool('er');
  $id('tool-plugins-btn').onclick = () => switchTool('plugins');
  $id('er-refresh-btn').onclick = loadER;
  $id('er-fit-btn').onclick = () => { if (erController) erController.fit(); };
  $id('er-reset-btn').onclick = () => { if (erController) erController.reset(); };
  $id('er-zoom-in-btn').onclick = () => { if (erController) erController.zoomIn(); };
  $id('er-zoom-out-btn').onclick = () => { if (erController) erController.zoomOut(); };
  $id('plugins-refresh-btn').onclick = loadPlugins;
  $id('chart-bar-btn').onclick = () => renderChart('bar');
  $id('chart-line-btn').onclick = () => renderChart('line');
  $id('chart-switch-btn').onclick = switchChartKind;
  $id('chart-export-svg').onclick = exportChartSVG;
  $id('chart-export-png').onclick = exportChartPNG;
  $id('chart-close-btn').onclick = () => {
    $id('chart-panel').classList.add('hidden');
  };
  $id('export-stream-btn').onclick = exportStreaming;
  $id('cancel-import-btn').onclick = cancelImport;
  $id('lang-select').onchange = e => window.setLang(e.target.value);
  if (typeof window.applyI18n === 'function') window.applyI18n();
  $id('report-refresh-btn').onclick = loadReportTemplates;
  $id('report-save-btn').onclick = saveReportTemplate;
  $id('report-preview-btn').onclick = () => {
    const name = selectedReport || $id('report-name-input').value.trim();
    if (!name) { setStatus('report-status', '请先选择或保存模板', true); return; }
    previewReport(name, $id('report-preview-btn'));
  };
  ['html', 'csv', 'xlsx'].forEach(fmt => {
    $id('report-export-' + fmt).onclick = () => {
      const name = selectedReport || $id('report-name-input').value.trim();
      if (!name) { setStatus('report-status', '请先选择或保存模板', true); return; }
      exportReport(name, fmt, $id('report-export-' + fmt));
    };
  });
  $id('quality-check-btn').onclick = runQualityCheck;

  $id('sql-editor').onkeydown = (e) => {
    if (e.key === 'Enter' && e.ctrlKey) {
      e.preventDefault();
      runQuery();
    }
  };

  // 检查已有 token
  if (token) {
    try {
      const user = await api('/me');
      showMain(user);
    } catch (e) {
      logout();
    }
  }
});
