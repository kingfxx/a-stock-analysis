# A 股季度分析页面设计文档

更新日期：2026-09-30。本文说明当前实现的数据源、代码职责、数据存储，以及加载、更新与备份流程。功能概览、页面截图、启动方式和指标口径见 [README](../README.md)。

## 当前数据源

以下是**当前运行代码实际调用**的数据源；不是候选接口列表。数据源地址和字段映射分别定义在 [sources.py](../quarterly_dashboard/sources.py) 与 [valuation.py](../quarterly_dashboard/valuation.py)。

| 来源 | 实际获取的数据 | 在页面中的用途 | 实现入口 |
|---|---|---|---|
| 新浪财报三表 | 利润表 `lrb`：累计营收、归母净利润、合并净利润、营业成本、利润总额、所得税费用、费用化利息及财务费用项下利息收入 | 收入／利润、同比、利润率、ROE、ROIC 的利润输入 | `sources.fetch_financial_reports()` |
| 新浪财报三表 | 资产负债表 `fzb`：报告期末股本、归母净资产、合并所有者权益、货币资金及债务组成科目 | 披露日估算市值、ROE、ROIC、有息负债与净现金 | 同上，与利润表按报告期合并 |
| 新浪财报三表 | 现金流量表 `llb`：累计经营活动现金流净额、购建长期资产支付的现金 | 经营现金流、CapEx、简化自由现金流 | `sources.fetch_cash_flow_reports()` |
| 腾讯行情 | 股票名称；未复权、前复权日 K | 共享日价服务供财务、估值和筹码取价；不复权价格供市值与股息率计算 | `sources.fetch_stock_name()`、`fetch_price_history()`、`price_service.PriceService` |
| 百度股市通 | 历史 PE(TTM)、PB、总市值序列 | 估值图的月度 PE／PB；总市值用于计算 PS(TTM) | `valuation.fetch_valuation_series()` |
| 东方财富分红数据 | 已实施分红的归属报告期、除权日、税前每股派息、数据源总股本 | 财务图的估算现金分红、年度悬停分红率，以及估值图近 12 个月股息率 | `valuation.fetch_dividend_events()`、`fetch_dividend_yields()` |
| 东方财富同行估值 | 当前同行业 PE／PB／PS 均值、样本数量 | 当前同行对照，只有当前快照，不是历史行业曲线 | `valuation.fetch_industry_snapshot()` |
| 东方财富 F10／股东户数详情 | 股东人数、统计截止日、公告日及人数口径 | 独立保存各来源事实；价格由共享服务匹配 | `chips._fetch_chip_report()`、`update_service.ChipService` |
| 东方财富融资融券 | 日期范围内的日度原始字段，包括融资／融资融券余额、净买入 | 逐日增量入库；最近一年融资与共享价格对照 | `chips._fetch_chip_report()`、`update_service.ChipService` |

代码中的接口地址：

| 接口 | 地址 |
|---|---|
| 新浪财报三表 | `https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022` |
| 腾讯名称 | `https://qt.gtimg.cn/q=` |
| 腾讯日／月 K | `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get` |
| 百度估值 | `https://gushitong.baidu.com/opendata` |
| 东方财富分红 | `https://datacenter-web.eastmoney.com/api/data/v1/get` |
| 东方财富同行估值 | `https://datacenter.eastmoney.com/securities/api/data/v1/get` |

PS(TTM)、ROIC、ROE、自由现金流、分红率、股息率及分位统计并非全部直接取自接口，而是基于上述输入按本项目口径计算。具体公式见 [README 的指标口径](../README.md#指标口径)。

[docs/data-sources/](data-sources/) 和 [experiments/source_probe/](../experiments/source_probe/) 保留早期数据源调研、探针与结果。mootdx、BaoStock、通达信盘后下载包等仅用于这些实验，当前页面没有调用它们；运行页面不需要安装这些实验依赖。历史调研文档中的候选源和阻断状态不等同于当前页面功能。

## 代码文件用途

| 文件／目录 | 主要职责 |
|---|---|
| [app.py](../app.py) | 命令行入口，解析 `--port`、`--open-browser`，启动本地服务。 |
| [start_dashboard.bat](../start_dashboard.bat) | Windows 本机启动脚本，切换到仓库目录、运行 Python 并打开浏览器。 |
| [quarterly_dashboard/server.py](../quarterly_dashboard/server.py) | 本地 HTTP 服务；组织财报、分红、估值、筹码和 `/api/prices`，使各异步接口使用固定价格版本。 |
| [quarterly_dashboard/storage.py](../quarterly_dashboard/storage.py) | SQLite 连接、两版结构迁移、同步事务、实例锁、完整性检查和一致备份。 |
| [quarterly_dashboard/update_service.py](../quarterly_dashboard/update_service.py) | 融资及股东来源事实、旧筹码 JSON 导入、增量更新和失败保留。 |
| [quarterly_dashboard/price_service.py](../quarterly_dashboard/price_service.py) | 共享日价抓取、重叠/锚点核对、前复权版本发布、租约与清理。 |
| [quarterly_dashboard/price_projection.py](../quarterly_dashboard/price_projection.py) | 财务、估值和筹码图表的共享价格日期匹配。 |
| [quarterly_dashboard/chips.py](../quarterly_dashboard/chips.py) | 筹码数据适配、完整分页检查、股东人数价格快照匹配、融资一年窗口和历史覆盖保护。 |
| [quarterly_dashboard/network.py](../quarterly_dashboard/network.py) | 统一数据源 Session，默认直连，可通过 `DASHBOARD_USE_SYSTEM_PROXY=1` 启用系统／环境代理。 |
| [quarterly_dashboard/sources.py](../quarterly_dashboard/sources.py) | 新浪／腾讯适配层：请求数据、解析数值、统一字段名称和利息费用符号、校正历史披露日期、分段获取行情。 |
| [quarterly_dashboard/core.py](../quarterly_dashboard/core.py) | 财务纯计算：累计值拆季、连续四季度 TTM、年度视图、披露日快照、同比、利润率、ROE、债务／净现金、分红归属及年度分红率；调用 ROIC 纯函数。 |
| [quarterly_dashboard/roic.py](../quarterly_dashboard/roic.py) | ROIC 纯计算与输入检查，保留 `excess_cash`、`non_operating_adjustments` 两个扩展口；不独立取数、不单独启动任务。 |
| [quarterly_dashboard/valuation.py](../quarterly_dashboard/valuation.py) | 百度／东方财富适配与估值计算：按月采样、PS、股息率、前复权股价合并、历史分位和同行对照。 |
| [web/index.html](../web/index.html) | 页面模板及前端 JS：股票搜索、A/B/C 选择、Plotly 图表、悬停详情、多轴零点对齐、后台加载状态及标签页选项保存。 |
| [tests/](../tests/) | Python 回归测试，覆盖解析、财务周期、ROIC、分红率、缓存保护、后台加载和估值。 |
| `tests/chart_hover.cjs`、`chart_axes.cjs`、`background_loading.cjs` | 用 Node.js 执行页面中的实际渲染／加载逻辑，检查悬停、零轴范围和异步更新；由 Python 测试调用。 |
| [docs/plans/](plans/) | 历史需求与实现方案；当前行为以代码、本设计文档和 [README](../README.md) 为准。 |
| [experiments/source_probe/](../experiments/source_probe/) | 独立数据源实验，不是生产页面的调用路径。 |
| [requirements.txt](../requirements.txt)、[.gitignore](../.gitignore)、[LICENSE](../LICENSE) | 运行依赖版本、本地数据忽略规则、项目许可证。 |

页面的数据流为：`来源适配 → SQLite 筹码事实/共享价格 + 财务/分红/估值 JSON → 计算与日期匹配 → 页面 JSON → Plotly 图表`。筹码事实更新不等待取价；页面先读取缓存，再在后台协调价格版本和各模块响应。财报字段映射在 `sources.py`，计算公式在 `core.py`／`roic.py`／`valuation.py`，图表交互在 `web/index.html`。

## 数据存储方式

### 文件布局

P2/P3 的融资、股东和共享日价使用 SQLite；财报、分红、估值及手工修正仍使用 UTF-8 JSON，留待 P4。路径相对于仓库根目录，首次需要写入时创建。

```text
data/
├── stock_analysis.sqlite3     # 融资、股东、共享日价与同步状态
├── stock_analysis.sqlite3.lock # 应用实例锁；退出后 OS 自动释放
├── backups/                  # SQLite 在线一致备份及迁移前备份
├── fundamentals/
│   ├── 300750.json             # 财报、名称、披露日价格快照、分红事件
│   └── 300750.json.bak         # 该文件上一次成功写入前的版本
├── valuation/
│   ├── 300750.json             # 已整理的月度估值及当前行业快照
│   └── 300750.json.bak
├── chips/
│   ├── shareholders/300750.json # 旧缓存，仅供首次导入并保留原文件
│   └── financing/300750.json    # 旧缓存，仅供首次导入并保留原文件
└── verification/              # 人工核验的官方财报 PDF、截图等资料
```

上述目录均由 `.gitignore` 排除，不随 Git 提交或推送。`data/cache/` 是早期路径，也被忽略，当前页面不使用它。`experiments/source_probe/results/` 的实验摘要则属于仓库中的调研资料，与运行缓存分开。

### 财报与分红缓存：`data/fundamentals/<代码>.json`

缓存保存**从接口解析、映射后的财报字段**，不是原封不动的接口响应，也不是预先保存好的所有图表指标。

| 字段 | 内容及单位 |
|---|---|
| `code`、`name`、`updated_at` | 股票代码、缓存名称、整批财务加载时记录的时间（UTC ISO 时间）；字段补齐或分红更新不一定改变 `updated_at`。 |
| `reports[]` | 按报告期保存的财报记录。`period` 为报告期末，`publish_date` 为校正后的披露日，`source_publish_date` 保留来源日期，`source_update_time` 保留来源更新标记。 |
| `reports[].*_ytd` | 年内累计利润表／现金流量表金额，单位为元。例如 `revenue_ytd`、`profit_ytd`、`operating_cash_flow_ytd`；不是单季度数。 |
| `reports[].equity`、`total_equity`、`monetary_funds`、债务组成科目 | 报告期末余额，单位为元；`equity` 为归母净资产，`total_equity` 为含少数股东权益的合并权益。 |
| `reports[].shares` | 报告期末股本，单位为股。 |
| `prices.raw`、`prices.qfq` | 未复权／前复权披露日价格快照，包含披露日、实际交易日和 `close`（元／股）。 |
| `prices.raw_reference`、`prices.qfq_reference` | 长停牌等情形下的参考快照，另保存相隔天数；与正式快照分开。 |
| `dividend_events[]` | 已实施分红：`report_period` 为归属期，`date` 为除权日，`per_share` 为元／股，`total_shares` 为接口股数。 |
| `*_basis` | 披露日、价格、财报字段、现金流、分红等处理口径的版本标记；程序据此识别需补齐或迁移的旧缓存。 |
| `warnings` | 缓存中的获取／处理警告；部分失败警告只随当次响应返回，不一定落盘。 |

共享日 K 在 SQLite 中保存完整历史；财务 JSON 缓存只留下披露日前的正式快照和参考点。财务金额在计算时使用元，前端显示“亿元”时再除以 `1e8`。

单季度、TTM、同比、ROE、ROIC、利润率、有息负债、净现金、分红金额及年度分红率在生成页面数据时计算，**不写入这份原始字段缓存**。两个 ROIC 扩展字段可以保存在报告记录中：`excess_cash` 为期末超额现金，`non_operating_adjustments_ytd` 为累计税前非经营性调整；自动迁移及手动刷新会保留已有值。JSON 的 `null` 对应 Python `None`，表示未知，不能当作零。

### 估值缓存：`data/valuation/<代码>.json`

这份缓存保存的是**按月整理后的结果**，包含部分计算值，与财报缓存的存储层级不同。

| 字段 | 内容 |
|---|---|
| `rows[]` | 每月估值记录：`date`、`pe`、`pb`、`ps`、`dividend_yield`、`dividend_yield_date`、`qfq_close`、`qfq_close_date`。不同来源的采样日可能不同，价格／股息率保留各自日期。 |
| `industry` | 当前同行业 PE／PB／PS 均值、样本数量等快照，不保存逐月行业历史。 |
| `updated_on` | 成功更新日期，使用服务所在机器的本地日期；同一天且口径版本一致时复用。 |
| `basis`、`warnings` | 估值处理口径版本与保存的警告。 |

PE／PB／PS 的单位为倍，股息率存百分数（例如 `3.5` 表示 `3.5%`），股价为元／股。百度总市值按亿元转为元后计算 PS；当前月度缓存不保留该总市值输入序列。完整日线和原始接口响应也不写入该文件。历史分位数及摘要在读取月度缓存后按所选年限重新计算。

### 筹码事实与旧缓存

`data/chips/<类型>/<代码>.json` 在迁移后保留原样，并按路径和哈希幂等导入 SQLite；正常筹码读写不再使用它们。导入前另存原文件副本和数据库备份。

`financing_daily` 按来源、股票、交易日保留完整历史；30 个自然日回看窗口 UPSERT，展示查询仅取最近 365 天。`shareholder_observations` 保存统计日、公告日、来源和人数口径，不把不同口径混为一条曲线。价格来自共享日价服务；股东取统计日或之前最多 15 天的最近交易日。详细字段见 [README](../README.md#筹码数据与存储)。

`/api/shareholders` 与 `/api/financing` 独立更新事实，失败保留旧记录；首次明确无数据才保存空状态。旧 JSON 不再日常改写。可选字段缺失时沿用已验证值及来源记录，来源明确置空时保存未知；融资历史缺口不被当作删除。

### 加载、更新与备份

1. **打开或切换股票**：根页面从 JSON 与 SQLite 读取已有数据，不等待外部网络；先显示已有图表。
2. **后台补齐**：首次查询或口径版本落后时，`/api/financial?code=<代码>` 加载／迁移财报和必要价格。旧财报字段补齐通常只重取相关报表，不重抓价格；披露日期或价格口径迁移需要重新匹配价格。
3. **分红**：`/api/dividends?code=<代码>` 独立补充已实施分红，写回财务缓存。已有有效分红缓存通常直接复用；缺少或空分红会重试。财务图无需等待它即可显示。
4. **估值**：`/api/valuation?code=<代码>` 在缺缓存、跨日或口径变化时更新月度估值；需要先加载财报时，等财报就绪以计算 PS，不等待财务图分红任务。股息率使用分红事件和共享不复权日价。
5. **手动刷新**：点击“刷新数据”在后台请求更新，保留已有图表和选择。普通打开不会按财报缓存日期自动刷新全部财报，查看新财报或新实施分红时可主动刷新。
6. **覆盖保护**：更新前检查原有报告期、非空财报字段、价格快照及估值月份等覆盖情况。接口报错或历史不完整时保留原缓存，并在页面显示原因。
7. **筹码与价格**：`/api/prices` 协调不复权和前复权历史；财务、估值、股东、融资图在同一轮异步加载中固定读取其前复权版本。前复权更新比较 20 个已有交易日与两个旧锚点，30 天到期或检测变化时全量核对；失败保留当前有效版本。图表默认前复权，可切换不复权；旧版本过期返回 409 供前端重新协调。
8. **写入与备份**：SQLite 业务事实与同步水位在同一事务提交，成功后刷新当日 Backup API 备份。仍使用 JSON 的财报和估值通过临时文件替换，写入前备份为同目录 `.json.bak`。

浏览器中的图表选择使用 `sessionStorage`，与磁盘财报／估值缓存无关。它保存当前标签页的指标、周期、时间范围和图形形式，切换股票或刷新页面时复用，不会因此向 Git 写入数据。

### SQLite 与后续迁移

`data/stock_analysis.sqlite3` 目前有以下 11 张应用表。记录数是 2026-09-30 对本机数据库的一次只读查询快照；打开其他股票或刷新页面后会变化。`sqlite_sequence` 等 SQLite 内部表不计入。

| 表 | 快照记录数 | 存储内容 |
|---|---:|---|
| `instruments` | 2 | 股票身份：交易所、六位代码、名称和创建时间；当前为 `600887`、`601919`，名称字段尚为空。 |
| `financing_daily` | 7,198 | 按股票、来源、交易日保存融资余额、融券余额、两融余额、融资净买入，以及来源原始字段、沿用字段的来源记录。 |
| `shareholder_observations` | 476 | 股东人数、统计日、公告日、来源和人数口径；旧缓存与在线来源各自留存，因此记录数不等于去重后的统计日期数。 |
| `raw_daily_prices` | 11,804 | 不复权日行情的开高低收、成交量、原始记录及获取来源。 |
| `adjusted_price_versions` | 2 | 前复权快照的版本身份、所属股票、覆盖日期、记录数、来源基准与校验状态；当前每只股票各有一个完成版本。 |
| `adjusted_daily_prices` | 11,806 | 按 `version_id` 关联前复权版本的每日收盘价及来源原始记录；同一交易日可属于不同版本。 |
| `price_version_leases` | 2 | 价格版本的读取租约及到期时间，使仍被页面引用的旧版本延迟清理。 |
| `sync_state` | 12 | 每只股票、数据集、来源和复权口径的成功水位、覆盖范围、最近检查时间及活动前复权版本。 |
| `sync_runs` | 31 | 每次同步的请求范围、时间、状态、记录数与错误摘要；快照中的运行均为成功。 |
| `legacy_imports` | 4 | 旧筹码 JSON 的文件路径、内容哈希、导入批次和记录数，用于防止重复导入。 |
| `schema_migrations` | 2 | 已应用的结构迁移编号、文件名、内容校验哈希及应用时间。 |

其中 `financing_daily`、`shareholder_observations`、`raw_daily_prices` 和 `adjusted_daily_prices` 保存业务事实或行情；其余表主要管理股票身份、版本、迁移和同步过程。

`quarterly_dashboard/storage.py` 管理连接、迁移、同步事务及在线备份，结构在 `001_initial.sql` 和 `002_shared_data.sql`。启动时获取数据库实例锁后建库、核验并恢复中断任务；失败运行的候选前复权版本一并清理。网络请求不占写事务。事实、水位和成功运行日志原子提交，修订号阻止旧任务覆盖新结果。

当前 Python 3.11.9 / SQLite 3.45.1 自动使用 DELETE journal；只有实际 SQLite 版本含已认可的 WAL-reset 修复时才允许 WAL。详细契约和维护命令见 [SQLite 存储说明](sqlite-storage.md)，来源观察见 [P0 数据源验证](data-sources/storage-upgrade-p0.md)。财务、分红、估值事实迁移及修复版 WAL 验收分别属于 P4、P5。
