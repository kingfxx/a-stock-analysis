# A 股季度分析页面设计文档

更新日期：2026-09-29。本文说明当前实现的数据源、代码职责、数据存储，以及加载、更新与备份流程。功能概览、页面截图、启动方式和指标口径见 [README](../README.md)。

## 当前数据源

以下是**当前运行代码实际调用**的数据源；不是候选接口列表。数据源地址和字段映射分别定义在 [sources.py](../quarterly_dashboard/sources.py) 与 [valuation.py](../quarterly_dashboard/valuation.py)。

| 来源 | 实际获取的数据 | 在页面中的用途 | 实现入口 |
|---|---|---|---|
| 新浪财报三表 | 利润表 `lrb`：累计营收、归母净利润、合并净利润、营业成本、利润总额、所得税费用、费用化利息及财务费用项下利息收入 | 收入／利润、同比、利润率、ROE、ROIC 的利润输入 | `sources.fetch_financial_reports()` |
| 新浪财报三表 | 资产负债表 `fzb`：报告期末股本、归母净资产、合并所有者权益、货币资金及债务组成科目 | 披露日估算市值、ROE、ROIC、有息负债与净现金 | 同上，与利润表按报告期合并 |
| 新浪财报三表 | 现金流量表 `llb`：累计经营活动现金流净额、购建长期资产支付的现金 | 经营现金流、CapEx、简化自由现金流 | `sources.fetch_cash_flow_reports()` |
| 腾讯行情 | 股票名称；未复权、前复权日 K；前复权月 K | 缓存股票名称、财报披露日前股价快照、市值；估值图的股价叠加；股息率的价格分母 | `sources.fetch_stock_name()`、`fetch_daily_prices()`、`fetch_monthly_prices()` |
| 百度股市通 | 历史 PE(TTM)、PB、总市值序列 | 估值图的月度 PE／PB；总市值用于计算 PS(TTM) | `valuation.fetch_valuation_series()` |
| 东方财富分红数据 | 已实施分红的归属报告期、除权日、税前每股派息、数据源总股本 | 财务图的估算现金分红、年度悬停分红率，以及估值图近 12 个月股息率 | `valuation.fetch_dividend_events()`、`fetch_dividend_yields()` |
| 东方财富同行估值 | 当前同行业 PE／PB／PS 均值、样本数量 | 当前同行对照，只有当前快照，不是历史行业曲线 | `valuation.fetch_industry_snapshot()` |

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
| [quarterly_dashboard/server.py](../quarterly_dashboard/server.py) | 本地 HTTP 服务；生成缓存页面与三个后台接口；组织财报、分红、估值加载；缓存迁移、历史覆盖检查、备份及同股票写入协调。 |
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

页面的数据流为：`数据源适配 → 本地 JSON 缓存 → 财务／估值计算 → 页面 JSON → Plotly 图表`。财报字段映射的更改通常在 `sources.py`；计算公式的更改在 `core.py`／`roic.py`／`valuation.py`；图表显示与交互的更改在 `web/index.html`。

## 数据存储方式

### 文件布局

采用本地 UTF-8 JSON 文件，按六位股票代码分文件保存，当前没有数据库。路径相对于仓库根目录，首次需要写入时创建。

```text
data/
├── fundamentals/
│   ├── 300750.json             # 财报、名称、披露日价格快照、分红事件
│   └── 300750.json.bak         # 该文件上一次成功写入前的版本
├── valuation/
│   ├── 300750.json             # 已整理的月度估值及当前行业快照
│   └── 300750.json.bak
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

日 K 在获取过程中用于匹配披露日前的收盘价，但财务缓存只留下正式快照和参考点，不保存完整日线。财务金额在计算时使用元，前端显示“亿元”时再除以 `1e8`。

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

### 加载、更新与备份

1. **打开或切换股票**：根页面和“已缓存股票”列表只读本地文件，不等待外部网络；先显示已有数据。
2. **后台补齐**：首次查询或口径版本落后时，`/api/financial?code=<代码>` 加载／迁移财报和必要价格。旧财报字段补齐通常只重取相关报表，不重抓价格；披露日期或价格口径迁移需要重新匹配价格。
3. **分红**：`/api/dividends?code=<代码>` 独立补充已实施分红，写回财务缓存。已有有效分红缓存通常直接复用；缺少或空分红会重试。财务图无需等待它即可显示。
4. **估值**：`/api/valuation?code=<代码>` 在缺缓存、跨日或口径变化时更新月度估值；需要先加载财报时，等财报就绪以计算 PS，不等待财务图分红任务。估值任务会另外获取股息率所需的分红和价格数据。
5. **手动刷新**：点击“刷新数据”在后台请求更新，保留已有图表和选择。普通打开不会按财报缓存日期自动刷新全部财报，查看新财报或新实施分红时可主动刷新。
6. **覆盖保护**：更新前检查原有报告期、非空财报字段、价格快照及估值月份等覆盖情况。接口报错或历史不完整时保留原缓存，并在页面显示原因。
7. **写入与备份**：每次通过检查并覆盖已有 JSON 前，先把旧文件备份为同目录的 `.json.bak`，再用临时文件替换主文件。每份缓存只有最近一个备份，后续迁移、分红更新或刷新都可能覆盖它，不是完整版本历史；首次创建没有备份。

浏览器中的图表选择使用 `sessionStorage`，与磁盘财报／估值缓存无关。它保存当前标签页的指标、周期、时间范围和图形形式，切换股票或刷新页面时复用，不会因此向 Git 写入数据。
