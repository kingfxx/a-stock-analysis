# 开发进度

- 已检查现有Python/JS计算、DCF只读API、FCF数据映射、Node测试及页面CSS，已阅读项目规则与财务补充规则。
- 已建立本次计划与财务口径决策，独立方案审查通过，Python及前端协作执行、主代理最终集成验收已完成。
- 本次初始Git工作区干净；不涉及采集、数据库结构或旧计划状态。

## Python执行与测试

- 已将预测与折现解耦：`forecast_standard`保持原幂次计算，`forecast_three_stage`按3/4/3年逐年复利，两者共用`discount_cash_flows`；新增`calculate_growth_dcf`。没有修改财报查询、FCF口径或股权桥接。
- API仅添加`growth_defaults`示例15/8/4%，原`defaults`不变；三阶段提示高增长、阶段反升、永续反升、负折现及终值依赖，不裁剪输入。
- `python -m pytest tests/test_dcf.py tests/test_dcf_server.py -q`：48 passed。覆盖阶段3/4及7/8、首年与第11年终值、复利与折现、元/股换算、负小数增长与折现、缺值/零负FCF、非有限值/序列长度/溢出、API只读与配置。
- 标准DCF历史golden逐字段严格相等；额外以固定随机种子对照HEAD原实现1000组有效参数，完整返回结果均严格相等。

## 前端执行（2026-10-15）

- 已完成标准／三阶段模式切换，复用基准窗口、三情景、每股参考值及矩阵；三个阶段固定1—3、4—7、8—10年，增长参数独立保留，API成长示例配置可覆盖。
- 已拆分标准／逐年复利预测与共享10年折现引擎。原标准模式保留幂次和PV乘法数值路径；5组原实现golden的显性现值、终值、终值现值、总值、每股和参考差额严格相等。
- 新增稳定Plotly预测图、三阶段背景及实际增长率标识、逐年明细阶段列；错误、金融企业、切回标准时清图，负FCF图不裁剪。高增长、阶段反升、永续反升及非常规折现只提示，不改输入；历史恒定增长推荐在成长模式禁用。
- 已执行 `node --check web/dcf.js` 和 `node tests/dcf_ui.cjs`，全部通过。覆盖阶段边界、终值、元／亿元／股本、零／负FCF、小数／负增长和折现、缺项／NaN／Infinity／溢出、模式参数保留、指标切换、本地无新增fetch、图数据／背景／单位与失效清理。隐藏标准增长率为极大值或空值不影响成长情景。
- 前端源文件仅修改 web/dcf.js、web/dcf.css、web/index.html 的DCF资源版本戳、tests/dcf_ui.cjs。真实宿主8765浏览器和窄屏验收已由主代理完成。

## 集成验收与完成状态

- 相关Python测试：155 passed（含5项Python/JS交叉对照），另2项宿主HTTP／真实浏览器回归2 passed，共157通过。覆盖既有DCF/季度转换/财报/估值及本次增长模型。使用隔离临时库，无需前端打包；`node --check web/dcf.js`、DCF与既有估值Node测试、`git diff --check`通过。
- 原库只读核对600887、300750、601919、601398：标准结果与原提交完整相同，三阶段10年现金流逐年与独立分段幂次公式相符，终值第11年正确，金融识别保持阻止。报告 `data/verification/reports/three-stage-dcf-history-20261015.json`。
- 核对原8765 PID 22320为本仓库app.py后自动重启，新PID 30292；原SQLite、结构版本33正常，成长配置API 200，保持运行。日志 `data/verification/logs/three-stage-dcf-8765.stdout.log`／`three-stage-dcf-8765.stderr.log`。
- 浏览器检查中远海控：标准为默认，成长示例15/8/4；修改阶段1即时重算，切换模式保留17.25%；负r=-0.5%、gt=-2%可算并警告；r=gt清空结果和预测图；返回PB收起DCF、再开保留成长模式；无console error。
- 桌面与390px验收通过；窄屏阶段标签重叠已分行修复，控件位于视口内，负值图未裁剪。最终图 `data/verification/reports/three-stage-dcf-acceptance-20261015.jpg`。共享JS引擎补稀疏数组缺年校验及回归。
- 已同步DCF文档、指标说明及README。仅保留正式验收图／报告及当前日志，清理本次4个临时测试目录；本次未提交Git。
- 遗留限制：沿用简化FCF，专业FCFF/EV到归母股权桥接仍因现有数据缺口不可用，因此展示总折现值、每股参考值及参考价格差额，而非伪精确股权估值和安全边际。成长参数是示例，非公司预测；无新增阻塞事项。
