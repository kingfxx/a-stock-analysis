"""Existing pages HTTP handlers; request contracts are unchanged."""
from __future__ import annotations

from stock_analysis.http.assets import asset_path
from urllib.parse import parse_qs
from plotly.offline import get_plotlyjs
from stock_analysis.http import rendering


class PagesRoutes:

    def _get_vendor_notes(self, parsed):
        if parsed.path.startswith('/vendor/notes/'):
            filename = parsed.path.removeprefix('/vendor/notes/')
            if filename not in {'codemirror.js', 'markdown-mode.js', 'markdown-it.js', 'footnote.js', 'purify.js', 'codemirror.css'}:
                self.send_error(404); return True
            body = (asset_path('vendor/notes/' + filename)).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/css' if filename.endswith('.css') else 'text/javascript')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers(); self.wfile.write(body); return True
        return False

    def _get_indices_page(self, parsed):
        if parsed.path in {'/indices','/indices.js','/indices.css','/congestion.js'}:
            name='indices.html' if parsed.path=='/indices' else parsed.path.lstrip('/')
            if parsed.path == '/indices':
                body=(asset_path(name)).read_text(encoding='utf-8').replace('__RESEARCH_SIDEBAR__',rendering.research_sidebar('indices')).encode('utf-8')
            else:
                body=(asset_path(name)).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type',{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.rsplit('.',1)[1]])
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(body)
            return True
        return False

    def _get_plotly_min_js(self, parsed):
        status = 200
        if parsed.path == "/plotly.min.js":
            body = get_plotlyjs().encode("utf-8")
            content_type = "text/javascript; charset=utf-8"
            self._send_body(status, body, content_type, cache='public, max-age=86400')
            return True
        return False

    def _get_stock_picker_js(self, parsed):
        status = 200
        if parsed.path == "/stock-picker.js":
            body = (asset_path('stock-picker.js')).read_bytes()
            content_type = "text/javascript; charset=utf-8"
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_samples_industry_summary_html(self, parsed):
        status = 200
        if parsed.path == "/samples/industry-summary.html":
            body = (asset_path('samples/industry-summary.html')).read_bytes()
            content_type = "text/html; charset=utf-8"
            self._send_body(status, body, content_type)
            return True
        return False

    def _get_home(self, parsed):
        status = 200
        if parsed.path == "/":
            query = parse_qs(parsed.query)
            code = query.get("code", ["601919"])[0]
            if query.get('tab')==['shipping-forecast'] and code=='601919':
                body=(asset_path('shipping-forecast.html')).read_bytes()
            else:
                body = rendering.render_page(code, query.get("refresh") == ["1"]).encode("utf-8")
            content_type = "text/html; charset=utf-8"
            self._send_body(status, body, content_type)
            return True
        return False
