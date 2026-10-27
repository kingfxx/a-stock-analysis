"""Real stock/source segments and selection for the existing domain services."""
from stock_analysis.collection.execution_context import current_execution


def segments(dataset):
    if dataset == 'financial':
        return [('sina', 'financial.' + kind, 'getFinanceReport2022:' + kind) for kind in ('lrb','fzb','llb','gjzb')]
    if dataset == 'prices':
        return [('tencent', 'prices_raw', 'fqkline/get'), ('tencent', 'prices_adjusted', 'fqkline/get')]
    if dataset == 'valuation':
        return [('baidu', 'valuation.' + metric, 'opendata') for metric in ('pe','pb','market_cap')] + [('eastmoney','valuation.industry','RPT_PCF10_INDUSTRY_CVALUE')]
    if dataset == 'shareholders':
        return [('eastmoney', dataset, source) for source in ('RPT_F10_EH_HOLDERNUM',)]
    if dataset == 'financing':
        return [('eastmoney', dataset, 'RPTA_WEB_RZRQ_GGMX')]
    if dataset=='business_segments':
        return [('eastmoney',dataset,'RPT_F10_FN_MAINOP')]
    if dataset=='dividends':
        return [('eastmoney',dataset,'RPT_SHAREBONUS_DET')]
    return [('cninfo' if dataset == 'announcements' else 'eastmoney', dataset, '')]


def selected(dataset, interface=''):
    execution = current_execution.get()
    allowed = execution.get('selected_segments') if execution else None
    return allowed is None or (dataset, interface) in allowed or not interface and any(d==dataset for d,i in allowed)


def versions(dataset):
    # Read the actual service constants at planning time; no duplicated version registry.
    if dataset.startswith('financial.'):
        from stock_analysis.domains.financials import fundamental_service as service
    elif dataset.startswith('prices_'):
        from stock_analysis.domains.stocks import price_service as service
    elif dataset=='business_segments':
        from stock_analysis.domains.financials.business_segment_service import DEFINITION
        return DEFINITION,DEFINITION
    elif dataset=='announcements':
        from stock_analysis.domains.stocks.announcement_service import RULE_VERSION,COLLECTION_DEFINITION_VERSION
        return RULE_VERSION,COLLECTION_DEFINITION_VERSION
    elif dataset.startswith('valuation.'):
        from stock_analysis.domains.valuation import valuation_service as service
        return (service.PEER_PARSER_VERSION,service.PEER_DEFINITION_VERSION) if dataset=='valuation.industry' else (service.COLLECTION_PARSER_VERSION,service.SAMPLING_VERSION)
    elif dataset=='dividends':
        from stock_analysis.domains.stocks import dividend_service as service
    else:
        from stock_analysis.domains.stocks import update_service as service
    return service.COLLECTION_PARSER_VERSION,service.COLLECTION_DEFINITION_VERSION
