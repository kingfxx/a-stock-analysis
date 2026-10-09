# 行业业绩指标与历史基准

行业批量财务新增八字段，统一来自东方财富 `RPT_LICO_FN_CPD`。原有六个金额及披露日不因历史补采而改写。

| 数据库字段 | 来源字段 | 定义/单位 |
|---|---|---|
| basic_eps | BASIC_EPS | 基本每股收益，元/股，本年累计 |
| bps | BPS | 每股净资产，元/股，报告期末 |
| weighted_roe | WEIGHTAVG_ROE | 加权净资产收益率，%，本年累计，不年化 |
| operating_cashflow_per_share | MGJYXJJE | 每股经营现金流量，元/股，本年累计 |
| deduct_basic_eps | DEDUCT_BASIC_EPS | 扣非基本每股收益，元/股，本年累计 |
| dividend_yield | ZXGXL | 来源股息率，%；价格基准及分红期间未核实，只保留采集时原值 |
| reported_gross_margin | XSMLL | 来源销售毛利率，%，本年累计；独立于现有三表计算毛利率 |
| performance_notice_date | NOTICE_DATE | 来源公告日期；不替换原 notice_date，不以 UPDATE_DATE 代替 |

有效零保留；空值、无效数值不当成零。每股指标与比率不得套用累计差分或 TTM 相加。金融公司毛利率可能为空。股息率挂在来源返回的报告期记录上不代表历史期末股息率，不用于历史比较。每项来源引用 `performance_source`，含请求、原始文件哈希和实际采集时间。数据表更新时间继续关联 `sw_imports.obtained_at`，不使用报告期或公告日冒充采集时间。

日常指定报告期任务与批量历史采集自动补充这些字段。独立历史补采支持缓存续传，按原有当前沪深成分和最近十年已存报告期匹配，保留旧值和修订版本，不保证已退市公司覆盖。

```powershell
python -m quarterly_dashboard.industry_performance_backfill data/industry_sources/performance_fields_20261008 --report-directory data/verification/reports/performance-fields-20261008 --all-history --publish
```

## 统一 JSON 基准

行业内公司对比表在毛利率后展示 ROE（%），支持升降序排序、缺值置底，有效零值正常显示。ROE 直接读取所选报告期的 `weighted_roe`，不随营收的 TTM／单季度切换进行相加或差分；全年模式使用所选年报的累计加权 ROE。表头及页脚标明报告期累计加权口径，单元格提示来源 `RPT_LICO_FN_CPD.WEIGHTAVG_ROE`。不直接平均公司 ROE 生成行业排行指标。

页面直接读取 SQLite，日常不生成或保留 JSON 基准；2026-10-08 的临时基准已按用户要求删除。以下导出工具仅用于按需数据交换或固定分析快照，不能替代 SQLite 正式备份和原始响应。导出使用同一 SQLite 只读事务，输出至不存在的新目录，避免混入不同时间状态或覆盖旧快照。

- `financial/<报告期>.json`：该期全部股票、全部修订版本；`is_latest=true` 为最新值，同股票同报告期最大 `import_id` 决定最新版本。
- `sw_financial_provenance.json`：财务来源索引，按 `provenance_id` 查找；旧行兼容行内 `provenance_json`。
- 其余 `sw_*.json`：分类、成员、上市日期来源、季度市值、批次和任务记录等所有行业表；市值来源使用独立索引。
- `manifest.json`：格式版本、生成时间、数据库版本、文件记录数、字节数及 SHA256 校验值。

原始来源目录不移动、不删除。JSON 基准保存结构化历史及原始文件引用；独立机器审计还需一并迁移 `industry_sources` 原始响应。此快照不是完整业务数据库备份，也不是无偏的历史投资回测成分。

再次生成新基准可运行：

```powershell
python -m quarterly_dashboard.industry_snapshot data/industry_sources/baselines/<新版本目录>
```

## 字段补全旧版本清理

离线整理仅删除被明确字段补采批次完整替代的相邻旧版本：原有有效金额、披露日期、所有有效字段及其定义不变（原先为空的字段或披露日期补齐不算修订），且旧来源证据在后继版本中完整保留。真实修订、缺少原有效字段、口径变化或来源证据不完整的候选记录保留。整理前正式备份，整理后核对最新全部字段与完整来源指纹、SQLite 完整性及外键；原始响应及来源索引不删除。
