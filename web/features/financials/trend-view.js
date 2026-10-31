/* Shared display primitives for enterprise and industry company trends. */
(() => {
  'use strict';
  function draw({elements, rows, allRows, active, selectedPeriod, metrics, label, positions, isPriceOrCap, hasMarketComparisons, includeDisclosureDates = true, emptyDataMessage = ''}) {
    const labels = rows.map(label);
    const xs = positions(rows);
    const axisGroups = [];
    for (const series of active) {
      const axisKey = ['market_cap','period_end_market_cap'].includes(series.key) ? 'market_cap' : series.metric.unit;
      let group = axisGroups.find(item => item.key === axisKey);
      if (!group) {
        group = {key:axisKey, unit:series.metric.unit, names:[], color:series.color};
        axisGroups.push(group);
      }
      if (!group.names.includes(series.metric.name)) group.names.push(series.metric.name);
      const axisNumber = axisGroups.indexOf(group) + 1;
      series.axis = axisNumber === 1 ? 'y' : 'y' + axisNumber;
    }
    elements.scroll.classList.toggle('wide-chart', axisGroups.length > 2);
    elements.hint.hidden = axisGroups.length <= 2;
    elements.title.textContent = active.length
      ? active.map(series => series.metric.name).join(' / ') : '选择指标开始对照';
    elements.subtitle.textContent = active.length
      ? active.map(series => series.label + (series.kind === 'bar' ? ' 柱形' : ' 折线') + '：' + series.metric.name + '（' + series.metric.unit + '）').join(' · ')
      : '选择柱形或折线以显示指标';
    const custom = rows.map((row, index) => [
      labels[index], row.period, row.publish_date || '未知',
      row.price_date || '无价格', row.qfq_price_date || '无价格',
      row.cash_dividend_details || '该报告期暂无已实施分红'
    ]);
    const showQoq = key => selectedPeriod !== 'ytd' || hasMarketComparisons(key);
    const comparisonText = (row, key, months, labelText) => {
      const date = new Date(row.period + 'T00:00:00Z');
      date.setUTCDate(1);
      date.setUTCMonth(date.getUTCMonth() - months);
      const monthEnd = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0));
      const targetPeriod = monthEnd.toISOString().slice(0, 10);
      const previous = allRows.find(item => item.period === targetPeriod)?.[key];
      const current = row[key];
      if (current == null || previous == null) return labelText + ' —（缺少比较期）';
      if (previous <= 0) return labelText + ' —（基期非正，不计算百分比）';
      return labelText + ' ' + ((current / previous - 1) * 100).toFixed(1) + '%';
    };
    const comparisonHoverText = (row, key, months, labelText) => {
      const text = comparisonText(row, key, months, labelText);
      const match = text.match(/^(同比|环比)\s+([+-]?\d+(?:\.\d+)?%)(.*)$/);
      if (!match) return `<span style="color:#aab9bf">${text}</span>`;
      const value = Number(match[2].replace('%', ''));
      const color = value > 0 ? '#f09a75' : value < 0 ? '#72c9b3' : '#aab9bf';
      return `${match[1]} <span style="color:${color}">${match[2]}</span>${match[3]}`;
    };
    const periodOffsets = {yoy:12,qoq:selectedPeriod === 'year' ? 12 : 3};
    const hover = (metric, key, showFinanceDates, showMarketDate) => metric.name + '：%{y:,.2f} ' + metric.unit +
      '<br>%{customdata[8]}' + (showQoq(key) ? '　%{customdata[9]}' : '') +
      (metric === metrics.cash_dividend && selectedPeriod === 'year' ? '<br>%{customdata[6]}' : '') +
      (metric === metrics.cash_dividend ? '<br>归属报告期 → 实际除权日：%{customdata[5]}' : '') +
      (metric === metrics.period_end_market_cap ? '<br>季末日期：%{customdata[1]}' :
        showFinanceDates && includeDisclosureDates ? '<br>报告期：%{customdata[1]}<br>披露日：%{customdata[2]}' :
          showMarketDate ? '<br>%{customdata[10]}' : '') +
      '<extra></extra>';
    const traces = [];
    const barCount = active.filter(series => series.kind === 'bar').length;
    let barIndex = 0;
    let referenceCount = 0;
    const financeSeries = active.filter(series => !isPriceOrCap(series.key) && series.key !== 'period_end_market_cap');
    const marketDateSeries = active.filter(series => isPriceOrCap(series.key));
    for (const series of active) {
      const metric = series.metric;
      const showFinanceDates = financeSeries.at(-1)?.key === series.key;
      const showMarketDate = marketDateSeries.at(-1)?.key === series.key;
      const seriesCustom = custom.map((item, index) => {
        const row = rows[index];
        const payout = selectedPeriod !== 'year' || metric !== metrics.cash_dividend ? ''
          : row.dividend_payout_ratio != null ? '分红率：' + row.dividend_payout_ratio.toFixed(2) + '%（仅已实施口径）'
          : row.profit == null || row.cash_dividend == null ? '分红率：—（数据缺失）'
          : row.profit <= 0 ? '分红率：—（归母净利润非正）' : '分红率：—（数据缺失）';
        return [...item, payout, row.period_end_price_date || '无价格',
          comparisonHoverText(row, series.key, periodOffsets.yoy, '同比'),
          comparisonHoverText(row, series.key, periodOffsets.qoq, '环比'),
          (() => {
            const priceDate = row.price_date;
            const qfqDate = row.qfq_price_date;
            if (priceDate && qfqDate && priceDate === qfqDate) return '价格交易日：' + priceDate;
            if (priceDate && qfqDate) return '不复权交易日：' + priceDate + '；前复权交易日：' + qfqDate;
            return priceDate ? '价格交易日：' + priceDate : qfqDate ? '价格交易日：' + qfqDate : '价格交易日：无价格';
          })()];
      });
      const width = barCount ? (.72 - .04 * (barCount - 1)) / barCount : 0;
      const offset = -.36 + barIndex * (width + .04);
      const referenceXs = series.kind === 'bar' ? xs.map(x => x + offset + width / 2) : xs;
      if (series.kind === 'bar') {
        traces.push({type:'bar', name:series.label + ' 柱形 · ' + metric.name, x:xs, y:series.values,
          yaxis:series.axis, width, offset, customdata:seriesCustom,
          marker:{color:series.color}, hovertemplate:hover(metric, series.key, showFinanceDates, showMarketDate)});
        barIndex++;
      } else {
        traces.push({type:'scatter', mode:'lines+markers', name:series.label + ' 折线 · ' + metric.name,
          x:xs, y:series.values, yaxis:series.axis, customdata:seriesCustom, connectgaps:false,
          line:{color:series.color,width:3}, marker:{size:5,color:series.color},
          hovertemplate:hover(metric, series.key, showFinanceDates, showMarketDate)});
      }
      if (!metric.referenceKey) continue;
      const references = rows.map(row => row[metric.referenceKey] == null ? null : row[metric.referenceKey] / metric.scale);
      referenceCount += references.filter(value => value != null).length;
      const segmentX = [], segmentY = [];
      for (let index = 1; index < rows.length; index++) {
        const previous = series.values[index - 1] ?? references[index - 1];
        const current = series.values[index] ?? references[index];
        if (previous == null || current == null || (references[index - 1] == null && references[index] == null)) continue;
        segmentX.push(referenceXs[index - 1], referenceXs[index], null);
        segmentY.push(previous, current, null);
      }
      if (segmentX.length) traces.push({type:'scatter', mode:'lines', x:segmentX, y:segmentY,
        yaxis:series.axis, connectgaps:false, hoverinfo:'skip', showlegend:false,
        line:{color:series.color,width:2,dash:'dash'}});
      if (references.some(value => value != null)) traces.push({type:'scatter', mode:'markers',
        x:referenceXs, y:references, yaxis:series.axis, showlegend:false,
        customdata:rows.map((row, index) => [labels[index], row.period, row.publish_date || '未知',
          row[metric.referenceKey + '_date'], row[metric.referenceKey + '_lag_days']]),
        marker:{symbol:'circle-open',size:11,color:series.color,line:{color:series.color,width:2}},
        hovertemplate:'%{customdata[0]}<br>' + metric.name + '参考值：%{y:,.2f} ' + metric.unit +
          '<br>实际交易日：%{customdata[3]}（相隔 %{customdata[4]} 天）' +
          '<br>长间隔参考值，非披露日成交价<extra></extra>'});
    }
    const axisTicks = {color:'#ffffff',size:13};
    const axisConfig = group => ({
      title:{text:group.names.join(' / ') + '（' + group.unit + '）',
        font:{color:group.color,size:14},standoff:12},
      tickfont:axisTicks, automargin:true, zeroline:false, rangemode:'tozero',
      gridcolor:'#304049'
    });
    const layout = {
      paper_bgcolor:'#1a2227', plot_bgcolor:'#1a2227', font:{color:'#edf4f4'},
      margin:{l:100,r:axisGroups.length > 2 ? 195 : axisGroups.length > 1 ? 115 : 35,t:43,b:96},
      barmode:'overlay', bargap:.27, hovermode:'x unified', showlegend:active.length > 0,
      xaxis:{type:'linear', tickmode:'array', tickvals:xs, ticktext:labels, tickangle:-55,
        tickfont:axisTicks, gridcolor:'#304049', zeroline:false, automargin:true,
        visible:active.length > 0,
        range:xs.length ? [xs[0] - .7, xs.at(-1) + .7] : undefined},
      yaxis:axisGroups.length ? axisConfig(axisGroups[0]) : {visible:false},
      legend:{orientation:'h',x:.5,xanchor:'center',y:1.12,font:{size:12}},
      annotations:active.length ? [] : [{text:'选择柱形或折线以显示指标',xref:'paper',yref:'paper',
        x:.5,y:.5,showarrow:false,font:{color:'#9aadb3',size:17}}]
    };
    axisGroups.slice(1).forEach((group, index) => {
      layout['yaxis' + (index + 2)] = {...axisConfig(group), overlaying:'y', side:'right',
        anchor:'free', position:1, shift:index * 86, showgrid:false};
    });
    if (axisGroups.length > 1) {
      // Give every unit the same positive/negative space ratio. Include reference
      // traces, so aligning zero never hides a negative price or financial value.
      const extents = axisGroups.map((group, index) => {
        const axis = index === 0 ? 'y' : 'y' + (index + 1);
        const values = traces.filter(trace => trace.yaxis === axis)
          .flatMap(trace => trace.y).filter(Number.isFinite);
        const positive = Math.max(0, ...values);
        const negative = Math.max(0, ...values.map(value => -value));
        return {positive, negative, scale:Math.max(positive, negative) || 1};
      });
      const positiveFraction = Math.max(...extents.map(item => item.positive / item.scale));
      const negativeFraction = Math.max(...extents.map(item => item.negative / item.scale));
      extents.forEach((item, index) => {
        const axis = layout[index === 0 ? 'yaxis' : 'yaxis' + (index + 1)];
        axis.range = [-item.scale * negativeFraction * 1.12,
                       item.scale * (positiveFraction || (negativeFraction ? 0 : 1)) * 1.12];
        axis.autorange = false;
      });
    }
    if (emptyDataMessage && active.length && active.every(series => series.values.every(value => value == null)))
      layout.annotations = [{text:emptyDataMessage,xref:'paper',yref:'paper',x:.5,y:.5,showarrow:false,font:{color:'#9aadb3',size:17}}];
    Plotly.react(elements.chart, traces, layout, {responsive:true,displaylogo:false,locale:'zh-CN'});
    return {referenceCount, showQoq, comparisonText, periodOffsets};
  }
  function history({elements, rows, active, selectedPeriod, label, showQoq, comparisonText, periodOffsets, emptyDataMessage = '', marketComparisonLabel = '股价／市值'}) {
    const historyHead = elements.head;
    const historyBody = elements.body;
    const historyFooter = elements.footer;
    const historyRows = [...rows].reverse();
    const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    historyHead.innerHTML = `<tr><th scope="col">指标 / 单位（金额：亿元）</th>${historyRows.map((row,index) => `<th scope="col" class="${index===0?'trend-history-latest':''}">${escapeHtml(label(row))}${index===0?'<small>最新一期</small>':''}</th>`).join('')}</tr>`;
    historyBody.innerHTML = active.length ? active.map(series => `<tr><th scope="row"><div class="trend-history-metric-name">${escapeHtml(series.metric.historyName || series.metric.name)}${series.key==='period_end_market_cap'?' · 季末快照':''}</div><div class="trend-history-row-labels">${showQoq(series.key) ? '同比 / 环比' : '同比'}</div></th>${historyRows.map((row,index) => {
      const value=row[series.key], scaled=value==null?null:value/series.metric.scale;
      const formatted=scaled==null?'—':scaled.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
      const changes=[comparisonText(row,series.key,periodOffsets.yoy,'同比'),
        ...(showQoq(series.key) ? [comparisonText(row,series.key,periodOffsets.qoq,'环比')] : [])];
      const changeHtml=changes.map(change=>{
        const display=change.replace(/^(同比|环比)\s*/, '');
        const match=display.match(/^([+-]?\d+(?:\.\d+)?%)/);
        const tone=match?(Number(match[1].replace('%',''))>0?'positive':Number(match[1].replace('%',''))<0?'negative':''):'';
        return `<span class="${tone}" title="${escapeHtml(change+' · '+series.metric.name+' · '+row.period)}">${escapeHtml(display)}</span>`;
      }).join('<span class="trend-history-separator">/</span>');
      return `<td class="${index===0?'trend-history-latest':''}" title="${escapeHtml(series.metric.name+' · '+row.period+(series.metric.dateKey&&row[series.metric.dateKey]?' · 交易日 '+row[series.metric.dateKey]:''))}"><strong>${formatted}</strong><div class="trend-history-change">${changeHtml}</div></td>`;
    }).join('')}</tr>`).join('') : `<tr><th scope="row">历史数据</th><td colspan="${Math.max(1,historyRows.length)}" style="text-align:left;color:var(--muted)">请选择柱形或折线以显示指标。</td></tr>`;
    if (emptyDataMessage && active.length && !rows.length) historyBody.innerHTML = `<tr><th scope="row">历史数据</th><td style="text-align:left;color:var(--muted)">${escapeHtml(emptyDataMessage)}</td></tr>`;
    const historyUnit=active.length?active.map(series=>series.metric.unit).filter((unit,index,units)=>units.indexOf(unit)===index).join('、'):'所选指标单位';
    const historyPeriodLabel={quarter:'单季度',ytd:'当年累计',year:'年度',ttm:'TTM'}[selectedPeriod] || selectedPeriod;
    elements.caption.textContent=`口径：${historyPeriodLabel} · ${historyUnit} · ${selectedPeriod === 'ytd' ? `财务同比；${marketComparisonLabel}同比及环比` : '同比／环比并排'} · 时间由新到旧`;
    historyFooter.textContent=active.length?(selectedPeriod === 'ytd' ? `当年累计财务指标仅与去年相同累计期间比较同比，不显示环比；${marketComparisonLabel === '股价／市值' ? '股价和市值' : marketComparisonLabel}保留原有时点值及同比、环比；缺少比较期或基期非正时显示原因。` : '各报告期单元格在同一行并列显示同比和环比；缺少比较期或基期非正时显示原因。'):'';
  }

  const controlsTemplate = `      <div class="controls">
        <div class="metric-choice">
        <div class="metric-choice-head"><span class="metric-choice-name">指标 A</span><div class="metric-mode-group" role="group" aria-label="指标 A 表现形式">
          <button type="button" class="metric-mode" data-series-mode="bar" aria-pressed="true" aria-controls="chart">柱形</button>
          <button type="button" class="metric-mode" data-series-mode="line" aria-pressed="false" aria-controls="chart">折线</button>
        </div></div>
        <select id="bar-metric" aria-label="指标 A">
          <option value="revenue">营业收入</option>
          <option value="profit" selected>归母净利润</option>
          <option value="adjusted_profit">归母扣非净利润</option>
          <option value="net_assets">净资产（所有者权益合计）</option>
          <option value="revenue_growth">营收同比增长率</option>
          <option value="profit_growth">归母净利润同比增长率</option>
          <option value="roe">ROE（TTM 估算）</option>
          <option value="roic">ROIC（简化估算）</option>
          <option value="operating_cash_flow">经营活动现金流净额</option>
          <option value="main_business_cash_flow">主营现金流</option>
          <option value="free_cash_flow">简化自由现金流</option>
          <option value="cash_dividend">估算现金分红（已实施）</option>
          <option value="gross_margin">毛利率</option>
          <option value="net_margin">净利率</option>
          <option value="capex">资本开支 CapEx</option>
          <option value="interest_bearing_debt">有息负债（简化）</option>
          <option value="net_cash">净现金／净负债</option>
          <option value="period_end_market_cap">季末市值</option>
          <option value="market_cap">披露日估算市值</option>
          <option value="qfq_price">披露日前复权股价</option>
        </select>
        </div>
        <div class="metric-choice">
        <div class="metric-choice-head"><span class="metric-choice-name">指标 B</span><div class="metric-mode-group" role="group" aria-label="指标 B 表现形式">
          <button type="button" class="metric-mode" data-series-mode="bar" aria-pressed="false" aria-controls="chart">柱形</button>
          <button type="button" class="metric-mode" data-series-mode="line" aria-pressed="false" aria-controls="chart">折线</button>
        </div></div>
        <select id="bar-metric-2" aria-label="指标 B">
          <option value="revenue" selected>营业收入</option>
          <option value="profit">归母净利润</option>
          <option value="adjusted_profit">归母扣非净利润</option>
          <option value="net_assets">净资产（所有者权益合计）</option>
          <option value="revenue_growth">营收同比增长率</option>
          <option value="profit_growth">归母净利润同比增长率</option>
          <option value="roe">ROE（TTM 估算）</option>
          <option value="roic">ROIC（简化估算）</option>
          <option value="operating_cash_flow">经营活动现金流净额</option>
          <option value="main_business_cash_flow">主营现金流</option>
          <option value="free_cash_flow">简化自由现金流</option>
          <option value="cash_dividend">估算现金分红（已实施）</option>
          <option value="gross_margin">毛利率</option>
          <option value="net_margin">净利率</option>
          <option value="capex">资本开支 CapEx</option>
          <option value="interest_bearing_debt">有息负债（简化）</option>
          <option value="net_cash">净现金／净负债</option>
          <option value="period_end_market_cap">季末市值</option>
          <option value="market_cap">披露日估算市值</option>
          <option value="qfq_price">披露日前复权股价</option>
        </select>
        </div>
        <div class="metric-choice">
        <div class="metric-choice-head"><span class="metric-choice-name">指标 C</span><div class="metric-mode-group" role="group" aria-label="指标 C 表现形式">
          <button type="button" class="metric-mode" data-series-mode="bar" aria-pressed="false" aria-controls="chart">柱形</button>
          <button type="button" class="metric-mode" data-series-mode="line" aria-pressed="true" aria-controls="chart">折线</button>
        </div></div>
        <select id="line-metric" aria-label="指标 C">
          <option value="profit">归母净利润</option>
          <option value="adjusted_profit">归母扣非净利润</option>
          <option value="net_assets">净资产（所有者权益合计）</option>
          <option value="revenue">营业收入</option>
          <option value="revenue_growth">营收同比增长率</option>
          <option value="profit_growth">归母净利润同比增长率</option>
          <option value="roe">ROE（TTM 估算）</option>
          <option value="roic">ROIC（简化估算）</option>
          <option value="operating_cash_flow">经营活动现金流净额</option>
          <option value="main_business_cash_flow">主营现金流</option>
          <option value="free_cash_flow">简化自由现金流</option>
          <option value="cash_dividend">估算现金分红（已实施）</option>
          <option value="gross_margin">毛利率</option>
          <option value="net_margin">净利率</option>
          <option value="capex">资本开支 CapEx</option>
          <option value="interest_bearing_debt">有息负债（简化）</option>
          <option value="net_cash">净现金／净负债</option>
          <option value="period_end_market_cap">季末市值</option>
          <option value="market_cap">披露日估算市值</option>
          <option value="qfq_price" selected>披露日前复权股价</option>
        </select>
        </div>
        <div class="period" role="group" aria-label="财务周期">
          <button type="button" data-period="quarter" class="active">单季度</button>
          <button type="button" data-period="ytd">当年累计</button>
          <button type="button" data-period="year">年度</button>
          <button type="button" data-period="ttm">TTM</button>
        </div>
        <div class="comparison-range-control">
          <label for="comparison-range">时间范围</label>
          <select id="comparison-range" aria-label="财务图时间范围">
            <option value="all">全部时间</option>
            <option value="10" selected>最近 10 年</option>
            <option value="5">最近 5 年</option>
          </select>
        </div>
      </div>`;
  const displayTemplate = `    <div id="chart-scroll" class="chart-scroll"><div id="chart" class="trend-chart" role="img" aria-label="季度财务与市场价格对照图"></div></div>
    <div class="trend-history-heading"><h3>历史数据</h3><span id="trend-history-caption">金额按所选指标单位 · 时间由新到旧</span></div>
    <div id="trend-history-scroll" class="trend-history-scroll" tabindex="0" role="region" aria-label="财务与市场指标历史数据，可横向滚动">
      <table class="trend-history-table"><thead id="trend-history-head"></thead><tbody id="trend-history-body"></tbody></table>
    </div>
    <p id="trend-history-footer" class="trend-history-footer"></p>
`;
  function template(source, ids) {
    return source.replace(/(id|for|aria-controls)="([^"]+)"/g, (match, attr, id) => `${attr}="${ids[id] || id}"`);
  }
  function controls(ids = {}) { return template(controlsTemplate, ids); }
  function display(ids = {}) { return template(displayTemplate, ids); }
  function bindShapes(root, onChange) {
    const listener = event => {
      const button = event.target.closest('[data-series-mode]');
      if (!button || !root.contains(button)) return;
      const group = button.closest('.metric-mode-group');
      const active = button.getAttribute('aria-pressed') === 'true';
      group.querySelectorAll('[data-series-mode]').forEach(item => item.setAttribute('aria-pressed', String(item === button && !active)));
      onChange();
    };
    root.addEventListener('click', listener);
    return () => root.removeEventListener('click', listener);
  }
  window.TrendView = {draw, history, controls, display, bindShapes};
})();
