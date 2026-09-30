# A 股季度分析页面设计文档

更新日期：2026-09-30。本文说明当前实现的数据源、代码职责、数据存储，以及加载、更新与备份流程。功能概览、页面截图、启动方式和指标口径见 [README](../README.md)。

## 当前数据源

以下是**当前运行代码实际调用**的数据源；不是候选接口列表。数据源地址和字段映射分别定义在 [sources.py](../quarterly_dashboard/sources.py) 与 [valuation.py](../quarterly_dashboard/valuation.py)。

| 来源 | 实际获取的数据 | 在页面中的用途 | 实现入口 |
|---|---|---|---|
| 新浪财报三表 | 利润表 `lrb`：累计营收、归母净利润、合并净利润、营业成本、利润总额、所得税费用、费用化利息及财务费用项下利息收入 | 收入／利润、同比、利润率、ROE、ROIC 的利润输入 | `sources.fetch_financial_report_page()`、`FundamentalService` |
| 新浪财报三表 | 资产负债表 `fzb`：报告期末股本、归母净资产、合并所有者权益、货币资金及债务组成科目 | 披露日估算市值、ROE、ROIC、有息负债与净现金 | 同上，与利润表按报告期合并 |
| 新浪财报三表 | 现金流量表 `llb`：累计经营活动现金流净额、购建长期资产支付的现金 | 经营现金流、CapEx、简化自由现金流 | 同上，独立同步 |
| 腾讯行情 | 股票名称；未复权、前复权日 K | 共享日价服务供财务、估值和筹码取价；不复权价格供市值与股息率计算 | `sources.fetch_stock_name()`、`fetch_price_history()`、`price_service.PriceService` |
| 百度股市通 | 历史 PE(TTM)、PB、总市值观察 | 日／周／月估值图；市值用于按观察日计算 PS(TTM) | `valuation.fetch_valuation_indicator()`、`ValuationService` |
| 东方财富分红数据 | 预案、取消和已实施事件的报告期、公告日、除权日、税前派息及总股本 | 财务图分红与估值股息率共用事件事实 | `valuation.fetch_dividend_event_page()`、`DividendService` |
| 东方财富同行估值 | 当前同行业 PE／PB／PS 均值、样本数量 | 当前同行对照，保存变更快照 | `valuation.fetch_industry_snapshot_details()`、`ValuationService` |
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
| [quarterly_dashboard/storage.py](../quarterly_dashboard/storage.py) | SQLite 连接、三版结构迁移、同步事务、实例锁、完整性检查和一致备份。 |
| [quarterly_dashboard/update_service.py](../quarterly_dashboard/update_service.py) | 融资及股东来源事实、旧筹码 JSON 导入、增量更新和失败保留。 |
| [quarterly_dashboard/price_service.py](../quarterly_dashboard/price_service.py) | 共享日价抓取、重叠/锚点核对、前复权版本发布、租约与清理。 |
| [quarterly_dashboard/fundamental_service.py](../quarterly_dashboard/fundamental_service.py) | 新浪三表独立窗口/全量同步、旧缓存与手工覆盖合并、原始事实读取。 |
| [quarterly_dashboard/dividend_service.py](../quarterly_dashboard/dividend_service.py) | 分红公告/除权窗口、未完成预案复查、事件身份与已实施现金查询。 |
| [quarterly_dashboard/valuation_service.py](../quarterly_dashboard/valuation_service.py) | 百度多窗口观察、行业快照、旧月度兼容快照与覆盖元数据。 |
| [quarterly_dashboard/price_projection.py](../quarterly_dashboard/price_projection.py) | 财务、估值和筹码图表的共享价格日期匹配。 |
| [quarterly_dashboard/chips.py](../quarterly_dashboard/chips.py) | 筹码数据适配、完整分页检查、股东人数价格快照匹配、融资一年窗口和历史覆盖保护。 |
| [quarterly_dashboard/network.py](../quarterly_dashboard/network.py) | 统一数据源 Session，默认直连，可通过 `DASHBOARD_USE_SYSTEM_PROXY=1` 启用系统／环境代理。 |
| [quarterly_dashboard/sources.py](../quarterly_dashboard/sources.py) | 新浪／腾讯适配层：请求数据、解析数值、统一字段名称和利息费用符号、校正历史披露日期、分段获取行情。 |
| [quarterly_dashboard/core.py](../quarterly_dashboard/core.py) | 财务纯计算：累计值拆季、连续四季度 TTM、年度视图、披露日快照、同比、利润率、ROE、债务／净现金、分红归属及年度分红率；调用 ROIC 纯函数。 |
| [quarterly_dashboard/roic.py](../quarterly_dashboard/roic.py) | ROIC 纯计算与输入检查，保留 `excess_cash`、`non_operating_adjustments` 两个扩展口；不独立取数、不单独启动任务。 |
| [quarterly_dashboard/valuation.py](../quarterly_dashboard/valuation.py) | 百度／东方财富适配与估值计算：原始观察、日／周／月聚合、PS、股息率、历史分位和同行对照。 |
| [web/index.html](../web/index.html) | 页面模板及前端 JS：股票搜索、A/B/C 选择、Plotly 图表、悬停详情、多轴零点对齐、后台加载状态及标签页选项保存。 |
| [tests/](../tests/) | Python 回归测试，覆盖解析、财务周期、ROIC、分红率、缓存保护、后台加载和估值。 |
| `tests/chart_hover.cjs`、`chart_axes.cjs`、`background_loading.cjs` | 用 Node.js 执行页面中的实际渲染／加载逻辑，检查悬停、零轴范围和异步更新；由 Python 测试调用。 |
| [docs/plans/](plans/) | 历史需求与实现方案；当前行为以代码、本设计文档和 [README](../README.md) 为准。 |
| [experiments/source_probe/](../experiments/source_probe/) | 独立数据源实验，不是生产页面的调用路径。 |
| [requirements.txt](../requirements.txt)、[.gitignore](../.gitignore)、[LICENSE](../LICENSE) | 运行依赖版本、本地数据忽略规则、项目许可证。 |

页面的数据流为：`来源适配 → SQLite 各模块事实与共享价格 → 计算与日期匹配 → 页面 JSON → Plotly 图表`。页面先读已保存事实，再在后台协调价格版本和过期模块的独立更新。财报字段映射在 `sources.py`，计算公式在 `core.py`／`roic.py`／`valuation.py`，图表交互在 `web/index.html`。

## 数据存储方式

P4 后，正常加载以 data/stock_analysis.sqlite3 为唯一业务事实库。data/fundamentals、data/valuation 和 data/chips 内的旧 JSON 原样保留，启动时按文件路径、内容哈希和数据集幂等导入；导入不会伪造旧缓存从未保存的新浪原始字段、分红预案身份或百度日度观察。旧月度估值单独保存在 legacy_valuation_snapshots，带旧口径版本。

主要事实表为 financial_reports（三类报表按报告期）、dividend_events（含预案状态与内部事件身份）、valuation_observations（PE/PB/总市值按真实观察日期）、industry_snapshots、report_overrides，以及 P2/P3 的融资、股东和共享价格表。sync_state 和 sync_runs 记录各股票、数据集、来源的独立水位与运行状态；事实与水位同事务提交。003_p4_facts.sql 增加这些表，并将 legacy_imports 的身份扩展为路径与数据集的组合。

财报首次分页取完整可用历史，普通更新每类最近 8 期；分红分别按公告日、除权日窗口及未完成预案复查；估值首次按十年、五年、三年三个窗口建库，普通更新各指标近一年。定期全量审计和长期离线扩窗在对应服务被请求时触发。来源失败或数据缺页时保留原事实，成功数据集独立提交。手工财报覆盖不调用外部来源。

页面初始 HTML 只读库；后台 /api/financial、/api/dividends、/api/valuation、/api/prices 和筹码接口按需更新。财报计算合并已存报表和手工覆盖；分红图与股息率共用已实施现金事件；PS 将每个市值观察日关联当时已披露的收入 TTM。估值日、周、月频率只聚合已存原始观察，切换频率本身不请求来源。股票切换与异步响应由前端防串图逻辑处理。

数据库置于本地磁盘。启动先取得实例锁、迁移、完整性检查、恢复中断运行，再做当日 Backup API 备份；成功业务提交后刷新日备份。备份恢复到新路径进行完整性检查，不直接覆盖活动库。数据文件和备份由 .gitignore 排除。维护命令、DELETE/WAL 运行时约束见 [SQLite 存储说明](sqlite-storage.md)。

2026-09-30 的 P4 集成在运行库的隔离备份副本上完成，未直接升级结构版本 2 的现有运行库。三只代表股票的冷暖请求、旧新估值差异及未完成的 P5 跨机/WAL 验收见 [P4/P5 验收记录](data-sources/p4-acceptance.md)。
