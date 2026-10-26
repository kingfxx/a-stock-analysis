"""Ordered HTTP dispatch. Feature handlers own path matching and business calls."""
from __future__ import annotations

from urllib.parse import urlparse
from stock_analysis.http.routes.commands import command_post


def get(self):
    parsed = urlparse(self.path)
    if self._get_shipping_trend(parsed):
        return
    if self._get_shipping_forecast(parsed):
        return
    if self._get_notes(parsed):
        return
    if self._get_vendor_notes(parsed):
        return
    if self._get_business_segments(parsed):
        return
    if self._get_stock_announcements(parsed):
        return
    if self._get_maintenance_collections_operation(parsed):
        return
    if self._get_maintenance_collections_evidence(parsed):
        return
    if self._get_maintenance_collections(parsed):
        return
    if self._get_indices_congestion(parsed):
        return
    if self._get_indices_catalog(parsed):
        return
    if self._get_indices(parsed):
        return
    if self._get_indices_source(parsed):
        return
    if self._get_indices_page(parsed):
        return
    if self._get_research_reports(parsed):
        return
    if self._get_research_report_file(parsed):
        return
    if self._get_maintenance_backups(parsed):
        return
    if self._get_ai(parsed):
        return
    if self._get_auth_callback(parsed):
        return
    if self._get_plotly_min_js(parsed):
        return
    if self._get_stock_picker_js(parsed):
        return
    if self._get_samples_industry_summary_html(parsed):
        return
    if self._get_static_assets(parsed):
        return
    if self._get_stock_groups(parsed):
        return
    if self._get_maintenance_unfollowed(parsed):
        return
    if self._get_maintenance_stock_cleanup(parsed):
        return
    if self._get_home(parsed):
        return
    if self._get_maintenance_storage(parsed):
        return
    if self._get_industry(parsed):
        return
    if self._get_institution_forecast(parsed):
        return
    if self._get_dividend_forecast(parsed):
        return
    if self._get_dcf(parsed):
        return
    if self._get_dcf_wacc(parsed):
        return
    if self._get_statements(parsed):
        return
    if self._get_stock_charts(parsed):
        return
    self.send_error(404)

def post(self):
    path = urlparse(self.path).path
    if self._post_shipping_forecast_save(path):
        return
    if self._post_notes(path):
        return
    if self._post_maintenance_compact(path):
        return
    if self._post_maintenance_restore_upload(path):
        return
    if self._post_ai(path):
        return
    command_post(self, path)
