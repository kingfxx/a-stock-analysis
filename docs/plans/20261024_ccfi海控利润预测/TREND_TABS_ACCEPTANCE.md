# 双 Tab 与 CCFI/SCFI趋势验收

日期：2026-10-25

## 实现范围

- 独立研究页新增「利润预测」「CCFI / SCFI 趋势」Tab。旧 `/?code=601919&tab=shipping-forecast` 进入利润预测；`view=trend` 恢复趋势选中状态。预测Tab原有功能完整保留，切换不重建参数表单。
- 新增只读 `/api/shipping-trend`，严格白名单指标、频率、范围、质量和利润参数，校验证券代码601919。按当前修订读取 `research_shipping_rate_points`；月份仅 `monthly_mean`，周度仅 `release`。服务不发起网络请求、模型调用或业务写操作。
- 图表提供完整趋势、按日历月/ISO周的多年对比、年份多选及可选财务利润子图。日期提示按统一横轴；明细保留来源通道、质量、修订、来源ID和取得时间。月度缺期显式断线，周/月不互相换算。
- 月/周共享指标、频率、航线、时间范围及来源质量筛选。官方为默认，补充资料仅在没有官方同期记录时回退。周度航线别名只去除源格式后缀，西非与东西非保持不同名称。利润口径沿用金融指标字段：gjzb优先、lrb回退；累计直接读取，单季同年差分，缺上期不推算，零/负值有效。

## 隔离测试

- `python -m pytest tests/test_shipping_forecast.py tests/test_shipping_http.py -q --basetemp=.pytest-shipping-tmp`：22 passed。覆盖趋势口径/质量优先级、修订、航线边界、ISO周周年与第53周、利润差分/缺失/零负值，以及隔离HTTP和Edge页面。
- `node --check web/shipping-forecast.js`：通过。
- 隔离HTTP服务测试通过，临时绑定本机端口并使用临时SQLite。接口覆盖补充来源的46期月度事实、严格 `monthly_mean` 和 SCFI 月度空态；证券代码保护与预测保存Origin/session保护仍通过。临时服务已关闭。
- Edge Playwright 在1440px与390px检查双Tab、参数切换保留、多年多选、Plotly图及空态→有数据恢复、窄屏筛选换行和无横向溢出；无浏览器页面异常。缩放窗口改变后等待Plotly响应式重排再验收。
- `python -m pytest` 默认系统Temp在此受限环境不可写；改用项目内 `.pytest-shipping-tmp` 后运行通过，临时目录在验收后清理。

## 真实覆盖快照

只读查询正式库得到：

| 筛选 | 点数 | 首末期 | 说明 |
|---|---:|---|---|
| CCFI 官方月度综合 | 153 | 2014-01至2026-09 | 只取 monthly_mean |
| CCFI 官方周度综合 | 2 | 2026-10-16至2026-10-23 | 只取 release |
| SCFI 官方周度综合 | 2 | 2026-10-16至2026-10-23 | 两期观察 |
| SCFI 月度综合 | 0 | — | 页面明确空态，不派生月均 |

各航线和质量层级分别按筛选重新统计；首末期不代表逐期完整。周度月度数据缺失均不插值。

## 正式服务与页面复核

- 重启前确认8765监听进程为 `python.exe -u app.py --port 8765`（PID 10844），正式库无运行中采集任务；按项目规则重启后PID 23756，持续监听。
- 重启后正式趋势页HTTP 200，`/api/shipping-trend?index=SCFI&frequency=monthly` HTTP 200、点数0。运行日志保留于 `data/verification/logs/shipping-trend-tabs-final-8765.out.log` 和 `data/verification/logs/shipping-trend-tabs-final-8765.err.log`。
- 窄屏浏览器复核显示双Tab、可用年份、多年叠加图；CCFI官方综合月度153点，2014-01至2026-09。真实页面保持窄屏布局，筛选两列排列。SCFI月度空态在隔离Edge及正式数据API核对通过。
- 已在浏览器确认切换Tab不会丢失未保存参数，不触发保存。原始数据、定时任务配置及历史版本保持不变；正式页面GET仅本地只读。
- 最终结果：代码、文档、隔离数据库测试、HTTP与桌面/窄屏验收、生产服务重启和日志/访问检查均完成。真实覆盖仍以本记录表格所列为限，缺期不补造。
