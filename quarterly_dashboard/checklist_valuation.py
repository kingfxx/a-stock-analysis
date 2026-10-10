"""Render checklist valuation from its own snapshot, without model arithmetic."""
import math


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _position(percentile):
    if percentile <= 5:return '极低位'
    if percentile <= 20:return '低位'
    if percentile < 40:return '偏低位'
    if percentile <= 60:return '中位'
    if percentile < 80:return '偏高位'
    if percentile < 95:return '高位'
    return '极高位'


def valuation_item(data):
    parts=[];ids=[];positions=[];complete=True
    for metric in ('pe','pb'):
        evidence=next((e for e in data.get('evidence',[]) if e['id']=='valuation.'+metric+'.10y'),None)
        label=metric.upper()
        if not evidence:
            parts.append(label+'资料不足，待补充');complete=False;continue
        ids.append(evidence['id']);value=evidence['value']
        current=_number(value.get('current'));percentile=_number(value.get('percentile'))
        count=_number(value.get('sample_count',value.get('count')))
        # Legacy checklist snapshots also store percentile on the 0–100 scale.
        valid=(current is not None and current>0 and percentile is not None and
               0<=percentile<=100 and count is not None and count>0 and
               value.get('percentile_unit','%')=='%')
        if not valid:
            reason=value.get('unavailable_reason') or '缺少适用的正值估值或历史样本，待补充'
            parts.append(label+'：'+reason);complete=False;continue
        position=_position(percentile);positions.append(position)
        observed=value.get('current_date') or evidence.get('observed_on') or '日期未记录'
        parts.append(f'{observed}，近{evidence.get("years",10)}年同口径{label}为{current:.2f}倍，历史分位{percentile:g}%，处于{position}')
        if value.get('sparse'):
            parts[-1]+='（历史观测较稀疏）';complete=False
    if len(positions)==2 and positions[0]!=positions[1]:
        parts.append('PE与PB历史位置不同')
    if not ids:ids=['context.data_quality']
    if 'valuation' in data.get('quality',{}).get('missing_items',[]):complete=False
    return {'id':'valuation','conclusion':'；'.join(parts)+'。历史位置不代表内在价值。',
            'evidence_ids':ids,'status':'ready' if complete else 'limited' if positions else 'missing'}


def correct_valuation(result,data):
    """Return a display copy; persisted historical output and input stay intact."""
    items=[valuation_item(data) if item['id']=='valuation' else item for item in result['items']]
    return {**result,'items':items,'summary':'18项定性 checklist · '+str(sum(i['status']=='ready' for i in items))+'项资料完整'}
