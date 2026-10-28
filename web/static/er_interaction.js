/* task-66 ER 图交互（纯前端，无外部库/CDN）
 *
 * 纯函数：layoutNodes / screenToWorld / worldToScreen / applyZoom / panBy /
 * fitView / nodeAt / edgesForNode / dragNode。
 * 交互：节点拖拽、画布平移、滚轮缩放、适应/重置、点击节点高亮关联边。
 */
(function (root) {
  'use strict';

  function num(v, d) { return (typeof v === 'number' && isFinite(v)) ? v : d; }

  function layoutNodes(nodes, cols, nodeW, nodeH, gapX, gapY) {
    cols = num(cols, 3); nodeW = num(nodeW, 220); nodeH = num(nodeH, 140);
    gapX = num(gapX, 30); gapY = num(gapY, 35);
    return (nodes || []).map((n, i) => {
      const out = Object.assign({}, n);
      out.x = 20 + (i % cols) * (nodeW + gapX);
      out.y = 45 + Math.floor(i / cols) * (nodeH + gapY);
      out.w = nodeW;
      out.h = nodeH;
      return out;
    });
  }

  function clampScale(scale, min, max) {
    return Math.min(num(max, 3), Math.max(num(min, 0.25), num(scale, 1)));
  }

  function worldToScreen(x, y, view) {
    view = view || {scale: 1, tx: 0, ty: 0};
    return {x: x * view.scale + view.tx, y: y * view.scale + view.ty};
  }

  function screenToWorld(x, y, view) {
    view = view || {scale: 1, tx: 0, ty: 0};
    return {x: (x - view.tx) / view.scale, y: (y - view.ty) / view.scale};
  }

  function panBy(view, dx, dy) {
    view = view || {scale: 1, tx: 0, ty: 0};
    return {scale: view.scale, tx: view.tx + num(dx, 0),
            ty: view.ty + num(dy, 0)};
  }

  // 以屏幕点 (cx,cy) 为锚点缩放，保持锚点世界坐标不变
  function applyZoom(view, factor, cx, cy) {
    view = view || {scale: 1, tx: 0, ty: 0};
    factor = num(factor, 1);
    const before = screenToWorld(num(cx, 0), num(cy, 0), view);
    const scale = clampScale(view.scale * factor, 0.25, 3);
    return {
      scale: scale,
      tx: num(cx, 0) - before.x * scale,
      ty: num(cy, 0) - before.y * scale,
    };
  }

  function fitView(nodes, width, height, padding) {
    width = num(width, 800); height = num(height, 500);
    padding = num(padding, 40);
    if (!nodes || !nodes.length) {
      return {scale: 1, tx: 0, ty: 0};
    }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(n => {
      minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + (n.w || 220));
      maxY = Math.max(maxY, n.y + (n.h || 140));
    });
    const sw = Math.max(1, width - padding * 2);
    const sh = Math.max(1, height - padding * 2);
    const scale = clampScale(Math.min(sw / Math.max(1, maxX - minX),
                                     sh / Math.max(1, maxY - minY)), 0.25, 2);
    return {
      scale: scale,
      tx: padding - minX * scale,
      ty: padding - minY * scale,
    };
  }

  function nodeAt(nodes, x, y) {
    for (let i = (nodes || []).length - 1; i >= 0; i -= 1) {
      const n = nodes[i];
      if (x >= n.x && x <= n.x + (n.w || 220) &&
          y >= n.y && y <= n.y + (n.h || 140)) {
        return n;
      }
    }
    return null;
  }

  function edgesForNode(edges, name) {
    return (edges || []).filter(e => e.from === name || e.to === name);
  }

  function dragNode(node, dx, dy) {
    return {x: num(node.x, 0) + num(dx, 0),
            y: num(node.y, 0) + num(dy, 0)};
  }

  // >30 表按前缀分组（前缀在 "_" 前，否则取前 2 字符）
  function tablePrefix(name) {
    const s = String(name || '');
    if (s.indexOf('_') > 0) return s.split('_')[0];
    return s.slice(0, 2) || '?';
  }

  function groupByPrefix(nodes, threshold) {
    threshold = num(threshold, 30);
    const list = nodes || [];
    if (list.length <= threshold) {
      return [{key: 'all', count: list.length, nodes: list.slice()}];
    }
    const map = {}, order = [];
    list.forEach(n => {
      const key = tablePrefix(n.name);
      if (!map[key]) { map[key] = {key: key, count: 0, nodes: []}; order.push(key); }
      map[key].count += 1;
      map[key].nodes.push(n);
    });
    return order.map(k => map[k]);
  }

  function virtualGroupNodes(groups) {
    return (groups || []).map(g => ({
      name: g.key + ' (' + g.count + ')',
      prefix: g.key,
      columns: [{name: 'tables', type: String(g.count), key: ''}],
    }));
  }

  function layoutAll(nodes, cols) {
    return layoutNodes(nodes, cols || 3, 220, 140, 30, 35);
  }

  function render(container, data, options) {
    if (typeof document === 'undefined') return null;
    options = options || {};
    const NS = 'http://www.w3.org/2000/svg';
    const rawNodes = data.nodes || [];
    // 大库：先用前缀分组卡片渲染（只渲染组，不一次性渲染 100 张表）
    if (rawNodes.length > 30 && !options.forceSmall) {
      const groups = groupByPrefix(rawNodes, 30);
      const groupNodes = virtualGroupNodes(groups);
      const ctrl = render(container, {nodes: groupNodes, edges: []},
        Object.assign({}, options, {
          forceSmall: true,
          largeGroups: true,
          onNodeClick: name => {
            const prefix = String(name).replace(/\s*\(.*$/, '');
            const g = groups.find(x => x.key === prefix);
            if (!g) return;
            render(container, {
              nodes: g.nodes,
              edges: (data.edges || []).filter(e => {
                const names = new Set(g.nodes.map(n => n.name));
                return names.has(e.from) && names.has(e.to);
              }),
            }, Object.assign({}, options, {
              forceSmall: true,
              groupLabel: g.key,
              onNodeClick: null,
            }));
          },
        }));
      ctrl.groups = groups;
      ctrl.virtualized = true;
      return ctrl;
    }
    const nodes = layoutNodes(rawNodes, options.cols || 3, 220, 140, 30, 35);
    const edges = data.edges || [];
    const width = options.width || 980;
    const height = options.height || 620;
    container.textContent = '';
    const hint = document.createElement('div');
    hint.className = 'status';
    hint.textContent = nodes.length > 30
      ? ((root.t ? root.t('er.large_hint') : null) ||
         '表数量超过 30：已启用简化渲染，拖拽/缩放仍可用，建议先筛选表')
      : ((root.t ? root.t('er.interaction_hint') : null) ||
         '拖拽节点 · 空白处平移 · 滚轮缩放 · 点击节点高亮关联边');
    container.appendChild(hint);

    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('width', String(width));
    svg.setAttribute('height', String(height));
    svg.setAttribute('class', 'er-svg');
    const bg = document.createElementNS(NS, 'rect');
    bg.setAttribute('width', String(width));
    bg.setAttribute('height', String(height));
    bg.setAttribute('fill', 'transparent');
    bg.setAttribute('data-er-bg', '1');
    svg.appendChild(bg);
    const viewport = document.createElementNS(NS, 'g');
    viewport.setAttribute('class', 'er-viewport');
    svg.appendChild(viewport);
    const edgeLayer = document.createElementNS(NS, 'g');
    edgeLayer.setAttribute('class', 'er-edges');
    const nodeLayer = document.createElementNS(NS, 'g');
    nodeLayer.setAttribute('class', 'er-nodes');
    viewport.appendChild(edgeLayer);
    viewport.appendChild(nodeLayer);
    container.appendChild(svg);

    let view = fitView(nodes, width, height, 40);
    let selected = null;
    const nodeEls = {}, edgeEls = [];
    const applyTransform = () => {
      viewport.setAttribute(
        'transform', 'translate(' + view.tx + ',' + view.ty +
        ') scale(' + view.scale + ')');
    };
    const drawEdges = () => {
      edgeLayer.textContent = '';
      edgeEls.length = 0;
      edges.forEach(edge => {
        const a = nodes.find(n => n.name === edge.from);
        const b = nodes.find(n => n.name === edge.to);
        if (!a || !b) return;
        const line = document.createElementNS(NS, 'line');
        line.setAttribute('x1', String(a.x + (a.w || 220)));
        line.setAttribute('y1', String(a.y + (a.h || 140) / 2));
        line.setAttribute('x2', String(b.x));
        line.setAttribute('y2', String(b.y + (b.h || 140) / 2));
        line.setAttribute('class', 'er-edge');
        line.dataset.from = edge.from;
        line.dataset.to = edge.to;
        edgeLayer.appendChild(line);
        edgeEls.push(line);
      });
    };
    const drawNodes = () => {
      nodeLayer.textContent = '';
      nodes.forEach(n => {
        const g = document.createElementNS(NS, 'g');
        g.setAttribute('class', 'er-node');
        g.setAttribute('data-node', n.name);
        g.setAttribute('transform', 'translate(' + n.x + ',' + n.y + ')');
        const rect = document.createElementNS(NS, 'rect');
        rect.setAttribute('width', '220');
        rect.setAttribute('height', '140');
        rect.setAttribute('rx', '8');
        rect.setAttribute('class', 'er-node-box');
        g.appendChild(rect);
        const title = document.createElementNS(NS, 'text');
        title.setAttribute('x', '10'); title.setAttribute('y', '24');
        title.setAttribute('class', 'er-node-title');
        title.textContent = n.name;
        g.appendChild(title);
        (n.columns || []).slice(0, 4).forEach((c, j) => {
          const t = document.createElementNS(NS, 'text');
          t.setAttribute('x', '10');
          t.setAttribute('y', String(45 + j * 18));
          t.setAttribute('class', c.key === 'PRI' ? 'er-col-pk' : 'er-col');
          t.textContent = (c.key === 'PRI' ? 'PK ' : '') + c.name + ' : ' + c.type;
          g.appendChild(t);
        });
        nodeLayer.appendChild(g);
        nodeEls[n.name] = g;
      });
    };
    const refreshHighlight = () => {
      Object.keys(nodeEls).forEach(name =>
        nodeEls[name].classList.toggle('selected', name === selected));
      edgeEls.forEach(el => {
        const on = selected && (el.dataset.from === selected ||
                                el.dataset.to === selected);
        el.classList.toggle('highlight', !!on);
      });
    };
    const hitNode = (evt) => {
      let el = evt.target;
      while (el && el !== svg) {
        if (el.dataset && el.dataset.node) return el.dataset.node;
        el = el.parentNode;
      }
      return null;
    };
    let drag = null;
    svg.addEventListener('mousedown', evt => {
      const name = hitNode(evt);
      const p = screenToWorld(evt.offsetX, evt.offsetY, view);
      if (name) {
        const node = nodes.find(n => n.name === name);
        drag = {kind: 'node', name: name, start: p, node: node,
                moved: false};
      } else {
        drag = {kind: 'pan', start: {x: evt.clientX, y: evt.clientY},
                view: {scale: view.scale, tx: view.tx, ty: view.ty}};
      }
      evt.preventDefault();
    });
    svg.addEventListener('mousemove', evt => {
      if (!drag) return;
      if (drag.kind === 'node') {
        const p = screenToWorld(evt.offsetX, evt.offsetY, view);
        const dx = p.x - drag.start.x, dy = p.y - drag.start.y;
        const np = dragNode(drag.node, dx, dy);
        drag.node.x = np.x; drag.node.y = np.y;
        drag.start = p; drag.moved = true;
        drawEdges();
        nodeEls[drag.name].setAttribute(
          'transform', 'translate(' + drag.node.x + ',' + drag.node.y + ')');
      } else {
        view = panBy(drag.view, evt.clientX - drag.start.x,
                     evt.clientY - drag.start.y);
        applyTransform();
      }
    });
    svg.addEventListener('mouseup', () => { drag = null; });
    svg.addEventListener('mouseleave', () => { drag = null; });
    svg.addEventListener('wheel', evt => {
      evt.preventDefault();
      view = applyZoom(view, evt.deltaY < 0 ? 1.12 : 1 / 1.12,
                       evt.offsetX, evt.offsetY);
      applyTransform();
    }, {passive: false});
    svg.addEventListener('click', evt => {
      const name = hitNode(evt);
      if (!name) return;
      selected = (selected === name) ? null : name;
      refreshHighlight();
      if (typeof options.onNodeClick === 'function') options.onNodeClick(name);
    });

    const controller = {
      view: () => Object.assign({}, view),
      fit: () => {
        view = fitView(nodes, width, height, 40);
        applyTransform();
      },
      reset: () => {
        nodes.forEach((n, i) => {
          const fresh = layoutNodes(nodes, options.cols || 3, 220, 140, 30, 35)[i];
          n.x = fresh.x; n.y = fresh.y;
        });
        view = fitView(nodes, width, height, 40);
        drawNodes(); drawEdges(); applyTransform();
        selected = null; refreshHighlight();
      },
      zoomIn: () => { view = applyZoom(view, 1.2, width / 2, height / 2); applyTransform(); },
      zoomOut: () => { view = applyZoom(view, 1 / 1.2, width / 2, height / 2); applyTransform(); },
      select: name => { selected = name; refreshHighlight(); },
      selected: () => selected,
      nodes: () => nodes,
    };
    drawEdges();
    drawNodes();
    applyTransform();
    return controller;
  }

  const api = {layoutNodes, layoutAll, groupByPrefix, virtualGroupNodes,
               tablePrefix, clampScale, worldToScreen, screenToWorld, panBy,
               applyZoom, fitView, nodeAt, edgesForNode, dragNode, render};
  root.ERInteraction = api;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
})(typeof window !== 'undefined' ? window :
   (typeof globalThis !== 'undefined' ? globalThis : this));
