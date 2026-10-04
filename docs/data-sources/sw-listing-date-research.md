# 行业名单上市日期接入研究

研究日期：2026-10-04。下文“推荐数据设计”为最初研究方案；后续经用户确认，实施改为直接补齐当前版本，见“最终实施方案”。

## 来源实测

优先使用交易所官方批量名单，不逐股请求，不引入 AKShare 依赖。接口参数通过公开实现定位，再直接请求官方来源验证。

| 市场 | 官方接口 | 日期字段 | 本次记录数 |
|---|---|---|---:|
| 沪市主板 A 股 | `https://query.sse.com.cn/sseQuery/commonQuery.do`，`sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L`，`STOCK_TYPE=1` | `LIST_DATE`，YYYYMMDD | 1702 |
| 科创板 | 同上，`STOCK_TYPE=8` | `LIST_DATE`，YYYYMMDD | 618 |
| 深市 A 股 | `https://www.szse.cn/api/report/ShowReport`，`SHOWTYPE=xlsx`，`CATALOGID=1110`，`TABKEY=tab1` | `A股上市日期`，YYYY-MM-DD | 2904 |

沪市请求使用官方股票列表页面 Referer，分页参数、状态参数及响应 SHA256 见来源报告。深市响应为 XLSX；研究时已用 ZIP/XML 解析核对，实施时需按表头名称定位字段，并正确处理共享字符串、内联字符串及空单元格，不依赖固定列号。

三份响应共 5224 个唯一代码，无重复代码、无缺失或无效日期、无未来日期。当前 `member_import_id=3` 的 5223 家全部匹配，覆盖 100%。多出的 `689009` 不在现有名单，本次不据此扩展业务范围。

核对样本：伊利 600887 为 1996-03-12；茅台 600519 为 2001-08-27；平安银行 000001 为 1991-04-03；宁德时代 300750 为 2018-06-11；华兴源创 688001 为 2019-07-22。

样本目录：`data/verification/samples/listing-date-research-20261004/`。

来源请求及哈希：`data/verification/reports/listing-date-source-research-20261004.json`。

覆盖核对：`data/verification/reports/listing-date-coverage-20261004.json`。

## 推荐数据设计

1. `sw_memberships` 新增可空 `listing_date TEXT`，规范为 YYYY-MM-DD，定义为对应代码的 A 股上市日期。`effective_date` 仍为申万分类生效日，`source_update` 仍为申万来源更新时间。
2. 建议同时新增可空 `listing_source_key TEXT`，指向该成员版本所属 `sw_imports.source_manifest_json` 中的共享上市日期来源条目。每条来源记录 URL、请求参数、文件路径、SHA256、实际采集时间、字段名称和解析器版本。每批保存三份来源说明，不在 5223 行重复嵌入完整 JSON。
3. 初次补齐以当前成员为基础发布新的名单版本；不 UPDATE 历史成员版本，不替换申万行业分类，不重抓财务或市值。旧行新字段为空，已有季度固定成员版本保持原引用。
4. 之后在分类名单导入时读取或复用上市日期来源，仅新增股票、缺值或明确核对修订时补取。批量来源可缓存；页面 GET、个股刷新和财务／市值更新不额外请求上市日期。
5. 日期不变且其他成员字段不变时复用成员版本，不因源文件哈希、采集时间变化而复制名单。日期确有修订时发布新版本，保留旧版本与来源。缺值及请求失败不抹掉已有有效日期。

复用旧上市日期时，新名单版本的 manifest 必须携带其实际旧来源条目与原采集时间，不能把本次分类检查时间冒充日期采集时间。首次补齐也不应无条件复制到全部历史版本。

## 代码接入位置

- 新迁移文件增加字段；`industry_service.import_bundle()` 的 `INSERT INTO sw_memberships VALUES(...)` 改为显式列名。
- `industry_service.foundation()` 的字段白名单增加日期及来源键，并传递共享上市日期来源；所有复用 foundation 的财务／市值／历史工具必须保留这些字段。
- 名单版本比较加入上市日期；兼容旧 bundle 没有新增字段，不把缺失当成删除已有日期。来源键应稳定，采集元信息变化不构成业务值修订。
- `industry_sources` 新增官方名单获取和日期解析，保持原新浪名单及申万分类来源优先级。初次补齐提供独立批量导入入口；现有网页两个更新按钮仍只更新财务和市值。
- 维护页 `DESCRIPTIONS` 补充上市日期用途；仍通过 `sw_imports.obtained_at` 展示名单写入时间。日期来源的实际采集时间单独保留在 manifest，上市日期不能作为维护更新时间。
- API 可携带日期，但本次字段接入不自动新增前端列，也不自动改变上市前历史财务或市值计算规则。

## 验证与交付要求

使用隔离数据库验证迁移、旧 bundle 兼容、新版本发布、相同日期复用、修订保留和来源回退。验证空值、空字符串、非法日期、未来日期、重复代码、A/B 股日期区分，以及深市 XLSX 字符串和空单元格。核对批量结果仅匹配当前业务名单，失败保留已有资料。

核对维护页发现表、用途分类、记录数、大小及更新时间依据正确。初次日常补齐前备份数据库，保留三份原始响应和覆盖报告，检查财务／市值及原分类字段不变；完成测试后按项目规则重启 8765 后台。

## 参考

- 上交所官方股票列表：https://www.sse.com.cn/assortment/stock/list/share/
- 深交所官方股票列表：https://www.szse.cn/market/product/stock/list/index.html
- 接口定位参考（公开代码，未安装依赖）：https://github.com/akfamily/akshare/blob/master/akshare/stock/stock_info.py

## 最终实施方案

结构版本 14 增加 `sw_memberships.listing_date` 与 `listing_source_id`。日期来源统一保存在 `sw_listing_sources`，包含来源 URL、参数、响应文件、SHA256、字段、定义、解析器版本及实际采集时间；行内只引用来源 ID。首次只 UPDATE 当前成员版本的这两个字段，不新建成员版本，不改变历史成员、原分类字段、财务、市值及既有季度引用。

首次补齐工具：`python -m quarterly_dashboard.industry_memberships --directory data/industry_sources/listing_dates_20261004 --report data/verification/reports/listing-date-backfill-20261004.json`。工具在实例锁内迁移及备份，重新获取三份官方响应，再直接补齐当前名单，核对业务表记录数、季度名单引用与外键。重复执行日期不变时不重复写入。

全市场财务／市值任务开始前调用同一每日检查入口，以上海日期为键写入 `sw_membership_checks`。当天成功、失败或中断都不重复网络检查，后台重启后仍有效；下一日允许再次检查。每次完整检查读取官方沪深 A 股名单（三份响应）及申万分类／变更（两份 XLS），串行请求并间隔一秒。沪深范围排除 B 股、北交所和 689 开头的 CDR，保持当前 A 股业务范围。公开响应保存在 `data/industry_sources/membership_checks/<上海日期>/`。

新增公司自动进入名单，分类暂缺标为未分类；来源暂缺的旧公司保留，旧公司分类暂缺则保留已知分类。名单数量骤减或来源失败时沿用已保存资料并提示，不启动重复重试。成员名称、分类或范围变化仍发布新成员版本；上市日期直接补齐当前版本。普通财务／市值导入及旧 bundle 均保留已有上市日期及来源，不因此复制成员版本。

已有季度固定名单完全复用；新建季度固定名单排除上市日期晚于季末交易日的公司。已上市但没有可确认分类的公司可以作为未分类成员纳入。此次不改变历史财务聚合规则，也不回填历史成员版本的日期。

维护页已登记两张新表。`sw_listing_sources` 使用 `obtained_at`；`sw_membership_checks` 使用 `checked_at`／`finished_at`；`sw_memberships` 更新时间取名单批次与其引用日期来源采集时间的较新者，原名单批次时间不修改。空表／无时间值显示未记录，上市日期不能充当写入更新时间。

日常补齐已完成：当前成员版本仍为 3，5223/5223 家有上市日期；历史版本 1、2 的日期仍为空。财务、市值及固定季度成员记录数和季度引用未变。验收报告为 `data/verification/reports/listing-date-backfill-20261004.json`，包括备份路径及完整来源。120 项隔离测试通过（3 项此前已确认的旧采集测试失败排除），真实来源的隔离副本每日检查及重复检查复用通过，报告为 `data/verification/reports/listing-date-acceptance-20261004.json`。完整来源实测为 `data/verification/reports/membership-source-check-20261004.json`。页面读取不会创建每日检查记录，首次全市场更新任务仍会执行当天检查。日常后台日志为 `data/verification/logs/daily-8765-listing-dates.out.log` 和 `.err.log`。
