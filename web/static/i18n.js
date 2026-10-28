/* LightDB Studio Web 轻量 i18n（无框架/无 CDN）
 *
 * task-66 审计：index.html/app.js 旧文案硬编码中文。这里提供：
 * 1) key 字典（data-i18n / t()）覆盖新交互文案（图表/ER/插件/流式）；
 * 2) 常见旧页面短语中英映射（运行时翻译静态文本/placeholder/title）；
 * 3) LDB_UNTRANSLATED 显式未翻译清单（不静默留中文；测试/文档同步）。
 * 无外部框架/CDN；QJSEngine 可加载纯逻辑部分（document 缺失时安全降级）。
 */
(function (root) {
  const DICT = {
    zh: {
      'common.refresh': '刷新',
      'common.close': '关闭',
      'chart.title': '查询结果图表（本地 SVG，无 CDN）',
      'chart.bar': '📊 柱状图',
      'chart.line': '📈 折线图',
      'chart.empty': '当前查询结果为空，无法绘图',
      'chart.no_numeric': '当前结果没有数值列，无法绘图（柱状/折线需要数值列）',
      'chart.rendered': '图表已生成（本地 SVG）',
      'chart.switch': '切换柱/线',
      'chart.export_svg': '导出 SVG',
      'chart.export_png': '导出 PNG',
      'chart.legend_hidden': '图例已隐藏该数据系列（点击图例恢复）',
      'er.tab': 'ER 图',
      'er.title': 'ER 图（information_schema）',
      'er.no_conn': '请先选择数据库',
      'er.empty': '当前数据库没有表，无法生成 ER 图',
      'er.no_fk': '无外键关系（当前库没有可展示的 FOREIGN KEY 约束）',
      'er.failed': '加载 ER 图失败',
      'er.fit': '适应窗口',
      'er.reset': '重置',
      'er.interaction_hint': '拖拽节点 · 空白处平移 · 滚轮缩放 · 点击节点高亮关联边',
      'er.large_hint': '表数量超过 30：已启用简化渲染，拖拽/缩放仍可用，建议先筛选表',
      'er.generated': 'ER 图已生成',
      'plugins.tab': '插件',
      'plugins.title': '已加载插件',
      'plugins.empty': '插件目录为空：未发现已加载插件',
      'plugins.failed': '加载插件失败',
      'stream.export': '流式导出',
      'stream.processing': '流式导出中...',
      'stream.done': '流式 CSV 导出完成',
      'stream.cancelled': '导入任务已取消',
      'status.operation_failed': '操作失败: {message}',
      'status.loading': '加载中...',
      'status.rows': '返回 {count} 行 · {elapsed} ms',
      'status.affected': '影响 {count} 行 · {elapsed} ms',
      'status.exec_ok': '✓ 执行成功，影响 {count} 行',
      'status.import_progress': '导入进度 {progress}%（{processed}/{total}，失败 {failed}）',
      'status.import_done': '导入结束：{status}，成功 {inserted}，失败 {failed}',
      'status.import_started': '导入任务已启动：0/{total}',
      'status.schema_compare': '结构对比：缺少表 {missing}，新增表 {extra}，变化表 {changed}',
      'status.explain_rows': 'EXPLAIN 返回 {count} 行',
      'status.format_done': '格式化完成（{before} → {after} 字符）',
      'status.templates_loaded': '已加载 {count} 个模板',
      'status.template_saved': '已保存模板: {name}',
      'status.template_preview': '模板「{name}」共 {total} 行，预览 {returned} 行',
      'status.template_deleted': '已删除模板: {name}',
      'status.template_exported': '已导出「{name}」为 {format}（{filename}）',
      'status.quality_summary': '表 {table}: 共 {total} 项（error {error} / warning {warning} / info {info}）',
      'status.alter_preview': '预览 {count} 条 ALTER（未执行）',
      'status.alter_executed': '已执行 {count} 条 ALTER',
      'status.design_read': '已读取列/索引；修改 JSON 后预览或保存',
      'status.er_generated': 'ER 图已生成',
      'status.er_large': '{count} 张表：已启用前缀分组与按需渲染（大库提示）',
      'status.login_security': '账号来源：服务端 LIGHTDB_WEB_USERS 配置；未配置时使用启动时生成的随机 admin 口令（~/.lightdb_studio/web_admin.txt 或容器启动日志）。出于安全考虑，本页面不再预填任何默认口令。',
      'login.account_source': '账号来源：',
      'error.login_expired': '登录已过期，请重新登录',
      'error.backend_detail': '后端错误: {message}',
      'error.network': '网络错误: {message}',
      'error.csv_header': 'CSV 表头列不存在: {name}',
    },
    en: {
      'common.refresh': 'Refresh',
      'common.close': 'Close',
      'chart.title': 'Query result chart (local SVG, no CDN)',
      'chart.bar': 'Bar chart',
      'chart.line': 'Line chart',
      'chart.empty': 'The current query result is empty; nothing to chart',
      'chart.no_numeric': 'No numeric column found; bar/line charts require numbers',
      'chart.rendered': 'Chart rendered locally (SVG)',
      'chart.switch': 'Switch bar/line',
      'chart.export_svg': 'Export SVG',
      'chart.export_png': 'Export PNG',
      'chart.legend_hidden': 'Legend hidden this data series (click legend to restore)',
      'er.tab': 'ER Diagram',
      'er.title': 'ER diagram (information_schema)',
      'er.no_conn': 'Select a database first',
      'er.empty': 'The current database has no tables; cannot build an ER diagram',
      'er.no_fk': 'No foreign-key relationships found in this database',
      'er.failed': 'Failed to load ER diagram',
      'er.fit': 'Fit',
      'er.reset': 'Reset',
      'er.interaction_hint': 'Drag nodes · drag background to pan · wheel to zoom · click node to highlight related edges',
      'er.large_hint': 'More than 30 tables: simplified rendering enabled; drag/zoom still work, filter tables first',
      'er.generated': 'ER diagram generated',
      'plugins.tab': 'Plugins',
      'plugins.title': 'Loaded plugins',
      'plugins.empty': 'Plugin directory is empty: no loaded plugins',
      'plugins.failed': 'Failed to load plugins',
      'stream.export': 'Stream export',
      'stream.processing': 'Streaming export...',
      'stream.done': 'Streaming CSV export finished',
      'stream.cancelled': 'Import job cancelled',
      'status.operation_failed': 'Operation failed: {message}',
      'status.loading': 'Loading...',
      'status.rows': 'Returned {count} rows · {elapsed} ms',
      'status.affected': 'Affected {count} rows · {elapsed} ms',
      'status.exec_ok': '✓ Executed successfully; {count} rows affected',
      'status.import_progress': 'Import progress {progress}% ({processed}/{total}, failed {failed})',
      'status.import_done': 'Import finished: {status}, inserted {inserted}, failed {failed}',
      'status.import_started': 'Import job started: 0/{total}',
      'status.schema_compare': 'Schema compare: missing {missing}, extra {extra}, changed {changed}',
      'status.explain_rows': 'EXPLAIN returned {count} rows',
      'status.format_done': 'Format complete ({before} → {after} chars)',
      'status.templates_loaded': 'Loaded {count} templates',
      'status.template_saved': 'Template saved: {name}',
      'status.template_preview': 'Template "{name}": {total} rows, preview {returned}',
      'status.template_deleted': 'Template deleted: {name}',
      'status.template_exported': 'Exported "{name}" as {format} ({filename})',
      'status.quality_summary': 'Table {table}: {total} issues (error {error} / warning {warning} / info {info})',
      'status.alter_preview': 'Preview {count} ALTER statements (not executed)',
      'status.alter_executed': 'Executed {count} ALTER statements',
      'status.design_read': 'Columns/indexes loaded; edit JSON then preview or save',
      'status.er_generated': 'ER diagram generated',
      'status.er_large': '{count} tables: prefix grouping and on-demand rendering enabled',
      'status.login_security': 'Account source: server LIGHTDB_WEB_USERS configuration; if unset, a random admin password is generated at startup (~/.lightdb_studio/web_admin.txt or container startup log). For security, this page never pre-fills default passwords.',
      'login.account_source': 'Account source:',
      'error.login_expired': 'Login expired; please sign in again',
      'error.backend_detail': 'Backend error: {message}',
      'error.network': 'Network error: {message}',
      'error.csv_header': 'CSV header column not found: {name}',
    },
  };

  // 旧页面常见短语（静态文本/placeholder/title 的运行时翻译；非 key 字典）
  const PHRASES = {
    '登录': 'Sign in', '用户名': 'Username', '密码': 'Password',
    '退出': 'Sign out', '查询': 'Query', '历史': 'History',
    '报表模板': 'Report templates', '数据质量': 'Data quality',
    '数据质量检查': 'Data quality check', '插件': 'Plugins',
    '已加载插件': 'Loaded plugins', '查询历史': 'Query history',
    '选择连接...': 'Select connection...', '加载中...': 'Loading...',
    '就绪': 'Ready', '执行': 'Execute', '清空': 'Clear',
    '格式化': 'Format', '表数据': 'Table data', '设计表': 'Design table',
    '导出结果': 'Export result', '导入 CSV': 'Import CSV', '备份': 'Backup',
    '结构对比': 'Schema compare', '上一页': 'Previous', '下一页': 'Next',
    '关闭数据页': 'Close data page', '导出 CSV': 'Export CSV',
    '流式导出': 'Stream export', '取消导入': 'Cancel import',
    '表设计器（列 / AUTO_INCREMENT / 索引）': 'Table designer (columns / AUTO_INCREMENT / indexes)',
    '预览 ALTER': 'Preview ALTER', '保存 ALTER': 'Save ALTER',
    '关闭': 'Close', '刷新': 'Refresh', '预览': 'Preview', '删除': 'Delete',
    '导出 HTML': 'Export HTML', '导出 XLSX': 'Export XLSX',
    '检查': 'Check', '适应窗口': 'Fit', '重置': 'Reset',
    'ER 图': 'ER diagram', '插件名': 'Plugin', '版本': 'Version',
    '状态': 'Status', '错误': 'Error', '表': 'Table', '列': 'Column',
    '消息': 'Message', '级别': 'Level', '分类': 'Category',
    '默认排序': 'Default sort', '空结果': 'Empty result',
    '完成': 'Done', '失败': 'Failed', '未知错误': 'Unknown error',
    '保存当前 SQL': 'Save current SQL', '刷新列表': 'Refresh list',
    '模板名称': 'Template name', '无描述': 'No description', '无': 'None',
    '已配置': 'Configured', '未配置': 'Not configured',
  };
  // task-69：动态状态前缀（保留插值部分，运行时翻译前缀）
  Object.assign(PHRASES, {
    '加载失败: ': 'Load failed: ', '加载连接失败: ': 'Load connections failed: ',
    '加载表数据...': 'Loading table data...', '表数据已加载': 'Table data loaded',
    '操作失败: ': 'Operation failed: ', '执行失败: ': 'Execution failed: ',
    '查询失败: ': 'Query failed: ', '导出失败: ': 'Export failed: ',
    '导入启动失败: ': 'Import start failed: ', '导入失败: ': 'Import failed: ',
    '保存失败: ': 'Save failed: ', '读取设计器失败: ': 'Load designer failed: ',
    'JSON 解析失败: ': 'JSON parse failed: ', 'JSON 必须是列数组': 'JSON must be an array of columns',
    '备份失败: ': 'Backup failed: ', '结构对比失败: ': 'Schema compare failed: ',
    'EXPLAIN 完成': 'EXPLAIN done', 'EXPLAIN 失败: ': 'EXPLAIN failed: ',
    '查询结果已导出 CSV': 'Query result exported as CSV',
    '表 CSV 已导出': 'Table CSV exported',
    '备份中...': 'Backing up...', '对比中...': 'Comparing...',
    '读取结构...': 'Reading structure...', '导入任务已启动：0/': 'Import job started: 0/',
    '导入任务查询失败: ': 'Import job query failed: ',
    '取消失败: ': 'Cancel failed: ', '取消请求已发送': 'Cancel requested',
    '流式导出失败: ': 'Stream export failed: ', '加载 ER 图失败: ': 'Load ER diagram failed: ',
    '加载插件失败: ': 'Load plugins failed: ', '加载模板失败: ': 'Load templates failed: ',
    '预览失败: ': 'Preview failed: ', '删除失败: ': 'Delete failed: ',
    '检查失败: ': 'Check failed: ', '格式化失败: ': 'Format failed: ',
    '检查完成': 'Check complete', '预览中...': 'Previewing...',
    '保存中...': 'Saving...', '删除中...': 'Deleting...',
    '刷新中...': 'Refreshing...', '备份中... ': 'Backing up... ',
  });

  const PHRASE_REVERSE = {};
  Object.keys(PHRASES).forEach(zh => { PHRASE_REVERSE[PHRASES[zh]] = zh; });

  // 显式未翻译清单：确实不可译/服务端自由文本；旧页面动态前缀已
  // 通过 PHRASES 运行时翻译，登录页安全说明已 data-i18n key 化。
  // 不允许静默留中文——测试/ docs 会读取该列表。
  const UNTRANSLATED = [
    '后端错误 detail 原文（服务端生成的 SQL/引擎文本，作为 {message} 参数插入，本身不翻译）',
    '插件 description/author 字段（插件作者提供的自由文本）',
  ];

  let lang = 'zh';
  try {
    if (typeof localStorage !== 'undefined') {
      lang = localStorage.getItem('lightdb_lang') || 'zh';
    }
  } catch (e) { /* ignore */ }
  if (!DICT[lang]) lang = 'zh';

  function translatePhrase(text) {
    if (!text) return text;
    const trimmed = String(text).trim();
    if (lang === 'en') {
      if (Object.prototype.hasOwnProperty.call(PHRASES, trimmed)) {
        return PHRASES[trimmed];
      }
      let out = String(text);
      Object.keys(PHRASES).sort((a, b) => b.length - a.length)
        .forEach(zh => { out = out.split(zh).join(PHRASES[zh]); });
      return out;
    }
    let out = String(text);
    Object.keys(PHRASE_REVERSE).sort((a, b) => b.length - a.length)
      .forEach(en => { out = out.split(en).join(PHRASE_REVERSE[en]); });
    return out;
  }

  function translateElement(el) {
    if (!el || el.nodeType !== 1) return;
    ['placeholder', 'title', 'aria-label'].forEach(attr => {
      const value = el.getAttribute && el.getAttribute(attr);
      if (value) {
        const next = translatePhrase(value);
        if (next !== value) el.setAttribute(attr, next);
      }
    });
    Array.prototype.forEach.call(el.childNodes || [], node => {
      if (node.nodeType === 3) {
        const next = translatePhrase(node.nodeValue);
        if (next !== node.nodeValue) node.nodeValue = next;
      } else if (node.nodeType === 1) {
        translateElement(node);
      }
    });
  }

  root.LDB_I18N = DICT;
  root.LDB_PHRASES = PHRASES;
  root.LDB_UNTRANSLATED = UNTRANSLATED;
  root.currentLang = () => lang;
  root.t = function (key, fallback, params) {
    const table = DICT[lang] || DICT.zh;
    const template = Object.prototype.hasOwnProperty.call(table, key)
      ? table[key] : (fallback !== undefined ? fallback : key);
    if (params && typeof params === 'object') {
      return String(template).replace(/\{([A-Za-z0-9_]+)\}/g, (m, name) =>
        Object.prototype.hasOwnProperty.call(params, name)
          ? String(params[name]) : m);
    }
    return template;
  };
  root.translateText = translatePhrase;
  root.setLang = function (next) {
    lang = DICT[next] ? next : 'zh';
    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem('lightdb_lang', lang);
      }
    } catch (e) { /* ignore */ }
    if (typeof document !== 'undefined') {
      document.documentElement.lang = (lang === 'zh' ? 'zh-CN' : 'en');
      root.applyI18n();
    }
    return lang;
  };
  root.applyI18n = function () {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('[data-i18n]').forEach(node => {
      const key = node.getAttribute('data-i18n');
      node.textContent = root.t(key, node.textContent);
    });
    translateElement(document.body);
    const sel = document.getElementById('lang-select');
    if (sel) sel.value = lang;
  };
  // 浏览器兼容入口（QJSEngine 下无 window，使用 root）
  if (typeof window !== 'undefined') {
    window.t = root.t;
    window.applyI18n = root.applyI18n;
    window.setLang = root.setLang;
    window.translateText = root.translateText;
    window.LDB_UNTRANSLATED = UNTRANSLATED;
  }
  // 新渲染的动态文本自动翻译（浏览器环境）
  if (typeof document !== 'undefined' && typeof MutationObserver !== 'undefined') {
    try {
      const observer = new MutationObserver(() => translateElement(document.body));
      observer.observe(document.body, {childList: true, subtree: true,
                                       characterData: true});
    } catch (e) { /* ignore */ }
  }
})(typeof window !== 'undefined' ? window :
   (typeof globalThis !== 'undefined' ? globalThis : this));
