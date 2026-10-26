"""HTML templates and interpolation; business payloads come from the stock service."""
from __future__ import annotations

from stock_analysis.http.assets import asset_path
import html
import hashlib
import json
from stock_analysis import application as app


TEMPLATE = asset_path('index.html').read_text(encoding='utf-8')

RESEARCH_SIDEBAR_TEMPLATE = asset_path('research-sidebar.html').read_text(encoding='utf-8')

CHECKLIST_DISPLAY_VERSION = hashlib.sha256(asset_path('ai-checklist.js').read_bytes()).hexdigest()[:12]

def render_research_sidebar(page: str, template: str) -> str:
    """Render the shared stock picker and research navigation for each page."""
    views = (('trend', '企业经营趋势', 'trend-panel'), ('business', '主营构成', 'business-panel'), ('statements', '财务分析', 'statements-panel'), ('notes', '投资笔记', 'notes-panel'), ('industry', '行业内对比', 'industry-panel'), ('maintenance', '后台维护', 'maintenance-panel'))
    if page == 'indices':
        items = [f'<a class="nav-link" data-view="{key}" href="/?tab={key}">{label}</a>' for key, label, _ in views]
        items.insert(5, '<a class="nav-link" href="/indices" aria-current="page">指数分析</a>')
        nav_role = ''
        filters = ''
        entity = ('当前指数', 'current-index-name', 'current-index-code', '指数分析', '当前指数、股票选择和研究视图')
    else:
        items = []
        for index, (key, label, panel) in enumerate(views):
            tabindex = '' if index == 0 else ' tabindex="-1"'
            items.append(f'<button id="{key}-tab" type="button" role="tab" aria-selected="{str(index == 0).lower()}" aria-controls="{panel}"{tabindex}>{label}</button>')
        items.insert(5, '<a id="indices-link" class="nav-link" href="/indices">指数分析</a>')
        nav_role = 'role="tablist"'
        filters = '<details id="industry-filter-details" class="industry-filter-details" hidden><summary>行业筛选</summary><div class="industry-filter-body"></div></details>\n  <details id="statement-filter-details" class="industry-filter-details statement-filter-details" open hidden><summary>财务筛选</summary><div class="statement-filter-body"></div></details>\n  <details id="business-filter-details" class="industry-filter-details business-filter-details" hidden><summary>主营筛选</summary><div class="business-filter-body"></div></details>'
        entity = ('当前标的', 'current-stock-name', 'current-stock-code', '—', '当前标的、股票选择和研究视图')
    caption, name_id, code_id, initial_name, aria_label = entity
    return template.replace('__SIDEBAR_LABEL__', aria_label).replace('__ENTITY_CAPTION__', caption).replace('__ENTITY_NAME_ID__', name_id).replace('__ENTITY_CODE_ID__', code_id).replace('__ENTITY_NAME__', initial_name).replace('__ENTITY_CODE__', '—').replace('__NAV_ROLE__', nav_role).replace('__NAV_ITEMS__', ''.join(items)).replace('__FILTERS__', filters)

def research_sidebar(page: str) -> str:
    return render_research_sidebar(page, RESEARCH_SIDEBAR_TEMPLATE)

def render_page(code: str, refresh: bool) -> str:
    code, payload, error = app.data.page_payload(code, refresh)
    embedded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace('<', r'\u003c')
    return TEMPLATE.replace('__RESEARCH_SIDEBAR__', research_sidebar('stock')).replace('__PAYLOAD__', embedded).replace('__CODE__', html.escape(code, quote=True)).replace('__ERROR__', html.escape(error))
