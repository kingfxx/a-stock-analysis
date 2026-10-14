# 2015—2017 独立历史合成交易快照

后续用户授权停牌pre_close补价，采用独立系列保留本页普通合成原记录；新增验证、读取优先级及证据见[停牌参考价补齐](market-suspended-price.md)。

2026-10-14正式发布12期35,987条；市值有效32,355条、PE有效23,402条、PB有效30,247条。隔离幂等、逐字段来源回放、63张业务表保护及8765读取通过；五项估值留空，冲突及缺值保持partial。详见[验收报告](../../data/verification/reports/market-trade-snapshot-composite/run-20261014/acceptance.md)。

实现入口为 `quarterly_dashboard.market_snapshot_backfill`，方案见[补齐计划](../plans/2026-10-14-market-trade-snapshot-composite-backfill.md)，定义清单为 `quarterly_dashboard/resources/market_snapshot_composite_definition.json`。合成事实复用 `market_trade_snapshot`，定义为 `historical-composite-v1`，不改写东财原始快照、empty批次、旧市值、价格或财务事实。

## 来源及计算

行情、日频股本、历史有效证券目录分别查询2015—2017的12个实际季末及2018-03-30；保留沪深历史有效证券，不以当前行业名单裁剪。`cn_stock_real_bar1d` 保存日期、代码、历史名称、close、pre_close、change_ratio；`cn_stock_shares` 保存总股本、流通A股、自由流通股及流通合计。各表按日期独立计数与缓存对照；目录作为覆盖分母，外部源独立保留。

价格优先本地同日raw值，BigQuant补缺和核对；总股本优先同日 `total_shares`，A股流通股本取经东财重叠核对的 `a_float_shares`。`cn_stock_capital` 只取缺项、差异及独立样本证券的基期和窗口事件，保留最近日期的同日冲突候选。最近适用事件可补日频总股本缺项，事件流通A股只在适用性已核实后补充；未确认差异留空。

日涨跌幅为 `change_ratio * 100`，有效零保留；总市值优先旧同日最新有效值，缺少时以未复权价乘股数计算；旧市值与计算值冲突时保留旧金额并隔离股本，不让矛盾三元组通过校验。流通市值为价格乘经验证的流通A股。股数严格INTEGER，不四舍五入。缺价不沿用昨收或旧期；无效日期及重复键拒绝发布。

PE使用季末市值÷同报告期TTM归母利润，PB使用未复权价÷同报告期BPS，来自新东财行业财务；非年末TTM＝当期累计＋上年全年－上年同期累计，年末取全年。分子／分母非正或缺失留空，2015Q1/Q2因2014基期缺失不计算PE。最新财务修订不代表当时可知数据。`pe_lar/pcf_ocf_lar/pcf_ocf_ttm/ps_ttm/peg_car` 第一版全部NULL。

历史名称优先同日行情名称；其他描述继承该证券最早可用的东财最新记录，标明 `later_record_backfill` 和原行ID，机构代码冲突留空。继承值不能作为当时市场／板块状态证据。

## 公共调度、证据与恢复

`bigquant_sdk.py` 在独立SDK进程内拦截HTTP send及Flight do_action/do_get，每个实际交换通过 `collection.RequestCollector.invoke` 共享预算。Flight流在同源锁内完整消耗，保留流元数据以兼容SDK；HTTP认证字段脱敏后归档，不记录凭据或票据。预算、拒绝冷却、有限重试和取消遵守公共规则。

SDK来源数据集为 `market.trade_snapshot.bigquant_inputs`，本地只读输入集中归档于 `market.trade_snapshot.composite_inputs`；发布证据使用提供方composite、数据集 `market.trade_snapshot`。物化Arrow结果、HTTP脱敏响应及action证据按真实类型标注哈希依据，不冒充HTTP原文。每季计算证据含完整候选和逐行输入引用，事实source属于同batch，来源双哈希及缓存哈希均核验。

本机缓存在 `data/verification/samples/market-trade-snapshot-composite/run-20261014/`，报告在 `data/verification/reports/market-trade-snapshot-composite/run-20261014/`。步骤为 `collect → prepare → rehearse → publish`，使用已有SDK runtime并附加项目基础Python的site-packages；采集复用范围、哈希一致缓存，失败不重请求已验证来源。模块与定义随Git管理，缓存及凭据不入Git。

正式发布先Online Backup备份，一个事务提交12期已验证事实及发布批次结果；隔离演练使用原库备份和所需来源副本，核对首次发布、重跑幂等及非目标业务表保护。证据文件与SQLite需同时迁移。

## 读取与后续补字段

v30只更新已有市值View，不新增表；东财有效值优先、合成同日值其次、旧表回退。PB价格按相同顺序选择，API返回 `cap_source/pb_price_source`；悬浮说明显示来源及日期。成功的 `market.trade_snapshot` 批次使行业缓存失效；汇总继续使用已有冻结名单，不自动修订历史归属。

`publish_metric_patch` 在调用方核验定义并归档新旧依赖后合并指定数值字段：其他字段保留，旧版本latest=0，新完整版本version+1/latest=1，同事务提交；无变化不增版本，有值变NULL拒绝。不同定义仍须独立系列。页面按字段优先级显示一条当前结果。

实际覆盖、隔离差异及发布结果以本机 `preparation.json/rehearsal.json/publication.json` 为准；执行complete不等于字段完整。证券目录覆盖是提供方目录范围，不宣称独立核验了全部历史退市证券及有效期。
