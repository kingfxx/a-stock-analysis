# 全市场季度末未复权收盘价

`market_quarterly_prices` 保存当前沪深 A 股名单回溯的季末最后市场交易日收盘价，仅季末稀疏数据，不是完整日线或当时历史成分名单。本次回填范围 2014Q3—2026Q3，共 49 期。

| 表 | 字段与用途 |
| --- | --- |
| `market_quarterly_prices` | 主键 `(security_code,exchange,quarter_end)`；季度自然末日 `quarter_end`、实际交易日 `trade_date`、元／股正值 `close`、固定 `adjustment=raw`、来源／批次引用、写入时间 `obtained_at` |
| `market_price_sources` | 共享来源、数据集、SQL／日期分区参数、项目相对 gzip 响应路径及文件 SHA256、采集时间 `obtained_at` |
| `market_price_batches` | 名单版本、范围、开始／完成时间、候选哈希、发布状态及各期适用／有效／缺失数 |

价格来自 BigQuant `cn_stock_real_bar1d.close`。按年度仅查询目标日 `date,instrument,close`，同时使用 SQL 日期条件和 `filters` 日期分区，不读取后复权表或乘复权因子。腾讯上证指数日线确认真实市场日历；与原季度市值名单日期冲突时停止，不改变原日期。上市前股票排除；上市日期缺失停止发布。价格 null／缺行留缺口，不当零、不直接认定停牌、不沿用旧价。

同价重复导入跳过；已有交易日／价格冲突时事务回滚，不覆盖。来源文件及候选哈希需验证。本次不创建 `instruments`、个股同步任务或关注；原 `raw_daily_prices` 和个股更新流程不变。行业内公司表 PB 已改为该季末未复权收盘价除以同报告期最新 `market_financial_performance.bps`，BPS 通过 `market_financial_industry` 轻量 View 读取。缺失或 BPS 非正留空，不回退现成 PB 或其他报告期；已有市值交易日与价格日冲突留空。

## 工具与运行

`quarterly_dashboard.market_price_backfill` 随 Git 管理。SDK、凭据、原始响应、报告和日常数据库留在本机。凭据只在内存认证，SDK 诊断抑制、异常脱敏；本次验证授权登记截止 2026-10-17。每次新采集以接口实际授权结果为准，更新凭据后可以重试，不把旧授权截止日硬编码为永久限制。

行业页面「补齐全市场市值」先沿用东方财富保存所选季度市值，再通过 `quarterly_dashboard.industry_prices` 在独立 SDK 子进程中补同一季末交易日的未复权收盘价。股价范围采用该季度固定市值名单，上市日期来自当前分类；名单不匹配则明确提示，停止股价发布。价格已全部覆盖时跳过查询；存在缺口时一次查询该日全市场三字段；勾选重新核对时再次查询，但价格冲突仍停止而不覆盖。无精确日期价格留空。

SDK 子进程限时 120 秒，只回传不含密钥的结果。权限／授权过期、查询额度、缺凭据、缺 SDK、超时和其他查询错误分别提示。股价失败独立记录在 `sw_update_runs.result_json.quarter_prices`，状态接口及页面重载／后台重启后仍显示「未复权股价同步失败」和处理提示；本次已提交市值保留。恢复授权后再次点击可补缺。SDK 能安装并不保证所有数据表永久免费可读；以账户实际权限与额度为准，参考 [官方 SDK 文档](https://bigquant.com/wiki/doc/vac4qwmQr4)。

日常按钮股价来源及结果保存在 `data/verification/samples/quarterly-price-updates/<批次时间>/`；删除候选及缺口中间文件，保留压缩原始响应、来源元数据、名单与结果以便复查。

在项目根目录运行，盘点、采集和演练不写日常库：

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
