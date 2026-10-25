"""Version-controlled source mappings, independent of runtime request metadata."""
VERSION='v1'
METRICS=('pe_source','pe_dynamic','pe_static','pe_ttm','pe_rolling','pb','ps','ps_ttm','dividend_yield')
CS_CODES={
 '000300':'沪深300','000016':'上证50','000010':'上证180','000009':'上证380',
 '000903':'中证A100','000905':'中证500','000852':'中证1000','000906':'中证800','000985':'中证全指',
}
LG_CODES={
 '000016.SH':'上证50','000300.SH':'沪深300','000009.SH':'上证380','399673.SZ':'创业板50',
 '000905.SH':'中证500','000010.SH':'上证180','399324.SZ':'深证红利','399330.SZ':'深证100',
 '000852.SH':'中证1000','000015.SH':'上证红利','000903.SH':'中证A100','000906.SH':'中证800',
}
CS_CANDIDATES={
 '000510':'中证A500','930050':'中证A50','932000':'中证2000','000001':'上证指数',
 '000688':'科创50','000680':'科创综指','399001':'深证成指','399006':'创业板指',
 '399673':'创业板50','399330':'深证100','399005':'中小100','000015':'上证红利','399324':'深证红利',
}
BOARD_CODES={
 '上证指数':('000001','上证指数'),'上证180':('000010','上证180'),'上证50':('000016','上证50'),
 '沪深300':('000300','沪深300'),'上证380':('000009','上证380'),
 '红利指数':('000015','上证红利'),'中证红利':('000922','中证红利'),'中证500':('000905','中证500'),
}
STRATEGY_CODES={'000015','399324','000922'}


def canonical(code, provider):
    return ('sw:' if provider=='sws' else 'cn:')+str(code).split('.')[0]


def definition(dataset, fields, *, weighting='source_disclosed', period='source_disclosed', shares='pending', frequency='daily', evidence=None):
    return dict(dataset=dataset,fields=fields,statistical_method=weighting,profit_period=period,share_basis=shares,
                frequency=frequency,units={target:('percent' if target=='dividend_yield' else 'multiple') for target in fields.values()},
                conversion='identity; percent means 2.5 = 2.5%',null_sentinels=[None,'','-','--','null','None','N/A'],
                valid_zero=True,valid_negative=True,confirmation='field mapping confirmed; undisclosed weighting/share basis pending',
                evidence=evidence or 'docs/plans/2026-10-11-index-valuation-collection.md',
                observation_rule='actual trading date; monthly series includes latest unfinished month' if frequency=='monthly' else 'published trading date')


DEFINITIONS={
 ('csindex','history.rolling'):definition('index-perf',{'peg':'pe_rolling'},period='rolling'),
 ('csindex','dashboard'):definition('indexValuation',{'pe':'pe_static','peg':'pe_rolling','pb':'pb','dp':'dividend_yield'},period='static / rolling'),
 ('csindex','indicator.total_shares'):definition('indicator-xls',{'pe1':'pe_source','dp1':'dividend_yield'},shares='total_shares'),
 ('csindex','indicator.calculation_shares'):definition('indicator-xls',{'pe2':'pe_source','dp2':'dividend_yield'},shares='calculation_shares'),
 ('sws','analysis.daily'):definition('index_analysis_report',{'pe':'pe_source','pb':'pb','dp':'dividend_yield'},period='pending'),
}
for method,static,ttm,pb in (
    ('ordinary','addLyrPe','addTtmPe','addPb'),('equal','lyrPe','ttmPe','pb'),('median','middleLyrPe','middleTtmPe','middlePb')):
    DEFINITIONS['legu','pe.'+method]=definition('index-basic-pe',{static:'pe_static',ttm:'pe_ttm'},weighting=method,
                                               period='LYR / TTM (source field labels)',frequency='monthly')
    DEFINITIONS['legu','pb.'+method]=definition('index-basic-pb',{pb:'pb'},weighting=method,frequency='monthly')
