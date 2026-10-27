/* LightDB Studio Web 轻量 i18n（无框架/无 CDN）
 *
 * task-63 审计：原 Web UI 文案为硬编码中文，没有 i18n 框架；这里补一个
 * 最小原生字典 + data-i18n 扫描，覆盖本轮新增图表/ER/插件/流式导入导出文案。
 * 已有旧文案暂未全量迁移（记录在 docs/WEB_GUI_PARITY.md），不做假覆盖承诺。
 */
(function () {
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
      'er.tab': 'ER 图',
      'er.title': 'ER 图（information_schema）',
      'er.no_conn': '请先选择数据库',
      'er.empty': '当前数据库没有表，无法生成 ER 图',
      'er.no_fk': '无外键关系（当前库没有可展示的 FOREIGN KEY 约束）',
      'er.failed': '加载 ER 图失败',
      'plugins.tab': '插件',
      'plugins.title': '已加载插件',
      'plugins.empty': '插件目录为空：未发现已加载插件',
      'plugins.failed': '加载插件失败',
      'stream.export': '流式导出',
      'stream.processing': '流式导出中...',
      'stream.done': '流式 CSV 导出完成',
      'stream.cancelled': '导入任务已取消',
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
      'er.tab': 'ER Diagram',
      'er.title': 'ER diagram (information_schema)',
      'er.no_conn': 'Select a database first',
      'er.empty': 'The current database has no tables; cannot build an ER diagram',
      'er.no_fk': 'No foreign-key relationships found in this database',
      'er.failed': 'Failed to load ER diagram',
      'plugins.tab': 'Plugins',
      'plugins.title': 'Loaded plugins',
      'plugins.empty': 'Plugin directory is empty: no loaded plugins',
      'plugins.failed': 'Failed to load plugins',
      'stream.export': 'Stream export',
      'stream.processing': 'Streaming export...',
      'stream.done': 'Streaming CSV export finished',
      'stream.cancelled': 'Import job cancelled',
    },
  };
  let lang = 'zh';
  try { lang = localStorage.getItem('lightdb_lang') || 'zh'; } catch (e) { /* ignore */ }
  if (!DICT[lang]) lang = 'zh';
  window.LDB_I18N = DICT;
  window.currentLang = () => lang;
  window.t = function (key, fallback) {
    const table = DICT[lang] || DICT.zh;
    return Object.prototype.hasOwnProperty.call(table, key)
      ? table[key] : (fallback !== undefined ? fallback : key);
  };
  window.setLang = function (next) {
    lang = DICT[next] ? next : 'zh';
    try { localStorage.setItem('lightdb_lang', lang); } catch (e) { /* ignore */ }
    document.documentElement.lang = (lang === 'zh' ? 'zh-CN' : 'en');
    window.applyI18n();
    return lang;
  };
  window.applyI18n = function () {
    document.querySelectorAll('[data-i18n]').forEach(node => {
      const key = node.getAttribute('data-i18n');
      node.textContent = window.t(key, node.textContent);
    });
    const sel = document.getElementById('lang-select');
    if (sel) sel.value = lang;
  };
})();
