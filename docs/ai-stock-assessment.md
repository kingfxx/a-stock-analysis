# 投资 checklist

页面现已启用 18 项定性 checklist，每项一句至两句。旧 AI 研判代码与历史数据保留，页面不启用；原使用说明见 [旧研判说明](ai-stock-assessment-legacy.md)。

## 使用

1. 在股票页面打开 Checklist。查看页面、历史与 AI 设置均不会自动调用模型。
2. 点击“准备财报资料”：已有年报、中报直接复用；没有资料时从巨潮下载最新完整年报和中报。新一期发布后点击“刷新财报版本”。
3. AI 设置沿用现有 ChatGPT 登录授权和模型选择；资料更新结束后，明确点击“生成 checklist”才使用额度。
4. 结论按 18 项展示，展开依据可见生成时数据、公告地址及 PDF 页码；缺失项标记待补。资料变化后可重新生成，旧版本保留。
5. 历史支持选择、批量删除和最新版本回退；删除只移除任务与无引用快照，不删除可复用财报。

## 存储及复用

- checklist 仍保存在 SQLite 的 `ai_analysis_runs` 和 `ai_analysis_snapshots`，版本 `stock_checklist_output_v1`。
- 正式原件：`data/company_reports/<交易所代码>/<报告期>/<完整SHA256>/report.pdf`，不增加 annual 子目录。
- 逐页文本：同哈希目录 `parsed/pdfplumber_pages_v1/pages.jsonl`；主题原文、PDF 页码和提取版本索引位于三张 `company_report_*` 表。
- 财务取数 gjzb 同期同口径优先，三表补齐；加权 ROE 与既有 TTM 简化估算分别标注。估值复用十年历史分位，分红按归属年度已实施事件汇总，筹码用最近三期同口径股东和一年周度融资。
- 规则提取只整理原文，不自动进行 OCR；财报中的“其他地区”不能直接当海外，行业景气按报告期解释。模型不得用记忆补齐这些缺失。
- 正常生成仅一次模型调用；PDF 相同则复用缓存，不每次重解析。首次解析需下载和本地文本提取，刷新资料与模型生成分开触发。

## 导入已有 PDF

```powershell
python -m quarterly_dashboard.company_report_service --code 600887 --period 2025-12-31 --file <PDF路径> --url <正式来源URL> --published-on 2026-04-30
```

## 备份和恢复

既有 SQLite 日备份不包含文件系统 PDF。需要完整迁移时使用：

```powershell
python -m quarterly_dashboard.report_backup --output E:\backup\investment-full.zip
python -m quarterly_dashboard.report_backup --restore E:\backup\investment-full.zip --output E:\backup\investment-restored
```

备份仅包含数据库、已登记财报原件及必要解析文件和 SHA256 清单，不含 AI 凭据。备份文件和恢复目录必须不存在；恢复前验证路径与全部文件哈希，恢复后核验数据库。恢复资料位于新目录，接管日常库属于独立维护操作。
