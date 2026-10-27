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
  const PHRASE_REVERSE = {};
  Object.keys(PHRASES).forEach(zh => { PHRASE_REVERSE[PHRASES[zh]] = zh; });

  // 显式未翻译清单：旧动态拼接文案（含变量/复数），待后续 key 化；
  // 不允许静默留中文——测试/ docs 会读取该列表。
  const UNTRANSLATED = [
    'app.js 动态状态消息（含 e.message/行数等插值，约 80 处）',
    'app.js 报表/质量/历史面板的动态详情文案（含模板名/计数插值）',
    'index.html 登录页安全说明长文案（代码/环境变量说明）',
    '后端错误 detail 透传文本（服务端产生，Web 不做翻译）',
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
  root.t = function (key, fallback) {
    const table = DICT[lang] || DICT.zh;
    return Object.prototype.hasOwnProperty.call(table, key)
      ? table[key] : (fallback !== undefined ? fallback : key);
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
