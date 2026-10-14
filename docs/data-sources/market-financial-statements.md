# 全市场三表及业绩指标全字段采集

行业对比的“更新全市场财务”按钮一次更新所选报告期的四个数据集，均取 `columns=ALL`：

| 数据集 | 东方财富接口 | 取数方式 | 保存位置 |
|---|---|---|---|
| 利润表摘要 | `RPT_DMSK_FN_INCOME` | `columns=ALL` | `market_financial_income` |
| 资产负债表摘要 | `RPT_DMSK_FN_BALANCE` | `columns=ALL` | `market_financial_balance` |
| 现金流量表摘要 | `RPT_DMSK_FN_CASHFLOW` | `columns=ALL` | `market_financial_cashflow` |
| 业绩指标 | `RPT_LICO_FN_CPD` | `columns=ALL` | `market_financial_performance` |

三表的“全字段”仅指这些摘要接口的全部返回字段，不代表正式财报全部明细。三表来源层保存全 A 股（包含北交所），行业展示仍按现有沪深名单及申万分类。查看页面和个股刷新不触发本采集。

## 表与字段

Schema v18 新增三张业务表以及 `market_financial_batches`、`market_financial_sources`；v19 新增 `market_financial_performance`，完整保存业绩接口的 37 个源字段。已有表不改名、不迁移，个股新浪和行业指标的既有取数优先级不变。
字段字典是 `quarterly_dashboard/resources/market_financial_fields.json`，当前利润表 46、资产负债表 57、现金流量表 48、业绩指标 37 个源字段。未知定义的比例/辅助字段先保存，不参与业务计算，不强行解释其口径。

- 源字段转小写列名，金额及数值为可空 REAL，源代码、文本、日期为 TEXT；有效零和负值保留，无效数值阻止发布。
- 三表的 `REPORT_DATE`、业绩指标的 `REPORTDATE` 原始文本保存在 `source_report_date`；标准化 `report_date` 用于查询。公告日期保留来源文本，不解释为首次披露日期。
- 证券身份为 `security_code` + `exchange`；`secucode` 仍保留。最新版本使用 `is_latest=1`；历史真实修订版本仍可查询。
- `source_id` 引用分页来源，完整请求参数、原始文件及字段清单集中在来源表；每家公司不重复保存整行 JSON 或相同字段清单。
- `content_hash` 是规范化源字段的内容哈希；版本比较逐字段执行，金额、公告日期、适用定义变化保留真实修订。NULL 补成有效值会建立可追溯财务版本；无业务值变化时不改写事实版本及原来源，名称等描述变化不新增财务版本。
- `obtained_at` 是实际来源采集时间，`updated_at` 是本地记录写入/核对时间，不用报告期代替更新时间。

示例查询：

```sql
SELECT security_code, exchange, report_date, total_assets, total_liabilities, total_equity
FROM market_financial_balance
WHERE is_latest = 1 AND report_date = '2026-06-30';
```

## 发布、失败与原始响应

串行请求，每页 500 家，使用公共 RequestCollector 的跨进程 eastmoney 预算、至少 10 秒间隔、滚动 60 秒最多 5 次、每 20 次休息 60—120 秒及受预算约束的重试。拒绝响应归档后停止同源本轮请求并冷却。不再转换或兼容更新旧行业财务指标，不重复采部分字段利润表，不读取个股新浪。
全分页、证券唯一性、报告期、A 股范围、字段结构与有效数值校验通过后，新四表在同一个 SQLite 事务中发布。任何接口或写入失败均不发布该期的新数据；失败批次记录错误。日常按钮保留任务前分类检查；财务发布不调用旧行业导入入口，不更新旧财务表、旧财务来源或市值。
同报告期已有记录数骤减超过 20% 时阻止发布，保留原数据；缺少个别公司仅记录缺报，不使用旧期替代。
接口明确无数据且该报告期从未入库时允许记录空结果；后续披露后应再次点击更新。历史命令默认跳过已经完整处理的报告期，重新核对需加 `--recheck`。

历史旧来源位置曾使用 `market_financial_sources`。新公共响应位置相对于数据库目录：

```text
collection_sources/eastmoney/market.financial.<dataset>/<uuid>.raw.gz
```

gzip 解压后是原始 HTTP 响应内容；`collection_sources` 保存原文及压缩文件各自 SHA256、准确脱敏参数和请求状态。新事实引用对应公共 source/batch；旧来源表保留既有记录。
原始文件不计入维护页 SQLite 表大小，也不包含在单独 SQLite 备份内；资料迁移时须同时复制该目录。
不生成 baseline、全量合并 JSON 或未压缩响应副本。常规采集可在同一 UTC 日复用一组完整、同批次、同报告期／字段口径且双哈希通过的归档页；复用来源保留原始取得时间并用 `reused_source_id` 指向原记录。无法验证整组页、发现页数不一致或来源哈希损坏时整段重新请求，不跨批次拼页。显式 `--recheck` 绕过成功缓存，向来源重新请求以核对修订。该缓存是页面归档复用，不会把旧业务事实或旧专用来源表迁入公共来源体系。

## 历史补采

日常按钮只更新一个报告期的新四表。历史采集同样只写四张 `market_financial_*` 新表及其批次/来源，不调用旧行业指标导入、不更新分类和市值；逐期从新到旧提交。
原 **2016-09-30 至 2026-06-30，共 40 期** 已完成；用户追加向前两年 **2014-09-30 至 2016-06-30，共 8 期**，完成后共 48 期。更早数据不在本次授权范围内。财务输入校验与采集菜单允许最近十二年，市值仍十年；行业展示报告期按新表实际覆盖，不写死起始日期。

```powershell
python -m quarterly_dashboard.market_financial_backfill --start 2016-09-30 --end 2026-06-30 --online
# 本次追加八期，已完成期默认跳过
python -m quarterly_dashboard.market_financial_backfill --start 2014-09-30 --end 2016-06-30 --online
```

可指定 `--database`、`--report`；`--recheck` 用于重新核对已完成期。执行前生成正式 SQLite 备份，结果默认保存到 `data/verification/reports/market-financial-history/`。
`--online` 要求日常后台已经完成迁移，命令只验证结构、不并行迁移；通过现有 `sw_update_runs` 任务锁与页面按钮互斥，日常 8765 页面仍可读取。采集中不重启后台，重启会标记任务中断，采集进程随后退出；已提交期和原始响应保留。
不加 `--online` 时使用数据库实例锁，只能在停止后台后运行，结束后恢复日常后台。不要删除 `.lock` 绕过实例锁。
日常与历史财务请求共用 `RequestCollector` 的 eastmoney 预算/拒绝冷却。每报告期的三表与业绩指标各自记录公共 batch 和逐页 source；四段来源校验通过后在一个 SQLite 事务内发布。新事实只引用公共 `collection_batch_id` / `collection_source_id`，历史 `batch_id` / `source_id` 仍保持原值。字段变化新增事实版本，无变化保留原事实出处；来源有值变空时拒绝发布并标疑似撤回。
公共批次 `status=complete` 表示该请求段分页校验并发布成功；接口行数不足以证明全市场公司/字段覆盖，因此有行数据的 `coverage_status` 保持 `unknown`。批次结果记录请求数、来源数、校验/发布行数及逐字段非空/NULL计数。历史入口仅在四个数据集同 operation、scope、parser/universe/definition 和 ALL 口径均匹配且 `status=complete` 时跳过，不把执行状态解释成完整覆盖；明确 empty 或失败不能永久跳过。历史旧体系已有完整批次仍会按兼容规则跳过，避免自动重抓旧报告期；需要从旧体系切到公共来源或重新核对时显式传 `--recheck`。这会重新请求所选范围，不会迁移或改写旧来源元数据。
全区间使用同一个限频计数器，不因切换报告期重置限频；结束时验证旧 `sw_financial_facts` 完整指纹未变。失败后使用相同命令续采，已完成期跳过。

## 行业页面读取（结构版本 20）

`market_financial_industry` 是轻量逻辑 View：最新利润表左连接同公司、交易所、报告期的最新业绩指标，只投影营收、归母利润、毛利率计算字段、加权 ROE 和显式分开的旧/公共来源 ID。ROE 缺值不影响利润表指标；不要求资产负债表、现金流量表齐全。不存储副本，不进行 TTM、同比或行业汇总。

行业服务不再读取 `sw_financial_facts`，也不回退个股新浪。当前沪深申万成员范围、市值独立历史成员规则保持不变。原始营收使用 `TOTAL_OPERATE_INCOME`，归母利润使用 `PARENT_NETPROFIT`，成本使用 `OPERATE_COST`；营业收入优先 `OPERATE_INCOME`，缺值时仅非金融公司可用同口径营业总收入作毛利率分母。ROE 使用 `WEIGHTAVG_ROE` 所选报告期累计加权值。

`industry_financial_reader` 按所选周期及同比依赖期读取，至多缓存两组全市场报告期窗口；行业历史明细按所选成员临时读取，至多缓存八组汇总趋势，不长期缓存历史明细。缓存键分别纳入旧财务批次和公共财务批次的 ID/数量/完成时间，独立历史补采或原版本缺值补全发布后自动失效。页面只获取计算结果，不获取全市场历史明细或原始来源 JSON。

页面时间范围支持全部时间、最近十年、最近五年，默认十年。以当前财务口径的最新可用报告期为基准，季度分别显示最多四十期、二十期，年度分别最多十期、五期；报告期菜单、财务与市值趋势、行业历史表同步筛选。缩小范围排除当前选中期时，回到范围内最新期。仅筛选展示结果，不裁剪财务计算依赖期，不触发采集。

默认报告期从新财务报告期倒序检查所选口径的 80% 覆盖；财务更新时间采用所选报告期成功批次完成时间，名单时间单独展示。更早前置期缺失时单季度、TTM、同比保留空值。行业汇总、排行覆盖率及同组比较规则保持不变；统一来源后，中国电信三个历史期与旧新浪金额的差异属于来源切换。

旧财务表暂保留，日常按钮兼容写入已移除。旧 `market_financial.collect`、`industry_sources.download` 的混合试点联网路径、`industry_financial_extensions` 和 `industry_performance_backfill` 在线补采入口已停止，避免绕过公共预算及来源归档；分别改用 `market_financial_backfill` 四表公共入口，分类更新改用 `industry_memberships` 公共入口。`industry_bulk.collect` 继续停用。离线解析函数、只读核对、历史读取与压缩工具保留；既有旧数据不自动迁移或改写。

## 四表采集验收

自动测试使用隔离数据库，检查空表与有数据时维护统计、有效零/负值、字段补全、公告/金额/定义修订、响应去重、分页异常及事务回滚。
维护页六张新表均登记用途、分类，时间依据为各表的 `obtained_at`/`updated_at` 或批次完成时间；空表显示无数据，不用报告期推算。
