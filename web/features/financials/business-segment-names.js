/* Reversible read model: never mutate source periods or infer identity from ITEM_CODE. */
(() => {
  'use strict';
  const numericFields=['revenue','cost','gross_profit','revenue_share','cost_share','profit_share','gross_margin'];
  const additiveFields=numericFields.filter(f=>f!=='gross_margin');
  const valid=value=>typeof value==='number'&&Number.isFinite(value);
  const clean=name=>String(name??'').normalize('NFKC').trim().replace(/\s+/g,' ').replace(/([\u3400-\u9fff])\s+(?=[\u3400-\u9fff])/g,'$1');
  const excluded=name=>/^(?:其中\s*[:：])/.test(name)||['合计','总计','主营业务合计','主营收入合计','主营业务收入合计','营业收入合计','营业成本合计','主营业务成本合计'].includes(name);
  const sourceName=row=>(row.source_items||[row]).map(r=>r.item_name).join(' ＋ ');

  function identity(code,dimension,row,rules) {
    const name=clean(row.item_name);
    if(excluded(name))return {name,key:'source:'+row.item_key,reason:'保留原始层级',excluded:true};
    for(const rule of rules.regionalPartitions||[]) {
      if(rule.code!==code||rule.dimension!==dimension)continue;
      const parts=name.match(/^(.+?)\(([^()]+)\)$/);
      if(parts&&Object.hasOwn(rule.regions,parts[1])&&rule.businesses.includes(parts[2])) {
        return {name:rule.regions[parts[1]],key:'name:'+rule.regions[parts[1]],partition:parts[2],partitionRule:rule,reason:rule.reason};
      }
    }
    // Company-specific rules take priority over universal synonyms.
    const candidates=(rules.aliases||[]).filter(r=>(r.code===code||r.code==='*')&&r.dimensions.includes(dimension)).sort((a,b)=>(a.code==='*')-(b.code==='*'));
    const alias=candidates.find(r=>r.aliases.some(value=>clean(value)===name));
    if(alias)return {name:alias.name,key:'name:'+alias.name,reason:alias.reason};
    const geographic=dimension==='3'&&name.match(/^(华东|华南|华中|华北|华西|西南|西北|东北)(?:区域|地区|战区|大区)?$/);
    if(geographic)return {name:geographic[1]+'地区',key:'name:'+geographic[1]+'地区',reason:'同地理名称的区域／地区／战区／大区标签规范，保留括号限定范围'};
    // Suffixes describe the reporting label; never strip “及相关” or parenthetical scopes.
    const simple=dimension==='3'||/及相关业务$/.test(name)?name:name.replace(/(?:业务)?分部$/,'').replace(/业务$/,'');
    const canonical=simple.length>=2?simple:name;
    return {name:canonical,key:'name:'+canonical,reason:canonical!==name?'业务／分部标签规范':'同名及格式规范（不依赖来源代码）'};
  }
  function canAggregate(group) {
    return group.length>1 && group.every(r=>r.identity.partition&&r.identity.partitionRule===group[0].identity.partitionRule)
      && new Set(group.map(r=>r.identity.partition)).size===group.length;
  }
  function aggregate(group,key,name) {
    const originals=group.map(r=>r.row);
    const result={...originals[0],item_key:key,item_name:name,source_items:originals,calculated_fields:[]};
    if(group.length===1)return result;
    for(const field of additiveFields)result[field]=originals.every(r=>valid(r[field]))?originals.reduce((sum,r)=>sum+r[field],0):null;
    result.gross_margin=valid(result.revenue)&&result.revenue>0&&valid(result.gross_profit)?result.gross_profit/result.revenue:null;
    result.calculated_fields=numericFields.filter(field=>valid(result[field]));
    return result;
  }
  function normalize(code,periods,rules=window.businessSegmentAliasRules) {
    const blocked=new Set(),conflicts=[],mapping=new Map();
    const prepared=periods.map(period=>({period,dimensions:Object.fromEntries(['1','2','3'].map(d=>[d,(period.dimensions?.[d]||[]).map(row=>({row,identity:identity(code,d,row,rules)}))]))}));
    // One collision blocks that canonical group over the entire history: no partial stitching.
    for(const entry of prepared)for(const [dimension,rows] of Object.entries(entry.dimensions)) {
      const groups=new Map();
      for(const item of rows)if(!item.identity.excluded){const key=item.identity.key; if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item);}
      for(const [key,group] of groups)if(group.length>1&&!canAggregate(group)) {
        blocked.add(dimension+'|'+key);
        conflicts.push({dimension,name:group[0].identity.name,report_date:entry.period.report_date,original_names:group.map(r=>r.row.item_name),reason:'同一期存在多个候选项目，未确认互斥分项，整组保留原始身份'});
      }
    }
    const normalized=prepared.map(entry=>{
      const dimensions={};
      for(const [dimension,rows] of Object.entries(entry.dimensions)) {
        const groups=new Map();
        for(const item of rows) {
          const blockedGroup=blocked.has(dimension+'|'+item.identity.key);
          const key=blockedGroup?'source:'+item.row.item_key:item.identity.key;
          if(!groups.has(key))groups.set(key,[]);groups.get(key).push(item);
          if(!item.identity.excluded&&!blockedGroup) {
            const id=dimension+'|'+key;
            if(!mapping.has(id))mapping.set(id,{dimension,name:item.identity.name,original_names:new Set(),reasons:new Set(),dates:new Set(),aggregated:false});
            const record=mapping.get(id);record.original_names.add(item.row.item_name);record.reasons.add(item.identity.reason);record.dates.add(entry.period.report_date);
          }
        }
        dimensions[dimension]=[...groups].map(([key,group])=>{
          const blockedGroup=blocked.has(dimension+'|'+group[0].identity.key);
          const name=blockedGroup?group[0].row.item_name:group[0].identity.name;
          const result=aggregate(group,key,name);
          if(group.length>1)mapping.get(dimension+'|'+key).aggregated=true;
          return result;
        });
      }
      return {...entry.period,dimensions,source_dimensions:entry.period.dimensions};
    });
    return {periods:normalized,version:rules.version,conflicts,mappings:[...mapping.values()].filter(r=>r.aggregated||r.original_names.size>1||!r.original_names.has(r.name)).map(r=>({...r,original_names:[...r.original_names].sort(),reasons:[...r.reasons],dates:[...r.dates].sort()}))};
  }
  window.businessSegmentNames={normalize,clean,sourceName};
})();
