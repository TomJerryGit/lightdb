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
      '/tables/' + encodeURIComponent(currentTable) + '/import' +
      connQuery(''), {
      method: 'POST',
      body: JSON.stringify({csv: text, has_header: true}),
    });
    setStatus('exec-status',
      `导入完成：成功 ${data.inserted} 行，失败 ${data.failed} 行`, !data.ok);
    if (data.errors && data.errors.length) {
      $id('result-table').appendChild(
        el('div', 'result-error', JSON.stringify(data.errors.slice(0, 5))));
    }
    if (currentTable) await loadTableData(currentTable, currentDB, 0);
  } catch (e) {
    setStatus('exec-status', '导入失败: ' + e.message, true);
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
  ['history', 'reports', 'quality'].forEach(name => {
    const btn = $id('tool-' + name + '-btn');
    if (btn) btn.classList.toggle('active', name === tool);
    const section = $id(name + '-section');
    if (section) section.classList.toggle('hidden', name !== tool);
  });
  if (tool === 'reports') loadReportTemplates();
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

// ==================== 初始化 ====================

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
