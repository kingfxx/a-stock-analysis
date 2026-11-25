# 新窗口执行交接

状态：2026-10-24，研究实验已完成，正式接入方案已成型、尚未实施。用户准备换窗口继续执行。沿用本目录，不另建计划；未确认协作开发，不默认启动多代理。

2026-10-25衔接更新：统一采集管理已在 `../20261025_统一采集管理/` 实施，先读其TASK_CATALOG.md及当前collection_management.py/collection_schedule.py接口；本计划注册真实航运任务，不重建公共管理UI或调度。全部批量启动/停止/范围/定时只放后台维护→采集管理，预测页只保留导航。新航运定时默认关闭，既有任务授权和设置原样保留。新航运业务表使用research_shipping_*，具体迁移字段实施前定稿。

## 先读

- 根AGENTS.md，以及docs/agent-rules/collection.md、finance-and-reports.md、docs/chart-tooltip-style.md的适用章节。
- 本目录PLAN.md、DECISIONS.md、PROGRESS.md、PRODUCTION_DESIGN.md。后者是正式接入详细范围，本文件仅供定位。
- git status检查现有改动；当前计划目录和experiments/ccfi-profit-lab/尚未提交，不丢弃或覆盖已有成果。

## 已确定的用户选择

- 仅601919特化预测，复用原8765和日常数据库。入口在企业经营趋势“历史数据”表下、口径说明后、“说明”折叠前，文字“中远海控利润预测 →”；不加左侧导航或摘要卡。进入独立预测视图，可返回。
- 三情景、目标季度选择、可调整参数、保存方案/预测版本、数据与模型依据、历史回测及实际披露后复盘。页面读取不触发来源请求或模型。
- SSE周度CCFI综合+全部可见航线，上期/本期/真实日期/公布涨跌幅；SCFI当期周度同时积累，历史不足不进入主模型。CCFI页面当前12条航线+综合共13项目，一次请求解析多条事实。
- 交通运输部“出口集装箱运价指数”目录补官方月度综合和航线数据，含全年均值/YTD/月均及公布环比。HTML和图片均存在。目录最早2013年7月；2014—2025逐年12个月均有条目，2026到9月。仅目录完整已核对，尚未逐页逐值采集验收；旧年份有台湾/香港等额外航线，不能写死每月13行。
- 月度市场分析报告中的全国/沿海/内河吞吐量作为箱量候选依据，不等同公司承运量；先验证预测增益。2026年9月报告10月15日发布、引用8月数据，回测必须遵守可得时点。
- Excel周/月历史导入，同官方事实表保存，质量及来源区分；不覆盖原值。官方核验值可用于新版本计算，历史预测快照不改写。
- research_shipping_rate_points、research_shipping_report_metrics、research_shipping_forecast_versions为3张业务表；吞吐量先查可复用行业表，没有适用表再加第4张候选research_shipping_industry_metrics。不按航线/周月拆表。来源引用公共采集表，财务复用financial_reports，PDF及解析复用company_reports正式存储。
- 同一期Excel与官方保留各自证据，事实修订去重；全年/YTD参考在月报重复出现不机械重复新增。官方全年/YTD是平均值，但平均周值还是平均月均的具体年度口径尚未确认，不编造。
- 用户提出按周采集；尚未建立任何定时任务。先实现手动及增量闭环，定时开关与实际启用另明确处理。

## 资料与研究代码

- 实验入口：experiments/ccfi-profit-lab/index.html，模板template.html，生成build.py。
- 校准：calibrate.py、calibration.js；周度：weekly.py、weekly_view.js、weekly_section.html。
- 财报获取：capture_reports.py，复用公共RequestCollector。测试：test_model.py、verify.mjs。
- 输入Excel：E:/07_git/01_python/02_personal/xueqiu_get_comment/output/ccfi数据整理/CCFI_2023-2026_图中数据.xlsx（每周CCFI/月度CCFI/原图，无SCFI）。
- 参考PDF：d:/Users/HINATA/Downloads/Documents/601919_2026Q3_利润测算.pdf。
- 正式原件：data/company_reports/sh601919/2025-12-31/v1/report.pdf、2026-06-30/v1/report.pdf等。
- 14份报告及解析清单：data/verification/samples/ccfi-profit-lab/calibration_manifest.json；同目录有逐页解析、来源、周日期日历依据及原图。按清单实际路径和哈希复用，不重下所有报告。
- 数据与报告：data/verification/reports/ccfi-profit-lab/下snapshot.json、calibration.json、weekly.json、ELASTICITY_RESEARCH.md、WEEKLY_RESEARCH.md、VALIDATION.md。data目录被Git忽略，换机器需带上这些资料及原始采集归档，仅同工作区换聊天可直接读取。
- 日常库：data/stock_analysis.sqlite3。financial_reports.raw_json压缩，用financial_storage.decompress_json读取。营业成本用lrb.BIZCOST，gjzb.BIZTOTCOST是营业总成本，不能混用。

## 模型与未解决事项

- 原默认月度提前1月，国际单箱收入水平线性回归再以实际Q2锚定；收入/箱量先分别累计转单季后相除。14份报告、13季共同样本。等效弹性约0.6712，默认三情景136.5/171.9/200.1亿元，仅研究输入版本结果。
- 周度提前2周指整个季度向前平移14天，Q3窗口[6月17日,9月17日)，不是只用季度末两周。研究约173.1亿元，尚未替换月度默认；最优领先窗口随样本/日期误差变，不能宣称2周已稳定胜出。
- 非线性证据弱，高位样本少，不自动上线二次模型。SCFI、航线加权和吞吐量系数尚无验证。
- 官方2024年1月月均1191.91，与Excel1441冲突；Excel周均1191.75。2026年9月官方月值1887.59，Excel1879。新来源导入核对后必须重跑模型，不能沿用旧结果当作新数据测算。
- Excel周日期为发布日历辅助推定，原备注有冲突；不能冒称官方带日期历史。官方月均不能反推周值。历史“西非”和当前“东西非”不可直接合并。
- CompanyReportService.prepare/discover主要支持最新annual/interim，Q1/Q3需从公告元数据补齐准备能力，不假定已有完整季度自动获取。
- 曾通过8项Python测试及JS VM对账，但无正式页面浏览器验收。此前内置浏览器明确拒绝研究页file URL，不绕过其拒绝；正式页应按项目已有8765人工验收流程检查。

## 官方来源

- https://www.sse.net.cn/index/singleIndex?indexType=ccfi
- https://www.sse.net.cn/index/singleIndex?indexType=scfi （本轮访问曾超时，响应结构待核实）
- https://www.mot.gov.cn/shuju/yunjiazhishu/chukoujizhuangxiangyjzs/
- https://www.mot.gov.cn/shuju/yunjiazhishu/chukoujizhuangxiangyjzs/202512/t20251230_4192616.html （2024年1月HTML）
- https://www.mot.gov.cn/shuju/yunjiazhishu/chukoujizhuangxiangyjzs/202609/t20260903_4223763.html （2026年8月图片）
- https://www.mot.gov.cn/shuju/yunjiazhishu/chukoujizhuangxiangyjzs/202610/t20261015_4225991.html （9月市场分析，含8月吞吐量）


## 2026-10-25实施后状态

正式首版已接入原8765和日常库（v41），旧研究页保留。继续工作先读ACCEPTANCE.md与docs/shipping-forecast.md，勿重复迁入/重建模块。四个航运任务已注册，周/月定时仍关闭。官方月度仅3个月完成核验，其余保留Excel质量；本轮未声称全历史覆盖。计划与后续修复继续沿用本目录。
