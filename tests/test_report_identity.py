import pytest

from quarterly_dashboard.company_report_service import CompanyReportService
from quarterly_dashboard.storage import Database


@pytest.mark.parametrize('year,kind,code,summary,valid', [
    ('2025', '年度', '600132', False, True),
    ('2026', '半年度', '600132', False, True),
    ('2024', '年度', '600132', False, False),
    ('2025', '半年度', '600132', False, False),
    ('2025', '年度', '600900', False, False),
    ('2025', '年度', '16001320', False, False),
    ('2025', '年度', '600132', True, False),
    ('2026', '半年度', '600132', True, False),
])
def test_designed_report_header_and_stock_profile_on_page_seven(
    tmp_path, monkeypatch, year, kind, code, summary, valid
):
    import pdfplumber

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class PDF:
        pages = [Page(''), Page(f'{year}\n重庆啤酒股份有限公司 年{kind}报告'+('摘要' if summary else ''))]
        pages += [Page('目录'), Page('释义'), Page('公司简介'), Page('主要财务指标')]
        pages += [Page(f'股票代码 {code}\n'+ '公司主要业务 供应链 市场布局 '*60)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(pdfplumber, 'open', lambda path: PDF())
    db = Database(tmp_path/'identity.sqlite3')
    db.initialize()
    service = CompanyReportService(db)
    source = tmp_path/'source.pdf'
    source.write_bytes(b'%PDF-test-designed-header')
    period = '2026-06-30' if year == '2026' else '2025-12-31'
    document = service.import_report('600132', period, source)
    if valid:
        assert service.parse(document)['page_count'] == 7
        assert service.parse(document)['cache_hit']
    else:
        with pytest.raises(ValueError, match='财报股票或年度不匹配'):
            service.parse(document)
