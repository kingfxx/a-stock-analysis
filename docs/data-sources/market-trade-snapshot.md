# 全市场交易日快照

v30支持同表独立历史合成系列，字段与东财原值分开追溯，详见[历史合成快照](market-trade-snapshot-composite.md)。下文47期结果仅指原东财接口执行，不包含合成发布。

Schema v29 的 `market_trade_snapshot` 保存东方财富 `RPT_VALUEANALYSIS_DET`、`columns=ALL` 的22个已登记源字段：未复权股价、涨跌幅、股本、市值、估值及证券描述。当前范围2015Q1—2026Q3，共47个季度末；采集稀疏季度快照，不宣称完整日线。

字段字典为 `quarterly_dashboard/resources/market_trade_snapshot_fields.json`。源字段转小写；文本/代码用TEXT，股数严格INTEGER，数值可空REAL。零/负PE及PCF保留，NaN/Infinity、非法字段/日期/股数阻止发布。PEG、板块等未确认定义只保存原值。原始日期存 `source_trade_date`，标准日期为 `trade_date`；交易所从SECUCODE验证。该接口实测不含开高低、成交量/额。

事实身份为股票+交易所+实际交易日+定义版本；季度仅为批次采样策略，未来日频复用同表。同日指标修订新增版本，无变化保持原来源；描述变更更新最新记录，历史原文仍可追溯。有值变空停止发布待核实。未知新增列先归档再停止，不能静默丢列。

任务复用 `sw_update_runs`，公共 `collection_batches` 使用数据集 `market.trade_snapshot`、frequency=daily、scope.sampling=quarter_end，登记实际日期、季度、范围、解析/定义版本；`collection_sources` 逐次保存请求/重试/解析、字段清单及证据；`collection_provider_state` 提供方为eastmoney。事实通过 `(source_id,batch_id)` 组合外键保证出处一致。不新增控制表。

`collection.RequestCollector` 共享同源跨进程预算：并发1，完成后10—15秒，滚动60秒最多5次，每20次额外60—120秒。403/429/验证码停止本轮并持久冷却至少一小时；普通暂时错误最多3次，60/120秒退避。旧财务尚未接入，不能宣称所有旧请求已统一保护。

按一个交易日取全市场分页，每页500条，代码升序，所有页总数/长度/去重/日期/字段及市值乘积通过后短事务发布，事实与批次完成原子提交。来源明确空记empty，各字段NULL单独计数；历史名单有效期未核实时coverage=unknown。全分页不等于指标或全市场覆盖完整。

原文经公共模块gzip原子归档到 `collection_sources/eastmoney/market.trade_snapshot/`，请求参数、字段清单、原文/压缩文件双哈希集中存公共来源。SQLite备份不含文件，迁移须同时复制并核对。中断仅同日、同范围、同参数的已解析来源可复用，保留原时间和复用关系；成功季度恢复跳过，主动修订用 `--recheck`。

后台迁移完成后，历史命令与8765共库、共用行业任务锁，不并行迁移：

```powershell
python -m quarterly_dashboard.market_trade_snapshot --start 2015Q1 --end 2026Q3
```

先核对全部本地季度日期、备份，随后由新至旧执行。固定名单已存在则保留；新历史名单按当前已上市成分回溯，标记 `current_constituents_backfill`，未知上市日期不加入，不声称真实历史归属。源行保存不因当前行业名单删减。后台重启标记中断，命令同范围恢复，不自动联网继续。

日常「补齐全市场市值」使用此快照，复用原任务和名单检查，未复权价格随快照保存，不额外调用BigQuant。旧混合批采 `industry_bulk.collect` 停用并提示新快照及独立财务入口；旧历史及离线导入/解析工具保留。新请求不写旧市值事实/来源。

行业公司行市值和PB价格直接从 `market_trade_snapshot` 读取，不再回退旧市值或旧季度价格表。新采集季度的快照发布并冻结名单后，按冻结成员生成 `sw_industry_cap_quarters` 行业市值审计版本；行业总额、覆盖率和排行总额优先读取该版本。既有历史季度不批量回填，尚无新版本时继续读取原有历史汇总。公司市值按东财原始、已核验停牌估值、历史合成的顺序取同日首个有效值；PB按未复权收盘价/同报告期BPS计算，不用源PB替换口径。`market_cap_latest` View 仍为兼容读取保留旧来源，但行业页面不使用该View。不改个股日线和复权版本。新成功批次及汇总版本使行业缓存失效，季度选择保持精确日期。

维护页新表为行业数据，时间采用真实 `obtained_at/updated_at`，无适用记录显示未记录，交易日不替代更新时间。公共来源表分类为运行与配置以反映跨领域使用。逻辑View `market_cap_latest` 不保存请求/写入时间，维护页显示未记录；真实时间须查底层快照或旧市值关联来源，不能以交易日替代。

最终报告 `data/verification/reports/market-trade-snapshot-history-20261013.json` 保存逐季度页数/空期/字段覆盖、原文双哈希及来源/事实双向验收。执行complete不意味着无空期或所有指标完整。设计详见[方案](../plans/2026-10-13-market-quarterly-valuations.md)。

## 回填起点核验

2015—2017 多源合成候选的 BigQuant SDK 样本结果见 [历史快照样本测试](bigquant-snapshot-probe.md)。该测试未发布业务事实，不改变本表的东财空期状态或既有定义版本。

2026-10-14已优先实测2015Q1（2015-03-31）：来源code=9201、明确返回数据为空，公共batch登记empty。该期无法从当前接口取得2016同比所需完整基期快照，不用其他年份补齐。证据见 `data/verification/reports/market-trade-snapshot-2015Q1-early-check-20261014.json`；其余历史季度覆盖以最终运行报告为准。

## 2026-10-14执行结果

已执行2015Q1—2026Q3的47期请求检查。2018Q1—2026Q3的35期写入158,632条，2015—2017全部12期来源明确为空；不能从该接口提供2016同比的2015基期。全47期原文双哈希及22字段逐项回放通过，SQLite quick_check为ok，外键无异常；旧市值/来源/价格表与采集前备份逐行哈希一致。新表含3个索引共88,465,408字节（约84.4MiB），公共原文归档20,555,653字节（约19.6MiB，含中断复用副本）。该次执行complete仅表示请求及验收结束，覆盖为partial，历史证券名单有效期仍未核实。最终79项相关隔离测试通过；旧财务/API模拟和旧混合集采财务读取的4个既有fixture不匹配已记录在实施检查报告，未计入通过结果。8765已重启并保持运行。
