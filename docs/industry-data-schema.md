# 全行业数据表结构与字段字典

导航：[财务共同规则](#2-四张财务表的共同规则) · [四表字段](#3-四张财务表的全部源字段) · [批次与来源](#4-批次分页来源与逻辑-view) · [行业及市值字段](#5-行业分类成员市值及旧表完整字段) · [追溯与备份](#7-原始响应追溯时间与备份) · [查询示例](#8-常用只读查询)

更新日期：2026-10-10。依据本项目迁移 009—022、字段映射及当前读取/采集代码整理。本文描述已落库字段和程序实际使用方式；来源辅助字段尚未确认的单位、公式和枚举明确标为待核对，不把字段名推测当作已验证定义。

## 1. 范围与数据流

当前财务来源层为四张 `market_financial_*` 表，行业分类、成员和市值仍使用 `sw_*` 表。旧 `sw_financial_facts` 及其来源表仅保留历史和维护工具用途，行业页面及日常采集已与其解耦。

| 对象 | 用途 | 来源/写入入口 |
| --- | --- | --- |
| `market_financial_income` | 利润表摘要全字段 | 东方财富 `RPT_DMSK_FN_INCOME` |
| `market_financial_balance` | 资产负债表摘要全字段 | 东方财富 `RPT_DMSK_FN_BALANCE` |
| `market_financial_cashflow` | 现金流量表摘要全字段 | 东方财富 `RPT_DMSK_FN_CASHFLOW` |
| `market_financial_performance` | 业绩指标全字段 | 东方财富 `RPT_LICO_FN_CPD` |
| `market_financial_batches` | 每个报告期的四表发布批次 | 本地采集程序 |
| `market_financial_sources` | 每个接口分页的请求及原始响应引用 | 本地采集程序 |
| `market_financial_industry` | 行业页面轻量读取 View，无独立存储 | 最新利润表左连接最新业绩指标 |
| `market_quarterly_prices` / `market_price_sources` / `market_price_batches` | 当前沪深名单回溯的季末未复权收盘价、共享来源及批次 | BigQuant `cn_stock_real_bar1d.close`、腾讯指数交易日历；独立一次性工具 |
| `sw_industries` / `sw_memberships` / `sw_membership_history` | 申万层级、沪深成员版本及分类变更 | 申万官方分类文件、沪深交易所名单 |
| `sw_imports` / `sw_membership_checks` / `sw_listing_sources` | 名单/市值导入审计、名单检查及上市日期证据 | 本地任务与对应来源 |
| `sw_cap_facts` / `sw_cap_provenance` | 公司总市值及共享证据 | 当前批量接口主要为东财 `RPT_VALUEANALYSIS_DET`，旧记录可含百度/腾讯 |
| `sw_cap_quarter_rosters` / `sw_cap_quarter_members` / `sw_industry_cap_quarters` | 冻结季度成员及三级行业市值汇总 | 本地冻结名单及市值聚合 |
| `sw_update_runs` | 财务、市值、历史补采任务状态 | 本地任务程序 |
| `sw_financial_facts` / `sw_financial_provenance` | 旧版轻量财务历史及共享证据 | 旧采集/回填工具；日常按钮已停写 |

四表采集包括全 A 股（沪、深、北），当前行业展示按沪深申万名单筛选。`columns=ALL` 指摘要接口的全部返回列，不代表正式财报全部明细。不同接口可以缺少不同公司，不保证四表公司逐一齐全。

关系：财务行 → `source_id` → 分页来源 → `batch_id` → 采集批次；行业 View → `sw_memberships` 按选定成员版本连接；市值独立通过季度冻结成员及来源引用读取。公司身份使用 `(security_code, exchange)`，没有外键关联个股 `instruments`。

## 2. 四张财务表的共同规则

金额以元保存，页面可换算亿元；每股指标为元/股，明确标注的比例为百分数（例如 12.3 代表 12.3%）。三表中未确认的比例/辅助字段不自动按百分数解释。利润与现金流通常为本年累计，资产负债表为期末余额；加权 ROE 使用所选报告期累计来源值，不随 TTM 选项改成 TTM ROE。

源字段值为 null 或空字符串时保持 NULL；接口整列缺失会被当前全字段校验拒绝发布；有效 0 和负值保留，非法或非有限数值拒绝发布。日期原始文本保留，报告期另行标准化；来源公告日不等于本地采集日，也不保证首次披露日。日期类型和报告类型代码目前未建立完整枚举映射，不能据此声称所有原始字段口径相同。

### 2.1 共同本地字段（四表均有）

| 字段 | SQLite 类型 | 含义/来源 | 约束 |
| --- | --- | --- | --- |
| `id` | INTEGER | 本地记录主键 | 主键 |
| `exchange` | TEXT | 由 SECUCODE 后缀规范化为 sh/sz/bj | 非空 |
| `report_date` | TEXT | 从 REPORT_DATE/REPORTDATE 提取 YYYY-MM-DD | 非空 |
| `version` | INTEGER | 同证券、交易所、报告期内的财务修订编号，从 1 开始 | 非空；>0 |
| `is_latest` | INTEGER | 1 为当前最新修订，0 为旧修订；不代表最新报告期 | 非空；0/1 |
| `batch_id` | INTEGER | 引用 market_financial_batches.id，本行最后发布批次 | 非空；外键 |
| `source_id` | INTEGER | 引用 market_financial_sources.id，追溯本行当前来源分页 | 非空；外键 |
| `content_hash` | TEXT | 规范化源字段 JSON 的 SHA256；不是原始文件哈希 | 非空 |
| `definition_version` | TEXT | 本地解析/定义版本：三表 eastmoney-summary-v1；业绩 eastmoney-performance-all-v1 | 非空 |
| `obtained_at` | TEXT | 插入该修订时来源页的采集时间；原地补全/核对不更新此列，当前页采集时间应看 source_id 对应来源 | 非空 |
| `updated_at` | TEXT | 本地插入、补全或核对时间，UTC ISO 8601 | 非空 |

每张财务表均有唯一约束 `(security_code, exchange, report_date, version)`；部分唯一索引限制 `is_latest=1` 时每公司每期仅一条，另有 `(report_date, is_latest)` 查询索引。源字段在 DDL 中均可空，证券身份与报告期由采集校验保证有效。

### 2.2 更新与历史版本

四个接口完整校验后在同一事务中发布；失败不发布该期新财务行。真实金额、公告日期或定义修订新增版本，并将旧版本置 `is_latest=0`；仅 NULL 补齐为有效值直接补当前行，描述性字段变化及无变化核对也原地更新。原地更新会更换 `source_id`、`batch_id`、哈希和 `updated_at`，不会留下旧行完整镜像；原始响应页仍可追溯。因此这里的“版本”不是每次采集快照。

最新版本按每公司每报告期判断；未在本次响应出现的旧公司记录不自动删除。批次完成不表示每家公司在该批次都返回过；必要时检查行的 `batch_id` 与结果中的缺报统计。

## 3. 四张财务表的全部源字段

以下每行给出落库列、接口字段、类型、含义和单位/口径。字段来源为该节指定的接口；所有原始源字段映射均见 [`market_financial_fields.json`](../quarterly_dashboard/resources/market_financial_fields.json)。字段数不含上面的 11 个本地字段。

### 3.1 `market_financial_income` — 利润表

来源接口：`RPT_DMSK_FN_INCOME`；46 个源字段。

| 落库字段 | 接口字段 | 类型 | 含义 | 单位/口径 |
| --- | --- | --- | --- | --- |
| `secucode` | `SECUCODE` | TEXT | 证券完整代码（六位代码加 .SH/.SZ/.BJ） | 原始文本/代码/日期；不做枚举转换 |
| `security_code` | `SECURITY_CODE` | TEXT | 六位证券代码，文本保留前导零 | 原始文本/代码/日期；不做枚举转换 |
| `industry_code` | `INDUSTRY_CODE` | TEXT | 来源行业代码；不是申万三级代码的替代品 | 原始文本/代码/日期；不做枚举转换 |
| `org_code` | `ORG_CODE` | TEXT | 来源机构代码 | 原始文本/代码/日期；不做枚举转换 |
| `security_name_abbr` | `SECURITY_NAME_ABBR` | TEXT | 证券简称 | 原始文本/代码/日期；不做枚举转换 |
| `industry_name` | `INDUSTRY_NAME` | TEXT | 来源行业名称；不覆盖申万分类 | 原始文本/代码/日期；不做枚举转换 |
| `market` | `MARKET` | TEXT | 来源市场标识 | 原始文本/代码/日期；不做枚举转换 |
| `security_type_code` | `SECURITY_TYPE_CODE` | TEXT | 证券类型代码；采集筛选 A 股 058001001 | 原始文本/代码/日期；不做枚举转换 |
| `trade_market_code` | `TRADE_MARKET_CODE` | TEXT | 交易市场代码 | 原始文本/代码/日期；不做枚举转换 |
| `date_type_code` | `DATE_TYPE_CODE` | TEXT | 来源日期类型代码；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `report_type_code` | `REPORT_TYPE_CODE` | TEXT | 来源报告类型代码；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `data_state` | `DATA_STATE` | TEXT | 来源数据状态；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `notice_date` | `NOTICE_DATE` | TEXT | 来源公告日期；不保证是首次披露日期 | 原始文本/代码/日期；不做枚举转换 |
| `source_report_date` | `REPORT_DATE` | TEXT | 来源报告期原始文本 | 原始文本/代码/日期；不做枚举转换 |
| `parent_netprofit` | `PARENT_NETPROFIT` | REAL | 归属于母公司股东的净利润 | 元；本年累计 |
| `total_operate_income` | `TOTAL_OPERATE_INCOME` | REAL | 营业总收入 | 元；本年累计 |
| `total_operate_cost` | `TOTAL_OPERATE_COST` | REAL | 营业总成本 | 元；本年累计 |
| `toe_ratio` | `TOE_RATIO` | REAL | 营业总成本相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `operate_cost` | `OPERATE_COST` | REAL | 营业成本 | 元；本年累计 |
| `operate_expense` | `OPERATE_EXPENSE` | REAL | 来源营业支出科目；与营业总成本的适用关系待核对 | 元；本年累计 |
| `operate_expense_ratio` | `OPERATE_EXPENSE_RATIO` | REAL | 来源营业支出科目；与营业总成本的适用关系待核对相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `sale_expense` | `SALE_EXPENSE` | REAL | 销售费用 | 元；本年累计 |
| `manage_expense` | `MANAGE_EXPENSE` | REAL | 管理费用 | 元；本年累计 |
| `finance_expense` | `FINANCE_EXPENSE` | REAL | 财务费用 | 元；本年累计 |
| `operate_profit` | `OPERATE_PROFIT` | REAL | 营业利润 | 元；本年累计 |
| `total_profit` | `TOTAL_PROFIT` | REAL | 利润总额 | 元；本年累计 |
| `income_tax` | `INCOME_TAX` | REAL | 所得税费用 | 元；本年累计 |
| `operate_income` | `OPERATE_INCOME` | REAL | 营业收入；金融公司适用性需另核对 | 元；本年累计 |
| `interest_ni` | `INTEREST_NI` | REAL | 利息净收入（金融科目） | 元；本年累计 |
| `interest_ni_ratio` | `INTEREST_NI_RATIO` | REAL | 利息净收入（金融科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `fee_commission_ni` | `FEE_COMMISSION_NI` | REAL | 手续费及佣金净收入（金融科目） | 元；本年累计 |
| `fcn_ratio` | `FCN_RATIO` | REAL | 手续费及佣金净收入（金融科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `operate_tax_add` | `OPERATE_TAX_ADD` | REAL | 税金及附加 | 元；本年累计 |
| `manage_expense_bank` | `MANAGE_EXPENSE_BANK` | REAL | 业务及管理费（银行科目） | 元；本年累计 |
| `fcn_calculate` | `FCN_CALCULATE` | REAL | 手续费及佣金相关辅助计算字段；公式与单位未确认 | 待核对；保留原值 |
| `interest_ni_calculate` | `INTEREST_NI_CALCULATE` | REAL | 利息净收入相关辅助计算字段；公式与单位未确认 | 待核对；保留原值 |
| `earned_premium` | `EARNED_PREMIUM` | REAL | 已赚保费（保险科目） | 元；本年累计 |
| `earned_premium_ratio` | `EARNED_PREMIUM_RATIO` | REAL | 已赚保费（保险科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `invest_income` | `INVEST_INCOME` | REAL | 投资收益 | 元；本年累计 |
| `surrender_value` | `SURRENDER_VALUE` | REAL | 退保金（保险科目） | 元；本年累计 |
| `compensate_expense` | `COMPENSATE_EXPENSE` | REAL | 赔付支出（保险科目） | 元；本年累计 |
| `toi_ratio` | `TOI_RATIO` | REAL | 营业总收入相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `operate_profit_ratio` | `OPERATE_PROFIT_RATIO` | REAL | 营业利润相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `parent_netprofit_ratio` | `PARENT_NETPROFIT_RATIO` | REAL | 归属于母公司股东的净利润相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `deduct_parent_netprofit` | `DEDUCT_PARENT_NETPROFIT` | REAL | 扣除非经常性损益后的归母净利润 | 元；本年累计 |
| `dpn_ratio` | `DPN_RATIO` | REAL | 扣除非经常性损益后的归母净利润相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |

### 3.2 `market_financial_balance` — 资产负债表

来源接口：`RPT_DMSK_FN_BALANCE`；57 个源字段。

| 落库字段 | 接口字段 | 类型 | 含义 | 单位/口径 |
| --- | --- | --- | --- | --- |
| `secucode` | `SECUCODE` | TEXT | 证券完整代码（六位代码加 .SH/.SZ/.BJ） | 原始文本/代码/日期；不做枚举转换 |
| `security_code` | `SECURITY_CODE` | TEXT | 六位证券代码，文本保留前导零 | 原始文本/代码/日期；不做枚举转换 |
| `industry_code` | `INDUSTRY_CODE` | TEXT | 来源行业代码；不是申万三级代码的替代品 | 原始文本/代码/日期；不做枚举转换 |
| `org_code` | `ORG_CODE` | TEXT | 来源机构代码 | 原始文本/代码/日期；不做枚举转换 |
| `security_name_abbr` | `SECURITY_NAME_ABBR` | TEXT | 证券简称 | 原始文本/代码/日期；不做枚举转换 |
| `industry_name` | `INDUSTRY_NAME` | TEXT | 来源行业名称；不覆盖申万分类 | 原始文本/代码/日期；不做枚举转换 |
| `market` | `MARKET` | TEXT | 来源市场标识 | 原始文本/代码/日期；不做枚举转换 |
| `security_type_code` | `SECURITY_TYPE_CODE` | TEXT | 证券类型代码；采集筛选 A 股 058001001 | 原始文本/代码/日期；不做枚举转换 |
| `trade_market_code` | `TRADE_MARKET_CODE` | TEXT | 交易市场代码 | 原始文本/代码/日期；不做枚举转换 |
| `date_type_code` | `DATE_TYPE_CODE` | TEXT | 来源日期类型代码；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `report_type_code` | `REPORT_TYPE_CODE` | TEXT | 来源报告类型代码；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `data_state` | `DATA_STATE` | TEXT | 来源数据状态；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `notice_date` | `NOTICE_DATE` | TEXT | 来源公告日期；不保证是首次披露日期 | 原始文本/代码/日期；不做枚举转换 |
| `source_report_date` | `REPORT_DATE` | TEXT | 来源报告期原始文本 | 原始文本/代码/日期；不做枚举转换 |
| `total_assets` | `TOTAL_ASSETS` | REAL | 资产总额，报告期末余额 | 元；期末余额 |
| `fixed_asset` | `FIXED_ASSET` | REAL | 固定资产 | 元；期末余额 |
| `monetaryfunds` | `MONETARYFUNDS` | REAL | 货币资金 | 元；期末余额 |
| `monetaryfunds_ratio` | `MONETARYFUNDS_RATIO` | REAL | 货币资金相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `accounts_rece` | `ACCOUNTS_RECE` | REAL | 应收账款 | 元；期末余额 |
| `accounts_rece_ratio` | `ACCOUNTS_RECE_RATIO` | REAL | 应收账款相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `inventory` | `INVENTORY` | REAL | 存货 | 元；期末余额 |
| `inventory_ratio` | `INVENTORY_RATIO` | REAL | 存货相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `total_liabilities` | `TOTAL_LIABILITIES` | REAL | 负债总额 | 元；期末余额 |
| `accounts_payable` | `ACCOUNTS_PAYABLE` | REAL | 应付账款 | 元；期末余额 |
| `accounts_payable_ratio` | `ACCOUNTS_PAYABLE_RATIO` | REAL | 应付账款相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `advance_receivables` | `ADVANCE_RECEIVABLES` | REAL | 预收款项 | 元；期末余额 |
| `advance_receivables_ratio` | `ADVANCE_RECEIVABLES_RATIO` | REAL | 预收款项相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `total_equity` | `TOTAL_EQUITY` | REAL | 所有者权益合计；不能直接当作归母权益 | 元；期末余额 |
| `total_equity_ratio` | `TOTAL_EQUITY_RATIO` | REAL | 所有者权益合计；不能直接当作归母权益相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `total_assets_ratio` | `TOTAL_ASSETS_RATIO` | REAL | 资产总额，报告期末余额相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `total_liab_ratio` | `TOTAL_LIAB_RATIO` | REAL | 负债总额相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `current_ratio` | `CURRENT_RATIO` | REAL | 流动比率；来源倍率/展示单位待核对 | 待核对；保留原值 |
| `debt_asset_ratio` | `DEBT_ASSET_RATIO` | REAL | 资产负债率；来源比例单位待核对 | 待核对；保留原值 |
| `cash_deposit_pbc` | `CASH_DEPOSIT_PBC` | REAL | 现金及存放中央银行款项（银行科目） | 元；期末余额 |
| `cdp_ratio` | `CDP_RATIO` | REAL | 现金及存放中央银行款项（银行科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `loan_advance` | `LOAN_ADVANCE` | REAL | 发放贷款及垫款（金融科目） | 元；期末余额 |
| `loan_advance_ratio` | `LOAN_ADVANCE_RATIO` | REAL | 发放贷款及垫款（金融科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `available_sale_finasset` | `AVAILABLE_SALE_FINASSET` | REAL | 可供出售金融资产（历史会计科目） | 元；期末余额 |
| `asf_ratio` | `ASF_RATIO` | REAL | 可供出售金融资产（历史会计科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `loan_pbc` | `LOAN_PBC` | REAL | 向中央银行借款（银行科目） | 元；期末余额 |
| `loan_pbc_ratio` | `LOAN_PBC_RATIO` | REAL | 向中央银行借款（银行科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `accept_deposit` | `ACCEPT_DEPOSIT` | REAL | 吸收存款（银行科目） | 元；期末余额 |
| `accept_deposit_ratio` | `ACCEPT_DEPOSIT_RATIO` | REAL | 吸收存款（银行科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `sell_repo_finasset` | `SELL_REPO_FINASSET` | REAL | 卖出回购金融资产款（金融科目） | 元；期末余额 |
| `srf_ratio` | `SRF_RATIO` | REAL | 卖出回购金融资产款（金融科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `settle_excess_reserve` | `SETTLE_EXCESS_RESERVE` | REAL | 结算备付金（证券科目） | 元；期末余额 |
| `ser_ratio` | `SER_RATIO` | REAL | 结算备付金（证券科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `borrow_fund` | `BORROW_FUND` | REAL | 拆入资金（金融科目） | 元；期末余额 |
| `borrow_fund_ratio` | `BORROW_FUND_RATIO` | REAL | 拆入资金（金融科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `agent_trade_security` | `AGENT_TRADE_SECURITY` | REAL | 代理买卖证券款（证券科目） | 元；期末余额 |
| `ats_ratio` | `ATS_RATIO` | REAL | 代理买卖证券款（证券科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `premium_rece` | `PREMIUM_RECE` | REAL | 应收保费（保险科目） | 元；期末余额 |
| `premium_rece_ratio` | `PREMIUM_RECE_RATIO` | REAL | 应收保费（保险科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `short_loan` | `SHORT_LOAN` | REAL | 短期借款 | 元；期末余额 |
| `short_loan_ratio` | `SHORT_LOAN_RATIO` | REAL | 短期借款相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `advance_premium` | `ADVANCE_PREMIUM` | REAL | 预收保费（保险科目） | 元；期末余额 |
| `advance_premium_ratio` | `ADVANCE_PREMIUM_RATIO` | REAL | 预收保费（保险科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |

### 3.3 `market_financial_cashflow` — 现金流量表

来源接口：`RPT_DMSK_FN_CASHFLOW`；48 个源字段。

| 落库字段 | 接口字段 | 类型 | 含义 | 单位/口径 |
| --- | --- | --- | --- | --- |
| `secucode` | `SECUCODE` | TEXT | 证券完整代码（六位代码加 .SH/.SZ/.BJ） | 原始文本/代码/日期；不做枚举转换 |
| `security_code` | `SECURITY_CODE` | TEXT | 六位证券代码，文本保留前导零 | 原始文本/代码/日期；不做枚举转换 |
| `industry_code` | `INDUSTRY_CODE` | TEXT | 来源行业代码；不是申万三级代码的替代品 | 原始文本/代码/日期；不做枚举转换 |
| `org_code` | `ORG_CODE` | TEXT | 来源机构代码 | 原始文本/代码/日期；不做枚举转换 |
| `security_name_abbr` | `SECURITY_NAME_ABBR` | TEXT | 证券简称 | 原始文本/代码/日期；不做枚举转换 |
| `industry_name` | `INDUSTRY_NAME` | TEXT | 来源行业名称；不覆盖申万分类 | 原始文本/代码/日期；不做枚举转换 |
| `market` | `MARKET` | TEXT | 来源市场标识 | 原始文本/代码/日期；不做枚举转换 |
| `security_type_code` | `SECURITY_TYPE_CODE` | TEXT | 证券类型代码；采集筛选 A 股 058001001 | 原始文本/代码/日期；不做枚举转换 |
| `trade_market_code` | `TRADE_MARKET_CODE` | TEXT | 交易市场代码 | 原始文本/代码/日期；不做枚举转换 |
| `date_type_code` | `DATE_TYPE_CODE` | TEXT | 来源日期类型代码；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `report_type_code` | `REPORT_TYPE_CODE` | TEXT | 来源报告类型代码；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `data_state` | `DATA_STATE` | TEXT | 来源数据状态；枚举定义未登记 | 原始文本/代码/日期；不做枚举转换 |
| `notice_date` | `NOTICE_DATE` | TEXT | 来源公告日期；不保证是首次披露日期 | 原始文本/代码/日期；不做枚举转换 |
| `source_report_date` | `REPORT_DATE` | TEXT | 来源报告期原始文本 | 原始文本/代码/日期；不做枚举转换 |
| `netcash_operate` | `NETCASH_OPERATE` | REAL | 经营活动产生的现金流量净额 | 元；期初/期末余额除外，其余按累计流量 |
| `netcash_operate_ratio` | `NETCASH_OPERATE_RATIO` | REAL | 经营活动产生的现金流量净额相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `sales_services` | `SALES_SERVICES` | REAL | 销售商品、提供劳务收到的现金 | 元；期初/期末余额除外，其余按累计流量 |
| `sales_services_ratio` | `SALES_SERVICES_RATIO` | REAL | 销售商品、提供劳务收到的现金相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `pay_staff_cash` | `PAY_STAFF_CASH` | REAL | 支付给职工以及为职工支付的现金 | 元；期初/期末余额除外，其余按累计流量 |
| `psc_ratio` | `PSC_RATIO` | REAL | 支付给职工以及为职工支付的现金相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `netcash_invest` | `NETCASH_INVEST` | REAL | 投资活动产生的现金流量净额 | 元；期初/期末余额除外，其余按累计流量 |
| `netcash_invest_ratio` | `NETCASH_INVEST_RATIO` | REAL | 投资活动产生的现金流量净额相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `receive_invest_income` | `RECEIVE_INVEST_INCOME` | REAL | 取得投资收益收到的现金 | 元；期初/期末余额除外，其余按累计流量 |
| `rii_ratio` | `RII_RATIO` | REAL | 取得投资收益收到的现金相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `construct_long_asset` | `CONSTRUCT_LONG_ASSET` | REAL | 购建固定资产、无形资产和其他长期资产支付的现金（本项目 Capex 对应科目） | 元；期初/期末余额除外，其余按累计流量 |
| `cla_ratio` | `CLA_RATIO` | REAL | 购建固定资产、无形资产和其他长期资产支付的现金（本项目 Capex 对应科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `netcash_finance` | `NETCASH_FINANCE` | REAL | 筹资活动产生的现金流量净额 | 元；期初/期末余额除外，其余按累计流量 |
| `netcash_finance_ratio` | `NETCASH_FINANCE_RATIO` | REAL | 筹资活动产生的现金流量净额相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `cce_add` | `CCE_ADD` | REAL | 现金及现金等价物净增加额 | 元；期初/期末余额除外，其余按累计流量 |
| `cce_add_ratio` | `CCE_ADD_RATIO` | REAL | 现金及现金等价物净增加额相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `customer_deposit_add` | `CUSTOMER_DEPOSIT_ADD` | REAL | 客户存款和同业存放款项净增加额（金融科目） | 元；期初/期末余额除外，其余按累计流量 |
| `cda_ratio` | `CDA_RATIO` | REAL | 客户存款和同业存放款项净增加额（金融科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `deposit_iofi_other` | `DEPOSIT_IOFI_OTHER` | REAL | 存放中央银行和同业款项相关现金流科目；净增减方向待核对 | 元；期初/期末余额除外，其余按累计流量 |
| `dio_ratio` | `DIO_RATIO` | REAL | 存放中央银行和同业款项相关现金流科目；净增减方向待核对相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `loan_advance_add` | `LOAN_ADVANCE_ADD` | REAL | 客户贷款及垫款相关净增加额（金融科目）；现金流方向待核对 | 元；期初/期末余额除外，其余按累计流量 |
| `laa_ratio` | `LAA_RATIO` | REAL | 客户贷款及垫款相关净增加额（金融科目）；现金流方向待核对相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `receive_interest_commission` | `RECEIVE_INTEREST_COMMISSION` | REAL | 收取利息、手续费及佣金的现金（金融科目） | 元；期初/期末余额除外，其余按累计流量 |
| `ric_ratio` | `RIC_RATIO` | REAL | 收取利息、手续费及佣金的现金（金融科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `invest_pay_cash` | `INVEST_PAY_CASH` | REAL | 投资支付的现金 | 元；期初/期末余额除外，其余按累计流量 |
| `ipc_ratio` | `IPC_RATIO` | REAL | 投资支付的现金相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `begin_cce` | `BEGIN_CCE` | REAL | 期初现金及现金等价物余额 | 元；期初/期末余额除外，其余按累计流量 |
| `begin_cce_ratio` | `BEGIN_CCE_RATIO` | REAL | 期初现金及现金等价物余额相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `end_cce` | `END_CCE` | REAL | 期末现金及现金等价物余额 | 元；期初/期末余额除外，其余按累计流量 |
| `end_cce_ratio` | `END_CCE_RATIO` | REAL | 期末现金及现金等价物余额相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `receive_origic_premium` | `RECEIVE_ORIGIC_PREMIUM` | REAL | 收到原保险合同保费取得的现金（保险科目） | 元；期初/期末余额除外，其余按累计流量 |
| `rop_ratio` | `ROP_RATIO` | REAL | 收到原保险合同保费取得的现金（保险科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |
| `pay_origic_compensate` | `PAY_ORIGIC_COMPENSATE` | REAL | 支付原保险合同赔付款项的现金（保险科目） | 元；期初/期末余额除外，其余按累计流量 |
| `poc_ratio` | `POC_RATIO` | REAL | 支付原保险合同赔付款项的现金（保险科目）相关比例/变动辅助字段；公式未确认，不用于当前页面计算 | 待核对；保留原值 |

### 3.4 `market_financial_performance` — 业绩指标

来源接口：`RPT_LICO_FN_CPD`；37 个源字段。

| 落库字段 | 接口字段 | 类型 | 含义 | 单位/口径 |
| --- | --- | --- | --- | --- |
| `security_code` | `SECURITY_CODE` | TEXT | 六位证券代码，文本保留前导零 | 原始文本/代码/日期；不做枚举转换 |
| `security_name_abbr` | `SECURITY_NAME_ABBR` | TEXT | 证券简称 | 原始文本/代码/日期；不做枚举转换 |
| `trade_market_code` | `TRADE_MARKET_CODE` | TEXT | 交易市场代码 | 原始文本/代码/日期；不做枚举转换 |
| `trade_market` | `TRADE_MARKET` | TEXT | 交易市场名称 | 原始文本/代码/日期；不做枚举转换 |
| `security_type_code` | `SECURITY_TYPE_CODE` | TEXT | 证券类型代码；采集筛选 A 股 058001001 | 原始文本/代码/日期；不做枚举转换 |
| `security_type` | `SECURITY_TYPE` | TEXT | 证券类型名称 | 原始文本/代码/日期；不做枚举转换 |
| `update_date` | `UPDATE_DATE` | TEXT | 来源数据更新日期；不是本地采集时间 | 原始文本/代码/日期；不做枚举转换 |
| `source_report_date` | `REPORTDATE` | TEXT | 来源报告期原始文本 | 原始文本/代码/日期；不做枚举转换 |
| `basic_eps` | `BASIC_EPS` | REAL | 基本每股收益 | 元/股 |
| `deduct_basic_eps` | `DEDUCT_BASIC_EPS` | REAL | 扣非基本每股收益 | 元/股 |
| `total_operate_income` | `TOTAL_OPERATE_INCOME` | REAL | 营业总收入 | 元 |
| `parent_netprofit` | `PARENT_NETPROFIT` | REAL | 归属于母公司股东的净利润 | 元 |
| `weightavg_roe` | `WEIGHTAVG_ROE` | REAL | 加权平均净资产收益率；直接取数，不是期末权益估算值 | % |
| `ystz` | `YSTZ` | REAL | 来源营业总收入同比增长率 | % |
| `sjltz` | `SJLTZ` | REAL | 来源归母净利润同比增长率 | % |
| `bps` | `BPS` | REAL | 每股净资产 | 元/股 |
| `mgjyxjje` | `MGJYXJJE` | REAL | 每股经营现金流量 | 元/股 |
| `xsmll` | `XSMLL` | REAL | 来源销售毛利率 | % |
| `yshz` | `YSHZ` | REAL | 来源营业收入季度环比增长字段；具体季度口径待核对 | % |
| `sjlhz` | `SJLHZ` | REAL | 来源净利润季度环比增长字段；具体季度口径待核对 | % |
| `assigndscrpt` | `ASSIGNDSCRPT` | TEXT | 利润分配方案描述 | 原始文本/代码/日期；不做枚举转换 |
| `payyear` | `PAYYEAR` | TEXT | 分配相关年度标识；来源文本 | 原始文本/代码/日期；不做枚举转换 |
| `publishname` | `PUBLISHNAME` | TEXT | 来源行业/发布分类名称；不替代申万分类 | 原始文本/代码/日期；不做枚举转换 |
| `zxgxl` | `ZXGXL` | REAL | 来源股息率；价格基准、分红范围及观察时点待核对 | % |
| `notice_date` | `NOTICE_DATE` | TEXT | 来源公告日期；不保证是首次披露日期 | 原始文本/代码/日期；不做枚举转换 |
| `org_code` | `ORG_CODE` | TEXT | 来源机构代码 | 原始文本/代码/日期；不做枚举转换 |
| `trade_market_zjg` | `TRADE_MARKET_ZJG` | TEXT | 来源市场辅助标识；枚举未登记 | 原始文本/代码/日期；不做枚举转换 |
| `isnew` | `ISNEW` | TEXT | 来源新记录标识；枚举未登记 | 原始文本/代码/日期；不做枚举转换 |
| `qdate` | `QDATE` | TEXT | 来源季度标签 | 原始文本/代码/日期；不做枚举转换 |
| `datatype` | `DATATYPE` | TEXT | 来源数据类型标识；枚举未登记 | 原始文本/代码/日期；不做枚举转换 |
| `datayear` | `DATAYEAR` | TEXT | 来源年度标签 | 原始文本/代码/日期；不做枚举转换 |
| `datemmdd` | `DATEMMDD` | TEXT | 来源月日标签 | 原始文本/代码/日期；不做枚举转换 |
| `eitime` | `EITIME` | TEXT | 来源系统时间字段；业务语义未确认，不作本地采集时间 | 原始文本/代码/日期；不做枚举转换 |
| `secucode` | `SECUCODE` | TEXT | 证券完整代码（六位代码加 .SH/.SZ/.BJ） | 原始文本/代码/日期；不做枚举转换 |
| `board_name` | `BOARD_NAME` | TEXT | 来源板块名称 | 原始文本/代码/日期；不做枚举转换 |
| `ori_board_code` | `ORI_BOARD_CODE` | TEXT | 来源原板块代码 | 原始文本/代码/日期；不做枚举转换 |
| `board_code` | `BOARD_CODE` | TEXT | 来源板块代码 | 原始文本/代码/日期；不做枚举转换 |

## 4. 批次、分页来源与逻辑 View

财务请求地址由 `industry_sources.EM` 配置为 `https://datacenter-web.eastmoney.com/api/data/v1/get`。参数包含 `reportName`、`columns=ALL`、报告期及 A 股类型过滤、`pageSize=500`、`pageNumber`、证券代码升序、`source=WEB`、`client=WEB`。三表按 `REPORT_DATE` 筛选，业绩指标按 `REPORTDATE` 筛选。准确参数以 `params_json` 为准；本段是程序配置说明，不代表重新联网验证接口协议。

### `market_financial_batches`

每报告期一次四表发布；`result_json` 各数据集记录 rows/new/revised/enriched/unchanged/raw_bytes/stored_bytes/missing_previous 等计数。失败批次保留审计；成功分页来源与事实行一起发布。

| 字段 | 类型 | 含义 | 可空/键 |
| --- | --- | --- | --- |
| `id` | INTEGER | 本地自增整数主键 | 主键序号 1 |
| `report_date` | TEXT | 标准报告期 YYYY-MM-DD | 非空 |
| `started_at` | TEXT | 任务/批次开始时间 | 非空 |
| `finished_at` | TEXT | 结束时间；运行中可空 | 可空 |
| `status` | TEXT | 状态，财务批次/行业任务/检查为 running、complete、failed；sw_imports 仅 complete | 非空 |
| `result_json` | TEXT | 本地执行结果及计数 JSON；以对应任务写入结构为准 | 可空 |
| `error` | TEXT | 失败原因；非失败可空 | 可空 |
| `obtained_at` | TEXT | 采集/导入时间；不是报告期 | 非空 |

批次查询索引：`(report_date, id)`。同一期允许多次批次，不按 report_date 唯一。

### `market_financial_sources`

分页来源唯一约束 `(batch_id,dataset,page_number)`。`content_hash` 是解压后原始 HTTP 响应的 SHA256，区别于财务行规范化内容哈希；不同来源记录可以引用同一内容文件。

| 字段 | 类型 | 含义 | 可空/键 |
| --- | --- | --- | --- |
| `id` | INTEGER | 本地自增整数主键 | 主键序号 1 |
| `batch_id` | INTEGER | 引用 market_financial_batches.id | 非空 |
| `dataset` | TEXT | income / balance / cashflow / performance | 非空 |
| `page_number` | INTEGER | 接口分页编号，从 1 开始 | 非空 |
| `url` | TEXT | 实际请求地址 | 非空 |
| `params_json` | TEXT | 实际请求参数 JSON | 非空 |
| `file_path` | TEXT | 相对 market_financial_sources 根目录的 gzip 原始响应路径 | 非空 |
| `content_hash` | TEXT | 来源/清单内容 SHA256；具体哈希对象见本节说明 | 非空 |
| `raw_bytes` | INTEGER | 压缩前原始响应字节数 | 非空 |
| `stored_bytes` | INTEGER | gzip 文件字节数 | 非空 |
| `obtained_at` | TEXT | 采集/导入时间；不是报告期 | 非空 |
| `fields_json` | TEXT | 该分页保存的接口字段代码列表 JSON | 非空 |

外键：`market_financial_sources.batch_id` → `market_financial_batches.id`。`page_number` 正整数和 dataset 合法范围由采集程序校验，DDL 没有对应 CHECK。

### `market_financial_industry`（逻辑 View）

不存储独立数据、没有独立采集时间，不做季度/TTM/行业汇总。以最新利润表为主表，同公司、交易所、报告期左连接最新业绩指标，缺 ROE 不影响营收和利润。资产负债表、现金流量表不参与此 View。

| 字段 | 来源 | 含义/单位 |
| --- | --- | --- |
| `security_code` | income.security_code | 六位证券代码 |
| `exchange` | income.exchange | sh/sz/bj |
| `report_date` | income.report_date | 标准报告期 |
| `revenue` | income.total_operate_income | 营业总收入，元，本年累计 |
| `parent_profit` | income.parent_netprofit | 归母净利润，元，本年累计 |
| `operating_revenue` | income.operate_income | 营业收入，元，本年累计 |
| `operating_cost` | income.operate_cost | 营业成本，元，本年累计 |
| `weighted_roe` | performance.weightavg_roe | 所选报告期累计加权 ROE，%，允许空 |
| `bps` | performance.bps | 所选报告期每股净资产，元/股，允许空；PB 分母 |
| `income_source_id` | income.source_id | 利润表分页来源引用 |
| `performance_source_id` | performance.source_id | 业绩指标分页来源引用，允许空 |
| `updated_at` | 两行 updated_at 的较新值 | 本地更新/核对时间；只有利润表时取利润表时间 |

## 5. 行业分类、成员、市值及旧表完整字段

以下类型、主键、外键和索引来自当前迁移后的 SQLite 结构。复合主键的序号表示字段顺序；部分老表 DDL 未显式给主键列加 NOT NULL，业务写入仍需遵守身份完整性。每表先列实际来源/用途，再列所有列，旧财务表明确单列。

### `sw_imports`

名单及市值导入审计版本。`content_hash` 对应导入内容摘要；`member_import_id` 允许多个导入复用同一个名单。新四表财务批次不会自动成为 sw_imports 版本。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `id` | INTEGER | 本地自增整数主键 | 主键序号 1 |
| `obtained_at` | TEXT | 采集/导入时间；不是报告期 | 非空 |
| `content_hash` | TEXT | 来源/清单内容 SHA256；具体哈希对象见本节说明 | 非空 |
| `source_manifest_json` | TEXT | 导入来源文件、请求、哈希、校验及状态清单 JSON | 非空 |
| `status` | TEXT | 状态，财务批次/行业任务/检查为 running、complete、failed；sw_imports 仅 complete | 非空 |
| `member_import_id` | INTEGER | 引用 sw_imports.id，复用/冻结所使用的名单版本，不应直接当作最新财务批次 | 可空 |

外键：`member_import_id` → `sw_imports.id`。

唯一索引/主键：`content_hash`。

### `sw_industries`

来源：申万官方 SW2021 分类文件 `SwClassCode_2021.xls`。父子结构保存一、二、三级；代码是文本。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `code` | TEXT | 申万行业代码，文本 | 主键序号 1 |
| `name` | TEXT | 行业名称/证券简称，取决于所属表 | 非空 |
| `level` | INTEGER | 行业层级 1/2/3 | 非空 |
| `parent_code` | TEXT | 父行业代码，引用 sw_industries.code，一级为空 | 可空 |
| `standard` | TEXT | 行业分类标准，默认 SW2021 | 非空；默认 'SW2021' |

外键：`parent_code` → `sw_industries.code`。

唯一索引/主键：`code`。

### `sw_memberships`

来源：申万 `StockClassifyUse_stock.xls` 与沪深 A 股名单；上市日期由上交所 JSON/深交所 XLSX 补齐。当前财务行业页面按选定名单版本连接，不把财务接口自带行业替代为申万分类。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `import_id` | INTEGER | 引用 sw_imports.id 的导入版本；成员表中是名单所属版本 | 主键序号 1 |
| `stock_code` | TEXT | 六位股票代码，文本保留前导零 | 主键序号 2 |
| `name` | TEXT | 行业名称/证券简称，取决于所属表 | 非空 |
| `industry_code` | TEXT | 申万行业代码；通常三级；按表约束可为空 | 可空 |
| `effective_date` | TEXT | 来源行业归属生效日期；不是采集时间 | 可空 |
| `source_update` | TEXT | 来源分类更新标识/日期文本；不是本地写入时间 | 可空 |
| `listing_date` | TEXT | A 股上市日期，沪深交易所名单补齐 | 可空 |
| `listing_source_id` | INTEGER | 引用 sw_listing_sources.id | 可空 |

外键：`listing_source_id` → `sw_listing_sources.id`；`industry_code` → `sw_industries.code`；`import_id` → `sw_imports.id`。

唯一索引/主键：`import_id, stock_code`。

### `sw_membership_history`

保存来源提供的分类生效/变更记录；不能据此声称已拥有退市公司齐全的历史全市场名单。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `import_id` | INTEGER | 引用 sw_imports.id 的导入版本；成员表中是名单所属版本 | 主键序号 1 |
| `stock_code` | TEXT | 六位股票代码，文本保留前导零 | 主键序号 2 |
| `effective_date` | TEXT | 来源行业归属生效日期；不是采集时间 | 主键序号 3 |
| `industry_code` | TEXT | 申万行业代码；通常三级；按表约束可为空 | 主键序号 4 |
| `source_update` | TEXT | 来源分类更新标识/日期文本；不是本地写入时间 | 非空 |

外键：`import_id` → `sw_imports.id`。

唯一索引/主键：`import_id, stock_code, effective_date, industry_code`。

### `sw_listing_sources`

共享上市日期证据；content_hash 为序列化来源证据摘要。更新时间使用 obtained_at，不使用 listing_date。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `id` | INTEGER | 本地自增整数主键 | 主键序号 1 |
| `content_hash` | TEXT | 来源/清单内容 SHA256；具体哈希对象见本节说明 | 非空 |
| `source_json` | TEXT | 共享来源证据 JSON，含来源地址/参数/文件等 | 非空 |
| `obtained_at` | TEXT | 采集/导入时间；不是报告期 | 非空 |

唯一索引/主键：`content_hash`。

### `sw_membership_checks`

按上海自然日去重检查。记录检查结果和名单版本；成功、失败及中断当天均不重复自动检查。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `check_date` | TEXT | 北京时间自然日，控制当日名单检查去重 | 主键序号 1 |
| `checked_at` | TEXT | 本地名单检查开始时间 | 非空 |
| `finished_at` | TEXT | 结束时间；运行中可空 | 可空 |
| `status` | TEXT | 状态，财务批次/行业任务/检查为 running、complete、failed；sw_imports 仅 complete | 非空 |
| `member_import_id` | INTEGER | 引用 sw_imports.id，复用/冻结所使用的名单版本，不应直接当作最新财务批次 | 可空 |
| `result_json` | TEXT | 本地执行结果及计数 JSON；以对应任务写入结构为准 | 可空 |
| `error` | TEXT | 失败原因；非失败可空 | 可空 |

外键：`member_import_id` → `sw_imports.id`。

唯一索引/主键：`check_date`。

### `sw_cap_facts`

公司市值，来源主要为东财 RPT_VALUEANALYSIS_DET.TOTAL_MARKET_CAP（元）；旧记录也可来自百度总市值（亿元换算元）及腾讯行情。准确来源、日期、单位换算以 provenance_id 对应证据为准，不使用复权价估算。

2016—2018 一次性补缺工具沿用本表与来源表，可发布历史未复权收盘价 × 对应交易日历史总股本的计算市值，不将其标成数据源直接返回值。仅补适用缺口，保留有效原值和原冻结名单；实际覆盖、核对、备份及批次关系见[专项来源说明](data-sources/historical-market-cap-backfill.md)及本机验收报告。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `import_id` | INTEGER | 引用 sw_imports.id 的导入版本；成员表中是名单所属版本 | 主键序号 1 |
| `stock_code` | TEXT | 六位股票代码，文本保留前导零 | 主键序号 2 |
| `trade_date` | TEXT | 市值实际观察交易日 | 主键序号 3 |
| `total_cap` | REAL | 总市值，元；行业汇总表仅全部成员齐全才非空 | 非空 |
| `provenance_json` | TEXT | 共享来源证据 JSON；事实行仅兼容占位，需解析 provenance_id | 非空 |
| `provenance_id` | INTEGER | 引用本类 sw_*_provenance.id | 可空 |

外键：`provenance_id` → `sw_cap_provenance.id`；`import_id` → `sw_imports.id`。

查询索引：`stock_code, trade_date, import_id`。

唯一索引/主键：`import_id, stock_code, trade_date`。

### `sw_cap_provenance`

共享市值来源证据，content_hash 对规范化证据 JSON 去重。必须与事实表的 provenance_id 关联，不只读兼容占位。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `id` | INTEGER | 本地自增整数主键 | 主键序号 1 |
| `content_hash` | TEXT | 来源/清单内容 SHA256；具体哈希对象见本节说明 | 非空 |
| `provenance_json` | TEXT | 共享来源证据 JSON；事实行仅兼容占位，需解析 provenance_id | 非空 |

唯一索引/主键：`content_hash`。

### `sw_cap_quarter_rosters`

每季度冻结一份名单引用，避免后续分类更新悄悄改写该季度市值成员口径。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `quarter` | TEXT | 季度键，格式 YYYYQn，例如 2026Q2；不是日期文本 | 主键序号 1 |
| `target_date` | TEXT | 目标季末日期；与实际 trade_date 分开保存 | 非空 |
| `composition` | TEXT | 成员构成口径；current_constituents_backfill 表示当前成员回溯，不是当时历史全市场 | 非空 |
| `member_import_id` | INTEGER | 引用 sw_imports.id，复用/冻结所使用的名单版本，不应直接当作最新财务批次 | 非空 |

外键：`member_import_id` → `sw_imports.id`。

唯一索引/主键：`quarter`。

### `sw_cap_quarter_members`

冻结季度的逐公司行业归属；不随当前成员列表自动变化。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `quarter` | TEXT | 季度键，格式 YYYYQn，例如 2026Q2；不是日期文本 | 主键序号 1 |
| `stock_code` | TEXT | 六位股票代码，文本保留前导零 | 主键序号 2 |
| `industry_code` | TEXT | 申万行业代码；通常三级；按表约束可为空 | 可空 |

外键：`industry_code` → `sw_industries.code`；`quarter` → `sw_cap_quarter_rosters.quarter`。

唯一索引/主键：`quarter, stock_code`。

### `sw_industry_cap_quarters`

只持久化三级行业市值，一级/二级读取时汇总。known_count≤expected_count；total_cap 非空要求 known_count=expected_count。已知市值部分和不冒充完整行业总市值。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `import_id` | INTEGER | 引用 sw_imports.id 的导入版本；成员表中是名单所属版本 | 主键序号 1 |
| `industry_code` | TEXT | 申万行业代码；通常三级；按表约束可为空 | 主键序号 2 |
| `quarter` | TEXT | 季度键，格式 YYYYQn，例如 2026Q2；不是日期文本 | 主键序号 3 |
| `trade_date` | TEXT | 市值实际观察交易日 | 非空 |
| `expected_count` | INTEGER | 应有公司数 | 非空 |
| `known_count` | INTEGER | 有有效市值的公司数 | 非空 |
| `known_cap` | REAL | 已知公司市值之和，元；可不完整 | 可空 |
| `total_cap` | REAL | 总市值，元；行业汇总表仅全部成员齐全才非空 | 可空 |
| `target_date` | TEXT | 目标季末日期；与实际 trade_date 分开保存 | 可空 |
| `composition` | TEXT | 成员构成口径；current_constituents_backfill 表示当前成员回溯，不是当时历史全市场 | 非空；默认 'current_constituents_backfill' |

外键：`industry_code` → `sw_industries.code`；`import_id` → `sw_imports.id`。

唯一索引/主键：`import_id, industry_code, quarter`。

### `sw_update_runs`

共用任务状态/互斥锁，包括新四表日常采集及历史补采；与 market_financial_batches 不是同一对象，也没有任务 ID 外键关联。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `id` | INTEGER | 本地自增整数主键 | 主键序号 1 |
| `action` | TEXT | 任务动作标识，具体值由采集入口传入 | 非空 |
| `target` | TEXT | 任务目标报告期/季度/历史范围标识 | 非空 |
| `recheck` | INTEGER | 0/1，是否显式重新核对 | 非空 |
| `started_at` | TEXT | 任务/批次开始时间 | 非空 |
| `finished_at` | TEXT | 结束时间；运行中可空 | 可空 |
| `status` | TEXT | 状态，财务批次/行业任务/检查为 running、complete、failed；sw_imports 仅 complete | 非空 |
| `result_json` | TEXT | 本地执行结果及计数 JSON；以对应任务写入结构为准 | 可空 |
| `error` | TEXT | 失败原因；非失败可空 | 可空 |

### `sw_financial_facts`

旧财务表，行业页面不读取、日常按钮不写入。历史可混合新浪核对/旧优先级和东财来源，必须逐字段解析证据；11 及 17 号迁移扩展的字段仍保留。真实历史修订与纯字段扩展旧行应按清理规则区分。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `import_id` | INTEGER | 引用 sw_imports.id 的导入版本；成员表中是名单所属版本 | 主键序号 1 |
| `stock_code` | TEXT | 六位股票代码，文本保留前导零 | 主键序号 2 |
| `period` | TEXT | 旧财务报告期 YYYY-MM-DD | 主键序号 3 |
| `revenue` | REAL | 旧营业总收入，元，本年累计；实际来源见引用证据 | 可空 |
| `parent_profit` | REAL | 旧归母净利润，元，本年累计 | 可空 |
| `notice_date` | TEXT | 旧营收/利润来源公告日期；与 performance_notice_date 区分 | 可空 |
| `provenance_json` | TEXT | 共享来源证据 JSON；事实行仅兼容占位，需解析 provenance_id | 非空 |
| `operating_revenue` | REAL | 旧扩展营业收入，元；东财利润表 OPERATE_INCOME | 可空 |
| `operating_cost` | REAL | 旧扩展营业成本，元；东财利润表 OPERATE_COST | 可空 |
| `deduct_parent_profit` | REAL | 旧扩展扣非归母净利润，元；DEDUCT_PARENT_NETPROFIT | 可空 |
| `operating_profit` | REAL | 旧扩展营业利润，元；OPERATE_PROFIT | 可空 |
| `provenance_id` | INTEGER | 引用本类 sw_*_provenance.id | 可空 |
| `basic_eps` | REAL | 旧 BASIC_EPS，基本每股收益，元/股 | 可空 |
| `bps` | REAL | 旧 BPS，每股净资产，元/股 | 可空 |
| `weighted_roe` | REAL | 旧 WEIGHTAVG_ROE，累计加权 ROE，% | 可空 |
| `operating_cashflow_per_share` | REAL | 旧 MGJYXJJE，每股经营现金流，元/股 | 可空 |
| `deduct_basic_eps` | REAL | 旧 DEDUCT_BASIC_EPS，扣非基本每股收益，元/股 | 可空 |
| `dividend_yield` | REAL | 旧 ZXGXL，来源股息率，价格基准待核对 | 可空 |
| `reported_gross_margin` | REAL | 旧 XSMLL，来源销售毛利率，% | 可空 |
| `performance_notice_date` | TEXT | 旧业绩接口 NOTICE_DATE，与原 notice_date 来源不同，不保证相等 | 可空 |

外键：`provenance_id` → `sw_financial_provenance.id`；`import_id` → `sw_imports.id`。

查询索引：`import_id, period`。

唯一索引/主键：`import_id, stock_code, period`。

### `sw_financial_provenance`

旧财务共享证据；content_hash 对规范化证据 JSON 去重。优先解析 provenance_id；行内 provenance_json 仅兼容。

| 字段 | 类型 | 含义/来源 | 可空/键 |
| --- | --- | --- | --- |
| `id` | INTEGER | 本地自增整数主键 | 主键序号 1 |
| `content_hash` | TEXT | 来源/清单内容 SHA256；具体哈希对象见本节说明 | 非空 |
| `provenance_json` | TEXT | 共享来源证据 JSON；事实行仅兼容占位，需解析 provenance_id | 非空 |

唯一索引/主键：`content_hash`。

## 6. 页面指标及计算边界

| 页面指标 | 当前取数/计算 | 注意事项 |
| --- | --- | --- |
| 公司营业收入 | View.revenue → TOTAL_OPERATE_INCOME | 实际为营业总收入；显示口径由服务做累计/单季/年度/TTM 转换 |
| 公司归母净利润 | View.parent_profit → PARENT_NETPROFIT | 不使用业绩表同名值覆盖利润表 |
| 公司毛利率 | (operating_revenue − operating_cost) / operating_revenue × 100% | 缺营业收入时，仅非金融公司允许用同口径营业总收入回退；金融不直接套用 |
| 公司 ROE | View.weighted_roe → WEIGHTAVG_ROE | 来源累计加权值，不按单季/TTM差分，也不采用净利润/期末净资产估算 |
| 行业收入、利润、毛利率及同比 | Python 在统一成员、可比公司及覆盖率规则下计算 | 不是所有公司比例简单平均；原始表不另存 TTM、同比副本 |
| 公司总市值及行业市值 | sw_cap_facts 与冻结季度汇总 | 与财务报告期、分类版本分别追踪；不能从 sw_financial_facts 取市值 |
| 上市日期 | sw_memberships.listing_date → sw_listing_sources | 不是业绩指标接口公告日 |

累计转单季需同年上一季度累计值（第一季度直接取累计）；非年末 TTM 通常为本期累计 + 上年全年 − 上年同期累计。同比须有相同计算口径的上年同期。缺依赖期则留空，不以零或旧期值补齐。资产负债表余额不做流量差分或 TTM 累加，每股指标、加权 ROE 也不能直接套用金额加减。

页面使用有界缓存：最多两组全市场财务依赖期窗口、八组所选行业汇总趋势；新成功财务批次或名单版本变化会失效。时间范围筛选只裁剪展示，不删除库内历史，也不截断计算依赖期。

## 7. 原始响应、追溯、时间与备份

原始文件相对数据库目录保存为 `market_financial_sources/<report_date>/<dataset>/responses/<SHA256>.json.gz`；来源表 file_path 相对该根目录。文件名按响应内容 SHA256 去重，解压后为原始 HTTP 响应，不是逐公司重构 JSON。不另外生成 baseline 合并 JSON。

`market_financial_sources.obtained_at` 是该来源页实际采集时间；财务行 `updated_at` 是本地核对时间；财务行 `obtained_at` 保留该修订最初插入时的来源时间。页面财务更新时间来自所选报告期成功批次的 finished_at；分类和市值时间由各自来源/导入批次追溯。没有采集时间的老来源不能用报告期、上市日或交易日代替，应显示未记录。

失败任务可能留下原始响应/检查点，但不代表已发布事实行；必须区分文件存在、批次 complete 和事实行 latest。历史补采与日常按钮共用任务互斥，按报告期事务发布。单独 SQLite 备份不包含原始响应 gzip 文件，完整迁移需同时复制来源目录；原始文件大小不计入 SQLite 表空间。

截至本次整理，已完成的四表报告期范围为 2014-09-30 至 2026-06-30（48 期）。这是当前采集覆盖，不是每家公司每期都有报告；后续覆盖以数据库查询为准。财务采集菜单支持最近十二年，市值仍为十年。

## 8. 常用只读查询

### 查某公司某期最新四表

```sql
SELECT security_code, exchange, report_date, total_operate_income, parent_netprofit
FROM market_financial_income
WHERE security_code = '601728' AND exchange = 'sh'
  AND report_date = '2026-06-30' AND is_latest = 1;
-- 其他三表使用相同身份条件，按第 3 节选字段。
```

### 追溯财务行至分页响应

```sql
SELECT i.security_code, i.report_date, i.version, i.is_latest,
       i.total_operate_income, i.parent_netprofit,
       s.url, s.params_json, s.file_path, s.content_hash AS raw_response_sha256,
       s.obtained_at AS source_obtained_at, b.status, b.finished_at
FROM market_financial_income i
JOIN market_financial_sources s ON s.id = i.source_id
JOIN market_financial_batches b ON b.id = i.batch_id
WHERE i.security_code = '601728' AND i.exchange = 'sh'
  AND i.report_date = '2026-06-30'
ORDER BY i.version;
```

### 查每表最新记录的覆盖（不把旧修订重复计数）

```sql
SELECT report_date, count(*) AS companies
FROM market_financial_performance
WHERE is_latest = 1
GROUP BY report_date ORDER BY report_date DESC;
```

### 查当前行业成员对应的最新财务

```sql
SELECT m.name, m.industry_code, f.*
FROM market_financial_industry f
JOIN sw_memberships m ON m.stock_code = f.security_code
WHERE m.import_id = (
  SELECT member_import_id FROM sw_imports ORDER BY id DESC LIMIT 1
) AND f.exchange IN ('sh', 'sz') AND f.report_date = '2026-06-30';
```

## 9. 维护入口与结构依据

结构 21 的全市场季度末未复权价格独立存储于 `market_quarterly_prices`，共享来源为 `market_price_sources`，批次为 `market_price_batches`；不绑定用户关注列表。字段、时间及采集流程见[专项来源说明](data-sources/market-quarterly-prices.md)。

行业内公司表新增可排序的 PE（TTM）、PB，不展示估值同比。PE 固定为所选财报季度对应的总市值（元）÷ TTM 归母净利润（元），利润来自 `market_financial_industry.parent_profit`，按本期累计＋上年全年－上年同期累计计算，年末直接使用全年值；不随页面累计／单季度切换改变。市值来自原季度名单和 `sw_cap_facts` 最新有效版本。利润非正、市值非正或任一必要输入缺失时显示 `—`。

PB = `market_quarterly_prices.close` 季末最后市场交易日未复权收盘价（元/股）÷ 同报告期最新 `market_financial_performance.bps`（元/股），结果为倍。BPS 统一通过结构 22 的 `market_financial_industry.bps` 读取，不按季度差分、不相加、不随页面周期模式改变；最新财务修订使缓存失效。价格按股票和自然季度末精确匹配，若已有市值季末交易日还须一致。价格缺失、BPS 缺失或非正时显示 `—`，不回退其他报告期、腾讯／百度现成 PB 或含少数股东权益的总权益；无市值但具备有效季末股价及同报告期 BPS 时仍可计算 PB。查看行业表不会自动联网补采。API 返回 `bps` 和 `pb_trade_date` 便于复核，表格提示显示日期与 BPS。两项均为历史估值参考，使用该报告期最新财务修订，不能作为当时已披露口径的无偏回测。

公司表金额单位统一在表下注明亿元，金额最多两位小数；同比、毛利率及 ROE 显示一位小数，PE/PB 显示两位小数（倍）。显示精度不改变计算及排序精度。桌面采用紧凑固定列布局，小屏保留横向滚动。

- [SQLite 存储、更新与备份](sqlite-storage.md)
- [全市场四表采集、发布及历史补采](data-sources/market-financial-statements.md)
- [旧行业业绩字段扩展说明](data-sources/industry-performance.md)
- [数据库维护页与表统计](database-maintenance.md)
- [财务源字段映射](../quarterly_dashboard/resources/market_financial_fields.json)
- [行业财务 View 迁移](../quarterly_dashboard/migrations/020_industry_financial_view.sql)、[三表与来源迁移](../quarterly_dashboard/migrations/018_market_financial_statements.sql)、[业绩指标迁移](../quarterly_dashboard/migrations/019_market_financial_performance.sql)
- [采集和版本写入逻辑](../quarterly_dashboard/market_financial.py)、[行业轻量读取](../quarterly_dashboard/industry_financial_reader.py)

新增源字段需同时更新字段映射、迁移/存储列及本文；新增表或用途调整需同步维护页用途、分类和更新时间登记。来源新增未登记字段时当前采集会拒绝发布，保留原始响应供核对，不会静默丢弃字段。

