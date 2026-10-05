"""Explicit FCFF/FCFE scenario arithmetic; input choices require separate research."""
import argparse
import json
import math
from pathlib import Path


def validate(config):
    if config.get('mode') not in ('fcff','fcfe'):raise ValueError('mode must be fcff or fcfe')
    basis='WACC' if config['mode']=='fcff' else 'cost_of_equity'
    if config.get('discount_rate_basis')!=basis:raise ValueError('Cash flow / discount rate mismatch')
    for name in ('base_cash_flow','discount_rate','shares'):
        value=config.get(name)
        if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value<=0:raise ValueError('Invalid '+name)
    years=config.get('years')
    if isinstance(years,bool) or not isinstance(years,int) or not 1<=years<=50:raise ValueError('years must be integer 1..50')
    for name in ('terminal_growth','equity_adjustment'):
        value=config.get(name)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('Invalid '+name)
    if config['terminal_growth']<=-1 or config['discount_rate']<=config['terminal_growth']:raise ValueError('Discount rate must exceed terminal growth')
    if config['mode']=='fcfe' and config['equity_adjustment']!=0:raise ValueError('FCFE must not repeat debt/net-cash adjustment')
    if not config.get('cash_flow_basis') or not config.get('assumptions'):raise ValueError('Disclose cash flow basis and assumptions')
    if not config.get('scenarios'):raise ValueError('Missing scenarios')
    for g in config['scenarios']:
        if isinstance(g,bool) or not isinstance(g,(float,int)) or not math.isfinite(g) or not -1<g<=1:raise ValueError('Invalid growth scenario')
    if 'market_price' in config and (not isinstance(config['market_price'],(float,int)) or isinstance(config['market_price'],bool) or not math.isfinite(config['market_price']) or config['market_price']<=0):raise ValueError('Invalid market price')


def scenario(config,g,rate=None):
    r=config['discount_rate'] if rate is None else rate;n=config['years'];t=config['terminal_growth'];f=config['base_cash_flow']
    pv=sum(f*(1+g)**i/(1+r)**i for i in range(1,n+1))
    terminal=f*(1+g)**n*(1+t)/(r-t)/(1+r)**n
    value=pv+terminal;equity=value+config['equity_adjustment']
    return {'growth':g,'discount_rate':r,'cash_flow_pv':pv,'terminal_pv':terminal,'terminal_share_pct':terminal/value*100,
            'equity_value':equity,'value_per_share':equity/config['shares']}


def evaluate(config):
    if config.get('mode')=='pe_scenarios':return evaluate_pe(config)
    validate(config);results=[scenario(config,g) for g in config['scenarios']]
    sensitivity=[]
    for r in [config['discount_rate']-.02,config['discount_rate'],config['discount_rate']+.02]:
        if r>config['terminal_growth']:sensitivity.extend(scenario(config,g,r) for g in config['scenarios'])
    reverse=None;reason=None
    if 'market_price' in config:
        target=config['market_price'];lo,hi=-.95,1.
        if not scenario(config,lo)['value_per_share']<=target<=scenario(config,hi)['value_per_share']:reason='Market price is outside growth search bracket (-95%, +100%)'
        else:
            for _ in range(100):
                mid=(lo+hi)/2
                if scenario(config,mid)['value_per_share']<target:lo=mid
                else:hi=mid
            reverse=(lo+hi)/2
    return {'schema_version':'equity_valuation_v1','config':config,'scenarios':results,'sensitivity':sensitivity,
            'reverse_growth':reverse,'reverse_unavailable_reason':reason,'warning':'参数条件下的测算，不证明现金流正常化、资产可加回或模型适用性。'}


def evaluate_pe(config):
    for key in ('shares','market_price'):
        x=config.get(key)
        if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<=0:raise ValueError('Invalid '+key)
    if not config.get('assumptions') or not config.get('scenarios'):raise ValueError('Disclose assumptions and scenarios')
    results=[]
    for row in config['scenarios']:
        for key in ('parent_profit','pe'):
            x=row.get(key)
            if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<=0:raise ValueError('Invalid '+key)
        p=row['parent_profit'];multiple=row['pe'];shares=config['shares'];price=config['market_price']
        results.append({'label':row.get('label',''),'parent_profit':p,'pe':multiple,'equity_value':p*multiple,'value_per_share':p*multiple/shares,'implied_pe_at_market':price*shares/p})
    return {'schema_version':'equity_valuation_v1','config':config,'scenarios':results,'warning':'自身假设下的利润与PE情景；不另加净现金，不构成目标价。'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    result=evaluate(json.loads(Path(a.config).read_text(encoding='utf-8-sig')))
    Path(a.out).write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


if __name__=='__main__':main()
