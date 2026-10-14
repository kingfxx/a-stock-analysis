# 停牌季末pre_close参考价补齐

已于2026-10-14正式发布2,718条独立参考价快照，总市值有效数由32,355增至35,073；667个空价组合未通过验证，另保留原247个有价但股数待核实项。57项相关测试、隔离幂等、原合成及其他业务表保护、来源回放和8765读取通过。详见[验收报告](../../data/verification/reports/market-suspended-price/run-20261014/acceptance.md)。

2026-10-14用户授权：原始close为空时可以使用pre_close，并要求用BigQuant股本变化表验证期间股本变动。入口为 `quarterly_dashboard.market_suspended_price`；步骤为 collect、prepare、rehearse、publish，使用现有SDK、公共同源预算及行业任务互斥，不新增表或控制层。

## 验证及独立口径

目标为已发布 `historical-composite-v1` 中2015—2017十二期的3,385个空价组合。原始行情缓存、同日股本及原快照复用，不重新请求全量行情。新缓存包括：

- `cn_stock_suspend` 同日记录，确认停牌；suspend_period保留来源整数，不推断未经核验的计时单位。
- 行情表云端查询每个目标之前最近有效close的日期和金额，并连接该实际价格日的 `cn_stock_shares` 股数；返回键集合、数量及原pre_close须与缓存一致。
- `cn_stock_capital` 对1,655家目标证券取最早价格日前的基期事件及之后至目标窗口末日的全部事件；保留同日冲突候选，不按任意一条ROW_NUMBER消除差异。
- `cn_stock_dividend` 取该区间真实ex_date事件，检查现金分红、送转及其他除权日期。股本、停牌、除权缓存均与独立聚合计数对照。

仅全部条件通过的记录可补：pre_close为正且有限，存在同日停牌证据，与最近有效未复权close相同（浮点误差1e-7以内）；锚点和目标日日频总股本相同且为正；事件表对应两日的最近有效总股本与日频一致；区间所有事件总股本均相同且非空；期间无ex_date事件。先变动再恢复也拒绝，缺证据、同日冲突、分红除权或价格不一致保持空值。

使用独立 `definition_version=historical-suspended-preclose-v1`，不把参考价冒充当日成交收盘价，也不覆盖原始空价记录。新系列保存完整行，集中证据记录旧行／旧来源ID、raw_close=NULL、pre_close、锚点价格日、股本校验方式及所有输入来源；已有描述字段保留。对原先按2018重叠样本统一隔离的总股数，仅在本次该期两端日频与事件验证都通过时恢复，并标记total_shares_restored；未通过的保持隔离。流通A股原隔离状态不自动解除。

参考市值＝pre_close×经核验的目标日总股数，流通市值只在原流通A股有有效值时计算。PE/PB按既有同报告期TTM归母利润／BPS公式重算，必要输入缺失或非正留空；五项暂缓估值仍NULL。价格日可能早于季末，不用于假定当时可成交的回测。

## 入库、读取与验收

缓存及候选位于 `data/verification/samples/market-suspended-price/run-20261014/`，报告位于 `data/verification/reports/market-suspended-price/run-20261014/`；原文继续公共归档，哈希依据真实标记为SDK Arrow／物化响应或计算证据。

正式入库先SQLite Online Backup，重新核验原空价行集合、财务与本地输入哈希；单事务提交本次通过的季度候选及完成结果。隔离演练验证首次发布及重跑幂等，保护原35,987条普通合成系列和其他业务表，核对来源双哈希、逐字段回放及外键。

v31更新已有市值View，优先东财有效值、停牌参考价系列、普通合成和旧同日市值；价格按相同优先级读取，页面仍只显示一条结果。API分别返回季末 `pb_trade_date`、实际价格日 `pb_price_date` 和来源，悬浮说明标记停牌参考价。实际价格日通过批次 `result_json.price_dates` 集中读取，不在事实表增加列。

准备阶段2,718条通过、667条拒绝。拒绝原因可以重叠；实际发布数量及最终有效市值增量以 publication.json 和验收报告为准。此次不为除权期构造调整公式，后续有充分证据时另行处理。
