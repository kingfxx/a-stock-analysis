# 个股公共调度独立验收

日期：2026-10-14。当前结论：**S1–S4 修复已完成并部署，模拟隔离回归通过；3 项 live HTTP 测试及真实来源覆盖仍未验收。** 以下保留原始发现与修复记录，最新处理记录见文末。

## 验收范围与证据

- 读取本目录 PLAN／DECISIONS／PROGRESS，检查 stock_collection、新增 Session 路由、各领域接入、公共 metadata_only、sync 提交／恢复和页面改动。原有财务／行业未提交工作保留，不将其归为本计划新增产出。
- 按执行记录的组合命令独立重跑：**220 passed、3 deselected，27.91 秒**。三项排除为 live_routes、live_data_failure、live_shared_price_context，本轮未运行这些本机 HTTP 用例；不是全仓通过结论。
- 模拟响应和隔离库确认 metadata_only 不生成 `.raw.gz` 文件；新路径继续调用公共预算，现有事实 raw_json、sync_runs/state 和默认完整归档路径保留。此项与用户“先公共调度、暂缓归档”一致。
- 补充边界复现结果：`data/verification/reports/20261014_stock_collection_review.json`。临时库退出后清理，无真实来源网络请求，未改业务代码或日常库、未重启后台。
- 执行记录中的 PID 35192 和 HTTP200 属此前部署证据，本轮不据此宣称重新验证了生产采集或实际来源覆盖。

## S1 · P2：恢复探测只验证外层包络，业务无效响应会清除同源冷却

位置：`quarterly_dashboard/stock_collection.py:71–83`，特别是百度分支 79–80。

当前百度健康检查仅要求 Result 是非空列表，腾讯只要求 code=0 且 data 是 dict，东财 success=true 即通过，新浪也允许只存在 report_count。它们不能证明当前股票、接口、指标或价格窗口响应可用。公共 collector 在回调通过时立即清除冷却；后面的真实解析若报错，仅让该业务 run 失败，不会重新标记 provider 阻断。下一个指标／模块就能继续同源请求。

隔离复现：设置百度因拒绝进入的冷却已经到期，第一次 PE 请求返回 HTTP200、`{"Result":[{}]}`。健康回调通过并把 cooldown_until 置 NULL；实际 `fetch_valuation_indicator` 因缺少图表结构抛 ValueError。接着 PB 又发出第二次请求，仍得到无效响应。总请求数 2，两个实际业务解析均失败，但提供方冷却已被解除。这违背本计划“到期单次有效恢复探测，无效探测继续冷却”的要求。

修复建议：给当前请求绑定接口级健康校验，检查实际所需的结构、状态及身份，优先复用或拆出已有解析器的无写入校验，而不是仅按 provider 检查包络。对不支持／结构不符的响应返回 False；顶层为 list、null、标量等时也应安全返回 False，不让 AttributeError/TypeError 跳出探测处理。冷却只有来源适配验证有效才能解除；明确无数据按各接口已确认的无数据协议处理。

回归要求：通过真实 RequestCollector＋到期冷却＋RoutedSession 调用既有解析入口，覆盖百度缺失 chartInfo、腾讯缺请求股票节点、东财 success但缺结果／非法分页、新浪 count/list不一致，以及各自有效响应和明确无数据。无效探测应延长冷却并阻止第二次同源发送，有效探测才能恢复。全部使用模拟，不发送真实测试请求。

## S2 · P3：后续成功没有解除旧失败触发的自动重试静默

位置：`quarterly_dashboard/update_service.py:58–68`，`recent_sync_failure`。

查询先过滤 failed，再选最新失败，并不考虑之后已经存在的 success/no_data。因此“自动失败→手动刷新成功→再次普通查看”仍返回旧失败，页面继续提示暂缓重试，直到原失败满十分钟。成功数据通常仍保留，所以优先级较低，但状态提示与当前实际结果不一致。

隔离复现：同 SyncKey 建立 failed run 1，再建立并完成 no_data run 2，helper 仍返回 failed run 1。各服务在成功缓存判断前调用它，因而会使用该旧警告。

修复建议：根据同键最近已完成执行判断，或只接受“没有更新成功／明确无数据提交覆盖”的失败。区分后续成功、仍 running、superseded 等状态，避免简单查询最大 id 后忽略实际完成顺序。成功恢复不应继续报告上一轮自动失败。

回归要求：失败→成功／no_data 应解除旧静默；成功→失败仍维持十分钟；不同股票、来源、复权方式互不影响；手动请求不绕过 provider 预算／冷却。

## 修正复核 · 2026-10-14

- S1、S2 已按以上意见修正；实现和测试记录见本计划 `PROGRESS.md`。
- 复跑上述组合：**227 passed，3 deselected**；隔离测试未发送真实来源请求。`git diff --check` 通过。
- 三个 live HTTP 用例仍未运行；本次结论只覆盖模拟响应、来源解析和隔离存储，不代表真实来源或全仓覆盖验收。
- 已按项目规则核对旧 PID 35192 身份并重启；新进程 PID 39132。启动日志正常，首页与只读维护 API 均 HTTP 200。S1/S2 修正已部署。
- 启动轮询的一次短超时请求主动关闭大页面连接，stderr 有 `ConnectionAbortedError 10053`；随后页面及 API 的较长超时请求均成功。它不影响本次服务验收。
- 最终限制：3 项 live HTTP 用例仍 deselected；没有真实来源请求或来源覆盖验收。

## 修正后的独立复核 · 2026-10-14

- 已检查修复代码与新增回归，独立重跑原组合：**227 passed、3 deselected（27.46 秒）**。S1 原百度 Result=[{}] 的无效探测场景现在阻断第二次同源请求，有效百度图表可恢复；S2 查询改为按完成时间选择最新结果，原失败后 no_data／成功不再保留旧失败静默。原复现场景可关闭。
- 但来源级健康校验还需覆盖“有效响应必须通过”，不能只有各源非法响应被拒绝的测试。此次发现两个腾讯误拦截，均属于 S1 修复中新加入的健康检查路径。
- 隔离复现报告：`data/verification/reports/20261014_stock_collection_review_fix1.json`。未发真实来源请求，未改业务代码、原库或后台。

### S3 · P2：腾讯简称健康回调把 q= 当成股票代码的一部分

位置：`quarterly_dashboard/stock_collection.py:66–71`，`_healthy_quote`。

真实名称请求为 `https://qt.gtimg.cn/q=sh600519`。回调用 URL path 的最后一段提取 symbol，得到 `q=sh600519`；随后 `parse_stock_name` 要求响应是 `v_q=sh600519=...`，与真实 `v_sh600519=...` 永远不匹配。

复现：同一个有效响应 `v_sh600519="1~TestCompany~600519";`，正常名称解析得到 TestCompany，`_healthy_quote` 返回 False。通过实际 RequestCollector 和 `fetch_stock_name` 调用：无冷却时成功；已有腾讯拒绝、冷却到期后同一响应却抛 ProviderBlocked，错误延长整个 tencent 的冷却，影响其他腾讯价格请求。

修复要求：按真实报价 URL 协议提取并校验 sh/sz 股票身份，不带 q=。补充通过 RoutedSession 的有效报价恢复、错误股票响应拒绝两个用例；无效响应仍不能清除冷却。

### S4 · P2：腾讯恢复探测提前拒绝已支持的 qfq day 回退路径

位置：`quarterly_dashboard/stock_collection.py` 的 tencent JSON 健康分支（调用 `parse_price_history(payload,symbol,adjustment)`），对照 `sources.py:fetch_price_history` 中 qfqday 缺失时的 raw/hfq 同窗核对。

既有价格采集允许 qfq 请求返回 day：必须再发 raw 和 hfq 请求、核对同窗价格一致，才标记 verified-unadjusted 并作为适用前复权数据。新健康回调直接以 adjustment=qfq 调用只接受 qfqday 的底层解析器，第一步 day 响应就被判无效，根本到不了原有安全核对步骤。

复现：相同 code=0、指定股票 day 有效 OHLC 行，未冷却时 `fetch_price_history(...,'qfq')` 实际调用三次并成功返回 `source_basis=tencent-current-ohlc-verified-unadjusted`；冷却到期时第一次响应就 ProviderBlocked，后续核对不可达。此前能恢复的有效来源分支被改坏。

修复要求：设计兼容既有 raw/hfq 核对的恢复流程，并确保核对请求全部经过公共预算。不能仅为了通过测试无条件接受所有 day 或降低无效响应停止要求；需要明确何时认为来源可恢复、核对失败时如何继续阻断。补充 qfqday 直接有效、day＋raw/hfq一致有效、错误股票／不一致／结构无效均失败的完整入口回归。

当前验收：已修复的原场景与已通过的组合测试予以确认；S3/S4 修正并复测前不关闭整体采集验收。进度首部的“已部署”是执行事实，与“最终审查通过”分开表达。

## S3/S4 修正执行记录 · 2026-10-14

- S3 已按腾讯实际 URL 的 path 格式读取 `q=` symbol，并复用原简称解析器；测试覆盖正确 symbol 可恢复、错误 symbol 仍阻断。
- S4 不把 `day` 响应直接当作已恢复：qfqday 路径按原解析器验证；day 回退路径进入带 token 的待验证状态，raw／hfq 两个同窗请求均经 RequestCollector 计预算，价格字段与窗口一致后才解除冷却。错误或不一致延长冷却；其他 collector 不能抢先清除状态，遗留待验证状态超时后可重新探测。
- 实际业务解析入口隔离回归覆盖 qfqday 直返、day＋raw/hfq 一致、基准不一致、错误股票及有效名称／非法名称；Sina 有效页、东财有效分页与明确 9201 无数据分支也分别验证。
- 组合测试：**258 passed，3 deselected**；`compileall`、`git diff --check` 通过。没有真实来源请求。
- 已核对旧 PID 39132 为项目后台后重启，当前 PID 31336；启动日志正常，首页与维护只读 API 均 HTTP 200，stderr 为空。部署记录见 `PROGRESS.md`。
- 本次组合回归未运行 3 项 live HTTP 用例，也未验证真实来源覆盖；这两项仍是最终验收限制。
