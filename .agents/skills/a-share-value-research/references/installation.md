# 安装、迁移与自检

## 新电脑使用

1. 安装并登录 Codex，克隆 GitHub 上的本项目（或下载完整项目）。技能源码位于 `.agents/skills/a-share-value-research`，由 Git 管理。用 Codex 打开项目，项目技能会自动发现；未显示时重启 Codex。无需复制到用户目录。
2. 准备 Python 3.11+。研究和估值脚本只依赖标准库，无需额外 pip 安装。Windows 如只有 `py` 命令，以下 `python` 可替换为 `py -3.11`（或实际已安装的更新版本）。Codex 可通过运行时发现工具寻找 Python，不能写死用户名、盘符或缓存目录。
3. 恢复资料：`data/stock_analysis.sqlite3` 是项目财务数据库，`data/company_reports` 是正式财报及解析缓存。这些目录已被 Git 忽略，不随源码下载。使用项目现有备份／恢复工具迁移，核对 PDF 哈希；SQLite 备份不包含 PDF。也可以在项目中按现有流程重新采集所需公司资料。不要把私有数据库、凭据或持仓上传 GitHub。
4. 在项目根目录执行只读检查：

```text
python .agents/skills/a-share-value-research/scripts/research.py doctor
```

检查 Python、资料路径、数据库适配表和 HTML 模板；不写数据库、创建输出或启动后台。缺少 PDF 目录显示警告，缺数据库或表结构不符返回非零退出码。检查通过只证明运行资料入口可用，不保证所有公司或报告期的资料齐全。

5. 在 Codex 中选用 `a-share-value-research`，例如：“用 a-share-value-research 生成 601919 研报，截至今天。”实际研究按 SKILL.md 的流程执行，不额外调用未授权模型或开启项目后台。

## 路径配置

`references/local-profile.json` 默认使用项目相对路径。路径以自动发现的项目根目录为基准，而不是执行命令的当前目录；项目技能优先定位自身所在项目，复制到用户目录的技能再定位当前工作目录所属项目。

```json
{
  "adapter": "investment-sina-v1",
  "database": "data/stock_analysis.sqlite3",
  "reports_root": "data/company_reports",
  "output_root": "data/research_reports",
  "history_years": 10
}
```

不同布局使用 `--project-root <项目目录>`、`--database <数据库>`、`--reports-root <财报目录>`，或用 `--profile <另一个配置JSON>`。配置中的相对路径始终以项目根目录为基准；命令行显式路径按当前目录解析。`output_root` 提供默认目录约定，生成仍要求显式 `--out`，防止误覆盖既有研报。

```text
python .agents/skills/a-share-value-research/scripts/research.py collect --code 601919 --as-of YYYY-MM-DD --out data/research_reports/sh601919/YYYY-MM-DD/v1
```

缺少本地数据库时可以用 `collect --input <规范化事实JSON>`；该模式无需数据库和 profile，但不能伪造缺失事实。没有项目根标记时用 `--project-root` 显式指定资料目录。

## 维护与兼容

项目内版本是唯一源码维护入口，模板、分析框架、渲染器和测试一起提交 Git。HTML 的列宽按表头匹配，标签、星标、结论框和章节来源均静态渲染，不依赖 601919 专用脚本或浏览器 JavaScript。旧报告中的来源账本保留生成时路径，迁移技能不会改写历史出处；换电脑重生成会记录新资料的实际路径。

其他项目需要使用时，可通过 skill-installer 从本 GitHub 仓库的 `.agents/skills/a-share-value-research` 路径安装，并在取数时显式指定 `--project-root`。同名项目版和用户版不会合并；避免保留两份活动版本。本机旧用户版可以在 Codex 配置中禁用，保留原文件用于回退。

源码自检（不使用日常数据库）：

```text
python -m unittest discover -s .agents/skills/a-share-value-research/scripts -p test_research.py
```

适用平台为 Windows、macOS、Linux，路径由 pathlib 解析。首次迁移验收包含另一目录下的隔离数据库取数和报告渲染；网络访问、模型权限、PDF 阅读工具及数据库内容属于新电脑运行环境，不能靠技能源码代替。

研报资料包中的本地财务原始响应默认使用 `sources/financial-raw.json.gz` 压缩保存；gzip 属于 Python 标准库，无需安装依赖。只阅读HTML无需解压，取数逻辑仍在项目技能脚本中。旧 `.json` 文件兼容保留，转换时核对内容并更新报告manifest。
