# 2016—2018 季度末总市值一次性补缺

本工具仅处理 2016Q1—2018Q4 当前沪深名单回溯中的适用缺口，不增加常规采集流程，不新建业务表。实际新增及剩余缺口以本机验收报告为准；不将当前成员回溯称为当时历史全市场。

## 计算与来源

总市值（元）＝历史未复权收盘价（元／股）× 对应交易日历史总股本（股）。这是计算市值，数据源没有直接返回该计算结果。

- 价格使用 BigQuant `cn_stock_real_bar1d.close`，查询只选择目标日和需补缺／核对的股票，同时约束 SQL 日期及日期分区 `filters`。账号凭据只在内存使用，认证诊断不写入日志。
- 总股本使用巨潮 `p_stock2215.F003N`，按 Decimal 乘 10,000 从万股换算为股。调用方式和字段依据 [AKShare 官方源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/stock_share_changes_cninfo.py)。每只股票串行请求，间隔至少一秒；核对 `count/total`，截断时拆分时间段。
- 匹配交易日及以前最近的有效股本记录；同日金额冲突不能判定时留空。保留变动日、公告日、原因及股份结构。财报快照可能晚于统计日公告，因此不宣称可用于无前视偏差回测。
- 价格缺口依次检查本地腾讯精确日期及腾讯未复权日 K；不使用历史响应附带的最新 `qt` 市值。不确定停牌及系统沿用口径时，不用其他日期收盘价替代。
- 巨潮股本缺口才尝试通达信历史总股本；保留浮点协议精度并要求独立重叠核对。无法确认适用股本时继续留空，不把季度财报股本无条件当成交易日股本。
- 以 2018 年已有东方财富总市值做独立核对。A/H、A/B 公司仍使用现有总股本乘 A 股价口径，不改为各市场分别估值相加。超出来源舍入或已识别协议精度的差异，定位原价格和股数字段并隔离，不采用宽泛相对误差放行。

## 工具与运行资料

独立 CLI 位于本机 `data/verification/tools/backfill_market_cap_2016_2018.py`，隔离回归检查位于同目录 `test_backfill_market_cap_2016_2018.py`。验证目录被 Git 忽略，工具及 SDK 环境没有随仓库提交；迁移电脑时需单独保留这些维护工具及来源响应。

命令使用已有 BigQuant SDK 环境，项目根目录中执行：

```powershell
$runtime = 'data/verification/tools/bigquant-runtime/Scripts/python.exe'
$run = 'data/verification/samples/market-cap-backfill-2016-2018/run-20261009'
& $runtime data/verification/tools/backfill_market_cap_2016_2018.py inventory --run $run
& $runtime data/verification/tools/backfill_market_cap_2016_2018.py collect --run $run
& $runtime data/verification/tools/backfill_market_cap_2016_2018.py verify --run $run
& $runtime data/verification/tools/backfill_market_cap_2016_2018.py rehearse --run $run
& $runtime data/verification/tools/backfill_market_cap_2016_2018.py import --run $run
```

`inventory/collect/verify` 不写日常库；`price-gaps/share-gaps` 可分别重跑缺口核对。已成功保存的请求复用，失败请求有限重试，401/403/429 停止该来源。工具拒绝低于一秒请求间隔及禁用校验断言的 `-O` 模式。

运行候选及原始响应保存在 `data/verification/samples/market-cap-backfill-2016-2018/<run-id>/`；盘点、核对、差异、演练、发布和页面验收报告保存在 `data/verification/reports/market-cap-backfill-2016-2018/`；动作日志保存在 `data/verification/logs/market-cap-backfill-2016-2018/`。候选文件是运行暂存与证据，不生成长期 baseline JSON。

## 现有表发布与验收

导入仅在核对摘要、候选哈希及隔离库演练通过后执行，先使用项目 `Database.backup`（SQLite Online Backup API）生成 `data/backups/before-cap-backfill-2016-2018-<timestamp>.sqlite3`。不裸复制在线数据库主文件。

在一个事务中再次按成功／非拒绝批次读取当前有效市值，只插入仍缺少的股票／日期组合。复用当前 `member_import_id`，保留已有冻结日期和成员。缺少冻结名单的季度沿用项目 `cap_roster` 机制，不强行给无法确认历史归属的公司分类。

- `sw_imports` 登记一次性范围、候选哈希及成员版本，每个新增季度一批。
- `sw_cap_facts` 保存计算金额，`provenance_json` 使用兼容占位 `{}`，经 `provenance_id` 引用 `sw_cap_provenance`。
- 来源包含原价格、股数、日期、计算方法、原始文件路径／SHA256、回退及核对结果。
- 按该季度冻结名单刷新 `sw_industry_cap_quarters` 的 `expected_count/known_count/known_cap/total_cap`；不足覆盖时 `total_cap` 留空。旧名单包含上市前公司的，保持原名单及汇总分母，验收报告单列当时已上市的适用数。

隔离库验证原有效市值、其他年份汇总、财务及其他业务表、原冻结名单、结构和外键，并测试幂等。正式导入后再次核对。行业读取缓存以最新 `sw_imports.id` 为失效依据，先在 8765 原后台检查公司对比、汇总、排行与趋势；需要重启时按 AGENTS.md 操作。

## 2026-10-09 执行结果

完成 3,235 家巨潮股本历史采集，112,731 条事件／快照，响应哈希和计数全部通过。BigQuant 返回 36,055 条目标日价格记录；1,638 个价格缺口按需检查腾讯，未取得精确日价格，未沿用旧收盘价。实际新增全部使用 BigQuant 未复权价格及巨潮总股本；未使用通达信／BaoStock 回退。

| 季度 | 当时已上市应有数 | 原已有数 | 本次新增 | 仍缺 |
|---|---:|---:|---:|---:|
| 2016Q1 | 2,603 | 0 | 2,267 | 336 |
| 2016Q2 | 2,640 | 0 | 2,310 | 330 |
| 2016Q3 | 2,703 | 0 | 2,423 | 280 |
| 2016Q4 | 2,804 | 0 | 2,548 | 256 |
| 2017Q1 | 2,935 | 0 | 2,658 | 277 |
| 2017Q2 | 3,047 | 0 | 2,743 | 304 |
| 2017Q3 | 3,148 | 0 | 2,900 | 248 |
| 2017Q4 | 3,235 | 0 | 2,962 | 273 |
| 2018Q1 | 3,271 | 3,271 | 0 | 0 |
| 2018Q2 | 3,296 | 3,296 | 0 | 0 |
| 2018Q3 | 3,320 | 3,320 | 0 | 0 |
| 2018Q4 | 3,338 | 3,338 | 0 | 0 |

正式新增 **20,811 条**，写入 `sw_imports` 批次 **127—134**；2018 年原有 **13,225 条**有效记录全部保留。剩余 2,304 项中，1,585 项缺少精确未复权价格，719 项因所属公司股本核对差异隔离。93 家公司共 146 个重叠样本股本不一致，原价格一致；另有 444 个重叠项缺少精确日价格，未计算。12,350 个重叠样本在严格来源精度内通过。

2016Q1—Q3 新冻结名单可确认行业归属的成员分别为 1,195、1,218、1,239 家，其余成员保持未分类；2016Q4 起原冻结名单含 5,223 家当前成员，保留名单及原汇总分母，上市前期间单列不适用。

隔离库发布、幂等、公司读取、行业汇总和一至三级排行核对通过；正式库导入前后原市值、非目标年份、原成员、财务及其他业务表均通过校验，表结构未改变。备份为 `data/backups/before-cap-backfill-2016-2018-20261009-211444.sqlite3`（SQLite Online Backup API）。

完整验收资料见本机 `data/verification/reports/market-cap-backfill-2016-2018/run-20261009-acceptance.md`，页面记录见同目录 `run-20261009-page-acceptance.md`。未自动 commit/push。

## 2016Q1—Q3 未分类成员按当前行业回溯

用户随后授权不追求精确历史行业归属，按当前分类补齐这三期未分类成员。只填 `sw_cap_quarter_members.industry_code` 的空值，来源为当前成员版本 125；原有非空归属、成员集合、上市日期、季度交易日及市值事实保持不变。没有改写 `sw_membership_history`，因此这些填充值不能作为真实历史分类变更记录。

2016Q1、Q2、Q3 分别补充 1,408、1,422、1,464 个股票／季度归属，共 4,294 项。各期名单行业归属覆盖提升至 2,603/2,603、2,640/2,640、2,703/2,703；市值覆盖仍为 2,267、2,310、2,423 家，缺失市值继续留空。

季度名单及新汇总的 `composition` 标记为 `quarter_end_classification_with_current_fallback`，含义是保留已有季末分类，缺项以当前分类回溯。通过现有 `sw_imports` 保存逐项修改清单及来源成员版本，再追加三期行业汇总版本，使原后台缓存失效。旧汇总快照保留，其他季度不变；没有修改常规名单采集或未来季度冻结规则。

隔离演练、幂等、原分类和其他业务表保护核对，以及正式导入证据见本机 `data/verification/reports/market-cap-backfill-2016-2018/current-classification-fallback-20261009.json`。这是补采完成后的独立口径补充，原补采验收报告保留当时状态。
