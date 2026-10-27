# -*- coding: utf-8 -*-
"""task-66 P3：ER 交互 / 图表交互 / Web i18n 全量审计（QJSEngine 纯函数）。"""
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / 'web' / 'static'
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_APP = None


def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    _APP = app
    return _APP


def js_engine():
    qapp()
    from PySide6.QtQml import QJSEngine
    eng = QJSEngine()
    return eng


def eval_js(eng, source, name=''):
    res = eng.evaluate(source, name)
    if res.isError():
        raise AssertionError(f'JS error {name}: {res.toString()}')
    return res


def test_i18n_dictionary_keys_complete():
    eng = js_engine()
    eval_js(eng, (STATIC / 'i18n.js').read_text(encoding='utf-8'), 'i18n.js')
    raw = eval_js(eng, 'JSON.stringify(LDB_I18N)').toString()
    data = json.loads(raw)
    zh, en = data['zh'], data['en']
    assert zh and en
    assert set(zh) == set(en), ('zh/en key 不一致', set(zh) ^ set(en))
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    js = (STATIC / 'app.js').read_text(encoding='utf-8')
    html_keys = set(re.findall(r'data-i18n="([^"]+)"', html))
    js_keys = set(re.findall(r"tr\('([^']+)'", js))
    missing_zh = (html_keys | js_keys) - set(zh)
    assert not missing_zh, f'i18n.js 缺少 key: {sorted(missing_zh)}'
    # 未翻译项显式清单必须存在（不静默留中文）
    untranslated = json.loads(
        eval_js(eng, 'JSON.stringify(LDB_UNTRANSLATED)').toString())
    assert isinstance(untranslated, list) and untranslated
    docs = (ROOT / 'docs' / 'WEB_GUI_PARITY.md').read_text(encoding='utf-8')
    assert 'LDB_UNTRANSLATED' in docs or '未翻译' in docs


def test_i18n_language_switch_and_phrase_translation():
    eng = js_engine()
    eval_js(eng, (STATIC / 'i18n.js').read_text(encoding='utf-8'), 'i18n.js')
    assert eval_js(eng, "t('chart.bar')").toString() == '📊 柱状图'
    eval_js(eng, "setLang('en')")
    assert eval_js(eng, "t('chart.bar')").toString() == 'Bar chart'
    assert eval_js(eng, "t('er.tab')").toString() == 'ER Diagram'
    assert eval_js(eng, "translateText('登录')").toString() == 'Sign in'
    assert 'Sign' in eval_js(eng, "translateText('登录')").toString()
    eval_js(eng, "setLang('zh')")
    assert eval_js(eng, "t('chart.bar')").toString() == '📊 柱状图'
    # 中英字典 key 完整 + 关键旧文案短语可翻译
    for zh_phrase, en_phrase in [('用户名', 'Username'), ('密码', 'Password'),
                                 ('退出', 'Sign out'), ('查询', 'Query')]:
        eval_js(eng, "setLang('en')")
        assert eval_js(eng, 'translateText(%r)' % zh_phrase).toString() \
            == en_phrase
    eval_js(eng, "setLang('zh')")


def test_er_pure_transform_functions():
    eng = js_engine()
    eval_js(eng, (STATIC / 'er_interaction.js').read_text(encoding='utf-8'),
            'er_interaction.js')
    # 世界↔屏幕互逆
    assert eval_js(eng, 'JSON.stringify(ERInteraction.screenToWorld(100,50,{scale:2,tx:20,ty:10}))').toString() \
        == '{"x":40,"y":20}'
    assert eval_js(eng, 'JSON.stringify(ERInteraction.worldToScreen(40,20,{scale:2,tx:20,ty:10}))').toString() \
        == '{"x":100,"y":50}'
    # 以鼠标点为锚缩放：锚点世界坐标不变
    out = json.loads(eval_js(
        eng, 'JSON.stringify(ERInteraction.applyZoom({scale:1,tx:0,ty:0},2,100,80))').toString())
    assert out['scale'] == 2
    assert eval_js(eng, 'JSON.stringify(ERInteraction.screenToWorld(100,80,' +
                   json.dumps(out) + '))').toString() == '{"x":100,"y":80}'
    # 平移
    pan = json.loads(eval_js(eng,
        'JSON.stringify(ERInteraction.panBy({scale:1,tx:10,ty:20},5,-3))').toString())
    assert pan == {'scale': 1, 'tx': 15, 'ty': 17}
    # 适应/节点命中/拖拽/关联边
    nodes = [{'name': 'a', 'x': 0, 'y': 0, 'w': 100, 'h': 50},
             {'name': 'b', 'x': 200, 'y': 100, 'w': 100, 'h': 50}]
    fit = json.loads(eval_js(eng, 'JSON.stringify(ERInteraction.fitView(' +
                             json.dumps(nodes) + ',400,300,20))').toString())
    assert fit['scale'] > 0 and 'tx' in fit and 'ty' in fit
    assert eval_js(eng, 'ERInteraction.nodeAt(' + json.dumps(nodes) +
                   ',10,10).name').toString() == 'a'
    assert eval_js(eng, 'ERInteraction.nodeAt(' + json.dumps(nodes) +
                   ',500,500)').isNull()
    moved = json.loads(eval_js(eng, 'JSON.stringify(ERInteraction.dragNode(' +
        json.dumps({'x': 10, 'y': 20}) + ',5,-4))').toString())
    assert moved == {'x': 15, 'y': 16}
    edges = [{'from': 'a', 'to': 'b'}, {'from': 'b', 'to': 'c'}]
    assert len(json.loads(eval_js(eng, 'JSON.stringify(ERInteraction.edgesForNode(' +
        json.dumps(edges) + ',"a"))').toString())) == 1
    # 样式/交互实现存在
    src = (STATIC / 'er_interaction.js').read_text(encoding='utf-8')
    for token in ('mousedown', 'mousemove', 'wheel', 'applyZoom', 'panBy',
                  'fit', 'reset', 'classList.toggle(\'highlight\''):
        assert token in src, token


def test_chart_interactions_static_and_assets():
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    js = (STATIC / 'app.js').read_text(encoding='utf-8')
    for control in ('chart-legend', 'chart-switch-btn', 'chart-export-svg',
                    'chart-export-png', 'chart-bar-btn', 'chart-line-btn'):
        assert f'id="{control}"' in html, control
    for marker in ('function chartTitle', 'function exportChartSVG',
                   'function exportChartPNG', 'function switchChartKind',
                   'chartTitle(', 'legend-item', 'chart.legend_hidden'):
        assert marker in js, marker
    assert '<title' not in js or 'chartTitle' in js
    assert 'cdn.jsdelivr' not in js.lower()
    assert 'unpkg.com' not in js.lower()
    # 空/非数值显式
    assert 'chart.empty' in js and 'chart.no_numeric' in js
    assert 'createElementNS' in js


def test_chart_empty_non_numeric_messages_are_i18n_keys():
    eng = js_engine()
    eval_js(eng, (STATIC / 'i18n.js').read_text(encoding='utf-8'), 'i18n.js')
    for key in ('chart.empty', 'chart.no_numeric', 'chart.legend_hidden',
                'chart.export_svg', 'chart.export_png', 'er.interaction_hint'):
        assert eval_js(eng, f"t('{key}')").toString()
        eval_js(eng, "setLang('en')")
        assert eval_js(eng, f"t('{key}')").toString()
        eval_js(eng, "setLang('zh')")


def test_browser_e2e_explicit_skip():
    try:
        import playwright  # noqa: F401
    except Exception:
        pytest.skip('本机无 Playwright；交互纯函数用 QJSEngine 验证，'
                    'TestClient 覆盖 API；真浏览器 E2E 显式跳过')
    pytest.skip('Playwright 未配置浏览器/离线环境；真浏览器 E2E 显式跳过')
