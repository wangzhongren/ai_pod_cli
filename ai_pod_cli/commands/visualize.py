"""Generate an offline, interactive graph of an AIPod project."""

import html
import json
import webbrowser
from pathlib import Path

from ai_pod_cli.project_model import (
    ProjectModelError,
    extract_pipeline_services as _extract_pipeline_services,
    load_project_graph as _load_project_graph,
)


DEFAULT_OUTPUT = "aipod-graph.html"


def _graph_html(beans: list[dict], routes: list[dict]) -> str:
    """Return a self-contained HTML document with a clickable SVG graph."""
    data = json.dumps({"beans": beans, "routes": routes}, ensure_ascii=False).replace("<", "\\u003c")
    title = html.escape("AIPod Project Graph")
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: Canvas; color: CanvasText; }}
    header {{ padding: 20px 24px 12px; border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent); }}
    h1 {{ font-size: 20px; margin: 0 0 6px; }}
    p {{ margin: 0; color: color-mix(in srgb, CanvasText 68%, transparent); }}
    main {{ display: grid; grid-template-columns: minmax(0, 1fr) 290px; min-height: calc(100vh - 81px); }}
    #graph-wrap {{ overflow: auto; padding: 20px; }}
    #graph {{ min-width: 760px; width: 100%; }}
    #details {{ border-left: 1px solid color-mix(in srgb, CanvasText 18%, transparent); padding: 20px; }}
    .node {{ cursor: pointer; }}
    .node rect {{ stroke: color-mix(in srgb, CanvasText 35%, transparent); stroke-width: 1.25; }}
    .provider rect {{ fill: color-mix(in srgb, #3b82f6 18%, Canvas); }}
    .service rect {{ fill: color-mix(in srgb, #22c55e 18%, Canvas); }}
    .route rect {{ fill: color-mix(in srgb, #a855f7 18%, Canvas); }}
    .node.selected rect {{ stroke: CanvasText; stroke-width: 2.5; }}
    .edge {{ stroke: color-mix(in srgb, CanvasText 40%, transparent); stroke-width: 1.25; marker-end: url(#arrow); }}
    .pipeline-edge {{ stroke: #a855f7; stroke-width: 2; marker-end: url(#arrow); }}
    .label {{ font-size: 13px; fill: CanvasText; text-anchor: middle; dominant-baseline: middle; pointer-events: none; }}
    .section-label {{ font-size: 12px; fill: color-mix(in srgb, CanvasText 65%, transparent); font-weight: 600; }}
    .legend {{ display: flex; gap: 12px; margin-top: 10px; font-size: 12px; }}
    .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 4px; }}
    dl {{ margin: 12px 0; }} dt {{ font-size: 12px; color: color-mix(in srgb, CanvasText 62%, transparent); margin-top: 12px; }} dd {{ margin: 3px 0; overflow-wrap: anywhere; }}
    code {{ font-size: 12px; }}
    @media (max-width: 720px) {{ main {{ display: block; }} #details {{ border-left: 0; border-top: 1px solid color-mix(in srgb, CanvasText 18%, transparent); }} }}
  </style>
</head>
<body>
  <header><h1>AIPod 项目图谱</h1><p>组件依赖、路由与 Pipeline 服务链。点击节点查看详情。</p><div class="legend"><span><i class="dot" style="background:#3b82f6"></i>Provider</span><span><i class="dot" style="background:#22c55e"></i>Service</span><span><i class="dot" style="background:#a855f7"></i>Route</span></div></header>
  <main><section id="graph-wrap" aria-label="项目关系图"><svg id="graph" role="img" aria-label="AIPod component and pipeline graph"></svg></section><aside id="details"><strong>选择一个节点</strong><p>查看组件契约、路径和依赖关系。</p></aside></main>
  <script>
    const data = {data};
    const svg = document.getElementById('graph');
    const details = document.getElementById('details');
    const NS = 'http://www.w3.org/2000/svg';
    const providers = data.beans.filter(bean => bean.category === 'provider');
    const services = data.beans.filter(bean => bean.category !== 'provider');
    const positions = new Map();
    const nodeWidth = 150, nodeHeight = 46;
    const height = Math.max(460, 120 + Math.max(providers.length, services.length) * 78 + data.routes.length * 74);
    svg.setAttribute('viewBox', `0 0 920 ${{height}}`);
    svg.setAttribute('height', height);
    function element(tag, attrs = {{}}, text = '') {{ const node = document.createElementNS(NS, tag); Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v)); node.textContent = text; return node; }}
    const defs = element('defs'); const marker = element('marker', {{id:'arrow', viewBox:'0 0 10 10', refX:'9', refY:'5', markerWidth:'6', markerHeight:'6', orient:'auto-start-reverse'}}); marker.append(element('path', {{d:'M 0 0 L 10 5 L 0 10 z', fill:'currentColor'}})); defs.append(marker); svg.append(defs);
    svg.append(element('text', {{x:95, y:32, class:'section-label'}}, 'PROVIDERS'));
    svg.append(element('text', {{x:465, y:32, class:'section-label'}}, 'SERVICES'));
    svg.append(element('text', {{x:760, y:32, class:'section-label'}}, 'ROUTES / PIPELINES'));
    function place(items, x, startY, type) {{ items.forEach((item, index) => positions.set(item.id || `route:${{item.name}}`, {{x, y:startY + index * 78, type, item}})); }}
    place(providers, 95, 82, 'provider'); place(services, 465, 82, 'service'); place(data.routes, 760, 82, 'route');
    function line(a, b, className) {{ svg.append(element('line', {{x1:a.x + nodeWidth / 2, y1:a.y + nodeHeight / 2, x2:b.x - nodeWidth / 2, y2:b.y + nodeHeight / 2, class:className}})); }}
    data.beans.forEach(bean => (bean.dependencies || []).forEach(dep => {{ const source = positions.get(dep), target = positions.get(bean.id); if (source && target) line(source, target, 'edge'); }}));
    data.routes.forEach(route => {{ const source = positions.get(`route:${{route.name}}`); (route.services || []).forEach(service => {{ const target = positions.get(service); if (source && target) line(source, target, 'pipeline-edge'); }}); }});
    function show(item, type) {{
      document.querySelectorAll('.node').forEach(node => node.classList.remove('selected'));
      const selected = document.getElementById(`node-${{item.id || item.name}}`); if (selected) selected.classList.add('selected');
      if (type === 'route') {{ details.innerHTML = `<strong>${{escapeHtml(item.name)}}</strong><dl><dt>Pipeline</dt><dd><code>${{escapeHtml(item.pipeline)}}</code></dd><dt>服务链</dt><dd>${{(item.services || []).map(escapeHtml).join(' → ') || '未解析到服务'}}</dd><dt>描述</dt><dd>${{escapeHtml(item.description || '—')}}</dd></dl>`; return; }}
      details.innerHTML = `<strong>${{escapeHtml(item.id)}}</strong><dl><dt>类型</dt><dd>${{escapeHtml(item.category)}}</dd><dt>类路径</dt><dd><code>${{escapeHtml(item.class_path || '—')}}</code></dd><dt>依赖</dt><dd>${{(item.dependencies || []).map(escapeHtml).join(', ') || '—'}}</dd><dt>输入</dt><dd>${{escapeHtml(JSON.stringify(item.inputs || {{}}))}}</dd><dt>输出</dt><dd>${{escapeHtml(JSON.stringify(item.outputs || {{}}))}}</dd><dt>描述</dt><dd>${{escapeHtml(item.description || '—')}}</dd></dl>`;
    }}
    function escapeHtml(value) {{ return String(value).replace(/[&<>"']/g, char => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}}[char])); }}
    positions.forEach(position => {{ const item = position.item, key = item.id || item.name; const group = element('g', {{id:`node-${{key}}`, class:`node ${{position.type}}`, transform:`translate(${{position.x - nodeWidth / 2}},${{position.y - nodeHeight / 2}})`}}); group.append(element('rect', {{width:nodeWidth, height:nodeHeight, rx:8}})); const label = (item.id || item.name).length > 18 ? `${{(item.id || item.name).slice(0, 17)}}…` : item.id || item.name; group.append(element('text', {{x:nodeWidth / 2, y:nodeHeight / 2, class:'label'}}, label)); group.addEventListener('click', () => show(item, position.type)); svg.append(group); }});
  </script>
</body>
</html>'''


def handle_visualize(args) -> None:
    """Generate the graph file and optionally open it in the default browser."""
    try:
        beans, routes = _load_project_graph()
    except ProjectModelError as error:
        print(f"❌ 无法生成可视化: {error}")
        return

    output = Path(args.output or DEFAULT_OUTPUT)
    output.write_text(_graph_html(beans, routes), encoding="utf-8")
    print(f"🗺️  已生成项目图谱: {output}")
    print(f"   组件: {len(beans)} 个 | 路由: {len(routes)} 条")
    if args.open:
        webbrowser.open(output.resolve().as_uri())
