# A 股季度分析页面

本地运行的 A 股分析工具，提供企业经营趋势、财务三表、估值、筹码、行业对比和投资 checklist。数据保存在本地 SQLite，支持独立刷新、备份与恢复。

## 项目演示

1 分 37 秒了解主要功能，视频配有中文字幕。

https://github.com/user-attachments/assets/500b5d06-85d4-4666-b1f2-60e4c5a677c1

![A 股分析页面：宁德时代季度财务对照与月度估值](docs/images/dashboard.png)

截图以 300750 宁德时代为例，数据和缓存日期仅代表截图时状态。

## 主要功能

- **企业经营趋势**：A／B／C 独立选择 18 项指标及柱形／折线，支持单季度、年度、TTM 和历史范围筛选，包括主营现金流、季末市值及披露日估算市值。下方提供估值历史、分位／标准差参考线和筹码趋势图。
- **财务分析**：按报告期查看资产负债结构、利润与现金流瀑布及完整三表，支持累计／单季、比较期、金额单位与来源展开。
- **我的股票**：按代码、名称或拼音搜索，管理分组和组内顺序；支持取消关注与恢复，历史资料按维护规则保留。
- **行业分析**：公司对比、行业汇总、排行和趋势，财务与市值分别更新。
- **Checklist、经营判断与 AI 研报**：18项定性checklist；经营判断通过联网检索、严格结构化分析两阶段生成，后台绑定来源，提供三张前置表、行业地位附表、明确结论与独立历史，共用AI设置；AI研报入口浏览项目技能已生成的本地HTML。仅明确生成才使用模型额度。
- **后台维护**：查看数据库用途、记录数与占用，管理备份、恢复、未关注股票清理及数据库空间整理。

指标的含义、计算公式、来源表／字段、周期和缺值规则统一见[企业经营趋势指标说明](docs/enterprise-trend-metrics.md)。

## 启动

需要 Python 3.10 及以上。在仓库根目录运行：

```powershell
python -m pip install -r requirements.txt
python app.py --port 8765 --open-browser
```

打开 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)，指定股票可访问 [宁德时代页面](http://127.0.0.1:8765/?code=300750)。服务只监听本机；在启动窗口按 `Ctrl+C` 停止。日常使用及修改后的人工验收统一使用 8765 和原数据库；隔离测试服务须显式指定其他空闲端口。

Windows 可双击 [start_dashboard.bat](start_dashboard.bat)。脚本依次使用 `PYTHON_EXE`、项目 `.venv`、PATH 中的 Python 或 `py -3`，检查环境后启动；可传入启动参数，关闭窗口即停止服务。

Plotly 由本地 Python 包提供，不依赖 CDN；首次取数和后续更新需要访问外部数据源。完整依赖见 [requirements.txt](requirements.txt)。

## 数据与连接

正常业务从 `data/stock_analysis.sqlite3` 读取，旧 JSON 用于导入与保留原始备份。财报、价格、分红、估值和筹码按各自流程更新；请求失败时保留已验证记录并显示警告。

`data/` 不随 Git 同步。换电脑前使用[SQLite 备份与恢复](docs/sqlite-storage.md#维护命令)；SQLite 备份不含正式财报 PDF，完整资料迁移见[checklist 说明](docs/ai-stock-assessment.md)。清理资料或整理数据库空间使用后台维护入口，规则见[后台维护说明](docs/database-maintenance.md)。

数据源默认直连，不继承系统／环境代理。需要代理时，在启动窗口显式配置后重新启动：

```powershell
$env:DASHBOARD_USE_SYSTEM_PROXY = "1"
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
python app.py --port 8765 --open-browser
```

HTTPS 证书验证保持启用；代理地址应按实际环境设置。

## 文档导航

| 文档 | 内容 |
|---|---|
| [企业经营趋势指标说明](docs/enterprise-trend-metrics.md) | 第一个 tab 的 18 项对照指标、估值及筹码：含义、公式、来源、周期、缺值与限制 |
| [财务分析说明](docs/financial-analysis.md) | 第二个 tab 的三表分析、来源展开和金融企业口径 |
| [财务四表数据字典](docs/financial-data-dictionary.md)／[CSV 清单](docs/financial-data-dictionary.csv) | 新浪原始科目、字段码、口径及覆盖统计 |
| [设计文档](docs/architecture-design.md) | 代码职责、加载、同步、缓存及备份流程 |
| [SQLite 存储说明](docs/sqlite-storage.md) | 表结构、迁移、同步和维护命令 |
| [后台维护说明](docs/database-maintenance.md) | 统计、取消关注、数据清理与空间整理 |
| [行业数据说明](docs/industry-data-schema.md) | 行业分类、财务及市值数据结构与取数规则 |
| [Checklist 说明](docs/ai-stock-assessment.md) | 手动生成、正式财报、历史和完整资料备份 |
| [经营判断说明](docs/business-judgment.md) | 联网结论表、改变条件、独立历史与共享 AI 设置 |
| [项目研报技能](.agents/skills/a-share-value-research/SKILL.md) | 公司价值研究与 Markdown／HTML 研报生成 |

## 开发检查

```powershell
python -m pip install pytest
python -m pytest tests -q
```

自动测试使用隔离临时数据库。前端回归还需要 Node.js，部分真实浏览器测试需 Edge；测试端口须与日常后台分开。运行与项目维护约定见 [AGENTS.md](AGENTS.md)。
