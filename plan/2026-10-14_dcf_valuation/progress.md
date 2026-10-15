# 开发进度

- 已阅读根规则及财务补充规则，检查server、core、FundamentalService、PriceService、财务及估值页面、SQLite交易快照表。
- 已完成计划与规划阶段口径审查，允许简化DCF实施，禁止自动专业股权调整。
- 财务数据独立只读检查已完成；后端/前端开发及集成验收已完成。

## 后端执行代理 A

- 已实现 `quarterly_dashboard/dcf.py` 只读 payload，复用 core 年度 FCF 与既有财务/价格读取；连续1/3/5年窗口，最近10年稳健推荐及实际字段/币种/合并范围/hash/run/人工覆盖证据。
- 专业FCFF明确不可用；报表实收资本未经核实不替代总股数，无原始合并币种证据legacy不参与；金融模型阻止显示。
- 行情按最新有效未复权日期择新，同日优先EM；股本独立选择最新有效记录，未来日期不参与。
- `python -m pytest tests/test_dcf.py tests/test_dcf_server.py -q`：22 passed，涵盖第11年终值、元/股、缺值/负值/溢出、窗口缺年、外币、override、市场择新/缺股本/未来日期及WACC。
- 原库只读核对600887、300750、601919、601398共40个年度，FCF均与CFO-CapEx一致；工商银行识别blocked。报告：`data/verification/reports/dcf-history-20261014.json`。

## 前端执行代理
- 完成股息率旁DCF入口、独立dcf.js/dcf.css；首次展开只读加载，四参数/推荐/WACC主动应用均本地计算，财务刷新事件仅标旧数据并在下次展开重读。
- 三种情景、十年明细、终值现值/占比、5×5敏感性与历史来源；简化模式明确非FCFF/FCFE，专业模式展示缺口，金融企业阻断。
- node tests/dcf_ui.cjs通过：永续/第11年终值/亿元与股单位、缺失/零/负/NaN/溢出/r≤g、WACC、DOM默认配置/推荐/本地变化/缓存/刷新/失败重试/金融阻断。
- 主代理已完成真实浏览器及宿主8765集成验收。

## 最终集成验收（2026-10-15）

- 相关Python回归：124 passed、2个宿主HTTP/浏览器用例另行执行2 passed，总计126通过。覆盖DCF、API、季度转换、三表、财务服务、既有估值频率及传输。
- 初次长临时目录导致Windows路径过长，换到工作区短路径后通过；两项本地HTTP在沙箱受到socket限制，宿主使用隔离数据库复验通过。没有修改无关模块规避测试。
- `node tests/dcf_ui.cjs`、`node tests/valuation_ui.cjs` 通过；`git diff --check` 通过。
- 宿主核对8765无监听后启动 `python -u app.py --port 8765`，使用原SQLite，保持后台运行；日志 `data/verification/logs/dcf-8765.stdout.log` / `dcf-8765.stderr.log`。只读DCF API返回200，页面无浏览器console error。
- 浏览器验收宁德时代：按钮位于股息率旁；默认3年/3%/9%/2%；增长率修改即更新三情景及明细，折现率等于永续率时清空结果并提示；财务刷新后提示旧依据，重新展开读取更新；390px参数控件位于视口内，表格横向滚动。
- 桌面验收凭证 `data/verification/reports/dcf-acceptance-20261014.jpg`，历史交叉验证 `data/verification/reports/dcf-history-20261014.json`；保留正式验收证据及当前后台日志，清理本次临时测试目录。
- 剩余限制：专业FCFF/EV归母股权桥接未开放，资本成本自动来源缺失；页面明确数据缺口，人工WACC不改变现金流口径。属于已审定的本版边界，无阻塞事项。

## 指标切换修正（2026-10-15）

- 修复DCF展开后点击PE/PB/PS/股息率未收起DCF的问题；统一展开状态与按钮高亮，返回传统指标时恢复单一选中状态，保留DCF参数和缓存。
- Node DCF及现有估值回归通过，新增四指标逐一切换及缓存保留断言。
- 原库8765浏览器验证DCF展开后点击PB：DCF隐藏、aria-expanded=false、卡片显示PB(MRQ)。验收截图 `data/verification/reports/dcf-switch-20261015.jpg`。
- 仅静态JS修改，后台实时读取静态文件，刷新页面生效，无需重启8765。
