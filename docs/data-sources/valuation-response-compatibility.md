# 估值与复权接口兼容（2026-10-04）

688256、688825 的百度 PE、PB、总市值请求返回了有效历史序列，但范围标签为「全部」。解析接受请求范围或「全部」，仍校验指标标题、日期及重复记录，其他范围标签不匹配时拒绝。来源记录保留实际范围「全部」和请求范围；页面按所选时间区间筛选，不把上市前不存在的数据补零。PS 继续使用来源历史总市值与适用的营收 TTM 计算。

腾讯明确请求前复权时，688825 返回 `day`，没有 `qfqday`。仅此请求分支额外请求相同股票、日期范围和条数的未复权及后复权数据：两者均须有有效 `day`，后复权不能存在 `hfqday`，且三份完整行情逐项一致。通过后才接受无调整字段分支，并在前复权版本的 `source_basis` 标记 `tencent-current-ohlc-verified-unadjusted`。直接解析普通 `day` 响应为前复权仍会拒绝；任何不一致或请求失败均留空，不回填近似值。正常 `qfqday` 路径和既有分页、复权版本核验保持不变。

披露日估算市值独立使用已保存的未复权价格和财报股本，不再依赖共享前复权版本就绪。前复权不可用时价格留空，不阻断未复权价格投影；上市前披露日没有交易价格时市值仍留空。该值是财报股本口径的估算值，不等同于来源按披露日实际股本计算的总市值。

诊断原始响应保存在 `data/verification/samples/688256-valuation-diagnosis-20261004/` 与 `data/verification/samples/688825-price-diagnosis-20261004/`，诊断及修复验收报告保存在 `data/verification/reports/`。

## 页面估值传输去重（2026-10-06）

初始页面、`GET /api/valuation` 和 `GET /api/prices` 中的估值结果采用 `transport_version=1`：唯一图表点集中在 `row_pool`，各指标及时间范围用 `row_indices` 和 `row_indices_by_frequency` 引用。前端解码恢复原 `rows`／`rows_by_frequency`，复用相同图表点；无前复权版本时的价格回退先复制要修改的点，防止影响其他频率。未标记传输版本的旧数据仍可读取。

去重只改变传输结构，不改变日／周／月聚合、观察日期、价格日期、负 PE 区间及分位计算。内部 `_p4_valuation_payload` 仍返回原结构；接口调用者需按索引读取图表点。维护和趋势内存验收记录见 `data/verification/reports/maintenance-trend-memory-fix.json`。
