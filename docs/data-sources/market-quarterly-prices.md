# 季度末未复权收盘价（旧来源层）

更新：2026-10-14。当前行业页面的未复权价格直接读取 `market_trade_snapshot`，不再读取或回退 `market_quarterly_prices`；日常「补齐季度交易快照」仅调用东财全字段接口，不再触发BigQuant价格补采，也不写本页三张旧价格表。旧价格、共享来源及批次保留用于历史追溯、已发布合成快照的输入核验及独立旧工具；数据库维护页继续统计它们。

`market_quarterly_prices` 保存原沪深 A 股名单回溯的季末最后市场交易日收盘价，仅季末稀疏数据，不是完整日线或当时历史成分名单。原回填范围 2014Q3—2026Q3，共 49 期；该范围描述旧任务，不表示日常仍持续更新此表。

| 表 | 字段与用途 |
| --- | --- |
| `market_quarterly_prices` | 主键 `(security_code,exchange,quarter_end)`；季度自然末日 `quarter_end`、实际交易日 `trade_date`、元／股正值 `close`、固定 `adjustment=raw`、来源／批次引用、写入时间 `obtained_at` |
| `market_price_sources` | 共享来源、数据集、SQL／日期分区参数、项目相对 gzip 响应路径及文件 SHA256、采集时间 `obtained_at` |
| `market_price_batches` | 名单版本、范围、开始／完成时间、候选哈希、发布状态及各期适用／有效／缺失数 |

价格来自 BigQuant `cn_stock_real_bar1d.close`。按年度仅查询目标日 `date,instrument,close`，同时使用 SQL 日期条件和 `filters` 日期分区，不读取后复权表或乘复权因子。腾讯上证指数日线确认真实市场日历；与原季度市值名单日期冲突时停止，不改变原日期。上市前股票排除；上市日期缺失停止发布。价格 null／缺行留缺口，不当零、不直接认定停牌、不沿用旧价。

旧工具同价重复导入跳过；已有交易日／价格冲突时事务回滚，不覆盖。来源文件及候选哈希需验证。不创建 `instruments`、个股同步任务或关注；原 `raw_daily_prices` 和个股更新流程不变。

## 当前页面与保留依赖

行业内公司表PB价格来自 `market_trade_snapshot.close_price`，按同一精确交易日优先东财原始值、已核验停牌参考价、普通历史合成；没有有效新快照价格时留空，不回退本页旧表。PB仍除以同报告期BPS，BPS通过 `market_financial_industry` 读取 `market_financial_performance.bps`；价格／BPS缺失或非正留空，不使用其他报告期或现成PB替代。

旧季度价格参与过2015—2017的[历史合成快照](market-trade-snapshot-composite.md)计算，本地输入导出及计算证据保留相应来源／批次引用；页面读取已发布的新快照，不在查看页面时重新读旧价格计算。后来授权的[停牌参考价补齐](market-suspended-price.md)使用独立验证缓存及定义身份，不改写本页旧价格表。

`market_trade_snapshot.target_date` 仍可读取旧价格表已有的季度交易日映射，与冻结名单日期交叉核对；这属于采集前日历验证，不是页面股价回退。`quarterly_dashboard.industry_prices` 和 `market_price_backfill` 的旧价格读写实现仍保留，但当前日常按钮调用链不进入它们。

因此，日常停写及页面停读不代表表、来源文件和SDK工具可以直接删除。若以后归档或移除，须另行盘点日期映射、合成证据及旧工具依赖。SQLite备份不包含压缩来源文件，迁移须同时保留文件并校验哈希。

## 保留的独立工具与历史运行

`quarterly_dashboard.market_price_backfill` 随 Git 管理。SDK、凭据、原始响应、报告和日常数据库留在本机。凭据只在内存认证，SDK 诊断抑制、异常脱敏；本次验证授权登记截止 2026-10-17。每次新采集以接口实际授权结果为准，更新凭据后可以重试，不把旧授权截止日硬编码为永久限制。

旧版行业按钮曾先保存季度市值，再通过 `quarterly_dashboard.industry_prices` 的独立SDK子进程补同一季末价格；该串联已被当前东财全字段快照流程替代，不能再通过日常按钮启动BigQuant价格补缺。保留适配器原逻辑按季度固定名单查询，名单不匹配停止发布，已覆盖跳过，重新核对遇到价格冲突仍停止；这些行为仅适用于显式调用旧工具。

旧SDK子进程限时120秒，只回传不含密钥的结果；仍有权限／过期、额度、凭据、SDK缺失及超时的错误分类。旧任务保存的 `sw_update_runs.result_json.quarter_prices` 及相应失败提示保留用于历史审计；恢复SDK授权后，显式运行独立工具才能补旧表，不是再次点击当前日常按钮。SDK实际权限和额度以账户结果为准，参考 [官方 SDK 文档](https://bigquant.com/wiki/doc/vac4qwmQr4)。保留旧工具不表示其请求已接入公共调度；新增或改造批采须按项目采集规则接入 `collection.py`。

旧版按钮产生的价格来源及结果保存在 `data/verification/samples/quarterly-price-updates/<批次时间>/`；保留压缩原始响应、来源元数据、名单与结果以便复查。当前按钮的来源归档使用公共 `collection_sources/`，不再为日常股价创建这类旧批次目录。

以下命令仅为显式启动的独立旧价格回填工具，在项目根目录运行；盘点、采集和演练不写日常价格事实，不属于当前页面常规更新流程：

```powershell
$taskRuntime = 'data/verification/tools/bigquant-runtime/Scripts/python.exe'
$taskFolder = 'data/verification/samples/quarterly-raw-prices-20261010'
& $taskRuntime -m quarterly_dashboard.market_price_backfill inventory --folder $taskFolder --start 2014-09-30 --end 2026-09-30
& $taskRuntime -m quarterly_dashboard.market_price_backfill collect --folder $taskFolder
& $taskRuntime -m quarterly_dashboard.market_price_backfill rehearse --folder $taskFolder --test-db data/verification/tools/quarter-price-rehearsal.sqlite3
# 核对并停止日常后台后，先备份再导入：
& $taskRuntime -m quarterly_dashboard.market_price_backfill import --folder $taskFolder
```

按年断点复用且验证哈希。导入必须有相同候选哈希的成功演练，先 SQLite 在线正式备份再迁移、事务写入，取得日常库实例锁。完成后按项目授权重启 8765 并检查启动日志、统计接口、原关注列表。

维护页价格／来源为「行业数据」，批次为「运行与配置」。前两者时间为 `obtained_at`，批次为 `finished_at`；空表／空时间显示「未记录」，不以季度或交易日代替采集时间。

响应保存在 `data/verification/samples/quarterly-raw-prices-20261010/`，最终报告在 `data/verification/reports/`，日志在 `data/verification/logs/`；正式备份在 `data/backups/`。临时演练库及测试产物执行后清理，来源响应和正式备份保留。
