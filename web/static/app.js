const API = '/api/v1';
let token = localStorage.getItem('lightdb_token') || '';
let currentConn = '';
let currentDB = '';

// ==================== 工具 ====================

async function api(path, options = {}) {
  const headers = options.headers || {};
  headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = 'Bearer ' + token;

  const resp = await fetch(API + path, {
    ...options,
    headers,
  });

  if (resp.status === 401) {
    logout();
    throw new Error('未登录');
  }
  return await resp.json();
}

function $id(id) {
  return document.getElementById(id);
}

// ==================== 登录 ====================

async function doLogin() {
  const user = $id('login-user').value.trim();
  const pass = $id('login-pass').value;

  try {
    const r = await fetch(API + '/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password: pass }),
    });
    const data = await r.json();
    if (!data.ok) {
      $id('login-error').textContent = data.detail || '登录失败';
      return;
    }
    token = data.token;
    localStorage.setItem('lightdb_token', token);
    showMain(data.user);
  } catch (e) {
    $id('login-error').textContent = '网络错误: ' + e.message;
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
  $id('user-label').textContent =
    `${user.username} (${user.role})`;
  loadConnections();
  loadHistory();
}

// ==================== 连接 ====================

async function loadConnections() {
  const data = await api('/connections');
  const sel = $id('conn-select');
  sel.innerHTML = '<option value="">选择连接...</option>';
  (data || []).forEach(c => {
    const opt = document.createElement('option');
    opt.value = c.name;
    opt.textContent = `${c.name} (${c.host}:${c.port})`;
    sel.appendChild(opt);
  });
}

async function onConnChange() {
  currentConn = $id('conn-select').value;
  if (!currentConn) {
    $id('object-tree').innerHTML = '';
    return;
  }
  await loadDatabases();
}

// ==================== 对象树 ====================

async function loadDatabases() {
  const tree = $id('object-tree');
  tree.innerHTML = '加载中...';

  const data = await api('/databases?connection=' + currentConn);
  tree.innerHTML = '';

  (data.databases || []).forEach(db => {
    const item = document.createElement('div');
    item.className = 'tree-item';
    item.innerHTML = `<span class="icon">🗄</span>${db}`;
    item.onclick = () => toggleDatabase(item, db);
    tree.appendChild(item);
  });
}

async function toggleDatabase(el, db) {
  // 折叠
  const next = el.nextElementSibling;
  if (next && next.classList.contains('tree-children')) {
    next.remove();
    return;
  }

  const children = document.createElement('div');
  children.className = 'tree-children';
  children.innerHTML = '加载中...';
  el.after(children);

  const data = await api(
    `/databases/${db}/tables?connection=${currentConn}`);
  children.innerHTML = '';

  (data.tables || []).forEach(t => {
    const item = document.createElement('div');
    item.className = 'tree-item';
    item.innerHTML =
      `<span class="icon">📋</span>${t.name} <span style="color:#666">(${t.rows})</span>`;
    item.onclick = (e) => {
      e.stopPropagation();
      selectTable(t.name, db);
    };
    children.appendChild(item);
  });
}

function selectTable(table, db) {
  currentDB = db;
  const sql = `SELECT * FROM \`${table}\` LIMIT 100;`;
  $id('sql-editor').value = sql;
  runQuery();
}

// ==================== 查询 ====================

async function runQuery() {
  const sql = $id('sql-editor').value.trim();
  if (!sql) return;

  $id('exec-status').textContent = '执行中...';
  $id('result-info').textContent = '执行中...';
  $id('result-table').innerHTML = '';

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
      $id('result-info').innerHTML =
        `<span style="color:#f48771">错误</span>`;
      $id('result-table').innerHTML =
        `<div class="result-error">${escapeHtml(data.error || '未知错误')}</div>`;
      $id('exec-status').textContent = '失败';
      return;
    }

    if (data.columns && data.columns.length) {
      renderTable(data.columns, data.rows);
      $id('result-info').textContent =
        `返回 ${data.row_count} 行 · ${data.elapsed_ms.toFixed(1)} ms`;
    } else {
      $id('result-info').textContent =
        `影响 ${data.affected} 行 · ${data.elapsed_ms.toFixed(1)} ms`;
      $id('result-table').innerHTML =
        `<div style="padding:20px;color:#89d185">✓ 执行成功，影响 ${data.affected} 行</div>`;
    }
    $id('exec-status').textContent = '完成';
    loadHistory();
  } catch (e) {
    $id('result-table').innerHTML =
      `<div class="result-error">${e.message}</div>`;
    $id('exec-status').textContent = '错误';
  }
}

function renderTable(cols, rows) {
  if (!rows || rows.length === 0) {
    $id('result-table').innerHTML =
      '<div style="padding:20px;color:#969696">空结果</div>';
    return;
  }

  let html = '<table><thead><tr>';
  cols.forEach(c => { html += `<th>${escapeHtml(c)}</th>`; });
  html += '</tr></thead><tbody>';

  rows.forEach(row => {
    html += '<tr>';
    row.forEach(v => {
      if (v === null || v === undefined) {
        html += '<td class="null-value">NULL</td>';
      } else if (typeof v === 'number') {
        html += `<td class="num-value">${v}</td>`;
      } else {
        html += `<td>${escapeHtml(String(v))}</td>`;
      }
    });
    html += '</tr>';
  });
  html += '</tbody></table>';
  $id('result-table').innerHTML = html;
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
}

// ==================== 格式化 ====================

function formatSQL() {
  const keywords = ['SELECT', 'FROM', 'WHERE', 'GROUP BY',
    'ORDER BY', 'HAVING', 'LIMIT', 'INSERT INTO', 'VALUES',
    'UPDATE', 'SET', 'DELETE FROM', 'INNER JOIN', 'LEFT JOIN',
    'RIGHT JOIN', 'CROSS JOIN', 'UNION', 'UNION ALL'];
  let sql = $id('sql-editor').value;
  keywords.forEach(kw => {
    sql = sql.replace(new RegExp(`\\b${kw}\\b`, 'gi'),
      '\n' + kw);
  });
  sql = sql.replace(/^\s+/, '');
  sql = sql.replace(/\n\s*\n/g, '\n');
  $id('sql-editor').value = sql;
}

// ==================== 历史 ====================

async function loadHistory() {
  try {
    const data = await api('/history?limit=30');
    const list = $id('history-list');
    list.innerHTML = '';
    (data.entries || []).forEach(e => {
      const item = document.createElement('div');
      item.className = 'history-item';
      const ts = new Date(e.ts * 1000);
      const timeStr = ts.toLocaleTimeString();
      item.innerHTML = `
        <div class="sql">${escapeHtml(e.sql.slice(0, 80))}</div>
        <div class="time">${timeStr}</div>
      `;
      item.onclick = () => {
        $id('sql-editor').value = e.sql;
      };
      list.appendChild(item);
    });
  } catch (e) {
    console.error(e);
  }
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
    $id('result-table').innerHTML = '';
    $id('result-info').textContent = '就绪';
  };
  $id('refresh-obj-btn').onclick = loadConnections;

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