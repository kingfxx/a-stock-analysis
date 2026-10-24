# 验收记录 · 2026-10-25

实施与验收完成，原8765保持运行。单代理，未新增常驻服务、未执行真实回填、未启用新增定时。

## 测试

最终相关测试命令覆盖collection_management、collection_management_ui、market_congestion、storage_startup、maintenance、industry_progress、industry_collection_review、industry_parallel_collection、market_trade_snapshot、index_collection、collection_provider_pacing、collection_entrypoints、database_restore、industry_memberships；使用隔离临时数据库，HTTP/浏览器在宿主机显式可用8768/8766等端口运行并关闭。

结果：**164 passed，3 deselected，44.29s**。另有node --check web/maintenance.js、compileall、git diff --check通过。新增23项核心及2项HTTP/浏览器测试包括：

- 默认tab顺序/ARIA/方向键，首次预览才加载、重复切换复用、明确刷新；隐藏页清理事件与轮询不扫描；表单编辑与展开详情不被轮询丢失。
- 参数白名单、同源写入限制、纯读取/保存配置不启动采集、按需完整结果。
- 旧/新入口领域互斥、真正两个Python进程争领同槽位；关闭自动不取消现有运行。
- 日/周/月、月末跨年、发布窗口、明确完整覆盖停止重复检查，未知覆盖继续；保存后未来槽位、漏跑合并、冷却与最多一次崩溃恢复。
- 拥挤度旧配置不重写，原daily调度复用；内部安全停机及RequestGate门禁；公共batch保留真实scheduled来源。
- 维护表登记、空表真实时间及代表性大小/读写。

下列原有失败在HEAD原始代码隔离副本重跑同样失败，未修改无关实现或断言：

1. test_browser_polling_preserves_zoom_and_filter_resets：闲置请求数受既有投资笔记读取影响。
2. test_browser_dual_axes_all_broad_ranges_and_read_only：旧断言day，当前已有默认auto。
3. test_old_schema_upgraded_in_copy_and_original_unchanged：旧断言新增定义表0行，实际预置5行。

## 原库部署与页面

原监听PID37744，核对python app.py --port 8765且无活动任务后重启；最终PID38332。原存储迁移自动保留迁移前备份（data/backups/pre-migration-20261024T162236188134Z.sqlite3）。schema40启动、恢复0中断任务，无异常日志。

拥挤度启用、Asia/Shanghai 18:00、原updated_at均保持。其他新增定时未启用。只读页面验收阻断来源更新与POST，不调用模型或启动回填。

真实页面默认采集管理；数据库预览默认请求0，任务刷新请求0，隐藏页清理额外扫描0，页面JS错误0。点击数据库预览的请求在原库验收时被拦截，实际扫描及缓存刷新已在隔离库验证，未因验收扫描大库。

原库任务列表由完整历史的12125099字节缩为76160字节；完整结果点击才读取。最终单次API约47ms，机器与规模相关。

## 可复核资料

- [代表性存储与查询计划](../../../data/verification/reports/collection_management_20261025.json)
- [原库部署前配置/活动任务快照](../../../data/verification/reports/collection_management_deploy_before.json)
- [原8765只读页面验收](../../../data/verification/reports/collection_management_live_20261025.json)
- 常用复测工具data/verification/tools/collection_management_benchmark.py与collection_management_live_check.py。
- 当前后台日志data/verification/logs/collection_management_8765.stdout.log、stderr.log。

1000槽位含索引258048字节（258.048字节/行）；一次写入1000领域记录+槽位约17ms，列表常用读取5—7ms。单行规则占两页8192字节是分配下限，不作为稳态平均行占用。

功能限制：行业用户停止暂不开放；财务／快照自动目标需用户明确填写及更新；最近历史与分段不是全历史完整性审计。航运采集与预测未实现于本任务。

## 页面反馈修正验收

2026-10-25追加三项用户反馈：共享进度界面、任务专属确认、国债真实来源及历史归属。

测试：collection_management、collection_management_ui、index_progress、index_progress_review、bond_yields及原指数页进度展开/轮询浏览器回归，**67 passed，39.67s**。新增测试核对42/42总进度与18/18、24/24分组、指数确认不出现财务/快照警告、历史模式才有长耗时提示、国债东方财富确认、取消确认不启动采集、独立/联合历史分离、国债调度忽略Legu冷却并遵守Eastmoney冷却。

原8765无活动任务后自动重启，最终PID40852，schema40及原拥挤度配置不变。真实页面使用两个页面共用的collection-progress.js/css；原库最新指数摘要42/42、成功6、跳过36，界面不默认展示进度JSON。国债标识东方财富、独立历史不继承联合Legu错误。原指数增量按钮联动国债的既有逻辑保留，管理页两个独立入口说明明确。

更新的只读live报告仍位于data/verification/reports/collection_management_live_20261025.json。默认/刷新无storage请求、隐藏页无新增扫描、页面无JS错误，来源更新和运行确认均拦截/取消。原库不执行真实回填。临时截图/测试库已清理，后台保持运行。

## 最新交互与统一补跑验收

用户要求的最新记录单次展示、公共分段默认折叠、提供方下拉多选已实现；拥挤度按用户明确要求改为指数相同的最近到期补跑规则，旧“次日上午等当天18:00”说明作废。此前保留原每日语义的记录仅为当时实现历史。

隔离相关回归 **84 passed，2 deselected，45.48s**。新增假时钟覆盖18:00到期后22:00/次日09:00启动、同槽位去重、拥挤度跨午夜成功HTML复用及保存后未来槽位；浏览器覆盖最新完成/进行中默认展开仅一次、手动折叠及公共分段展开在刷新保留、手动/自动来源下拉独立选择、键盘和Esc/外部关闭、表单及轮询保持。

原8765无活动任务后自动重启，最终PID24688。实际页面：最新运行正常时间戳行内展开、公共分段默认折叠、两个来源下拉正常、JS错误0、默认与刷新storage请求0。用户自己启用的指数/国债21:00及拥挤度18:00完整保留（包括真实updated_at）。部署前快照data/verification/reports/collection_management_unified_before.json，live报告沿用现有文件更新。

检查槽位与来源实际数据日期分开，补跑按来源水位和已发布资料更新，不伪造前一天数据。未新增表、未手动触发真实回填，原已授权定时按统一规则运行。临时库/截图已清理，后台及当前日志保留。

## 菜单裁切、四期目标与业务入口移除验收

相关检查 **87 passed，2 deselected，55.45s**。2项为此前已在HEAD复现的无关拥挤度图表基线失败；没有声称全仓库无失败。

- 修复提供方DOM三项但显示不全的根因：主页.panel的overflow:hidden裁切绝对定位菜单。采集卡片允许菜单溢出，根据视口上下/水平空间调整方向，保留滚动上限。浏览器复现该父样式并验证每项完整可见、三项可点击，手动/定时选择独立。
- 最近四个已结束季度+手填，手动默认最新，不被最近历史旧报告期覆盖；2026年10月为2026-09-30/2026Q3。定时原有目标保留，切换预设/手填后提交正确值，轮询不丢输入。
- 指数、拥挤度、行业旧页启动/停止/定时按钮移除；保留图表、只读进度及采集管理导航。原域采集器/接口及数据历史保留，根AGENTS登记批量入口唯一约束。
- 用户明确个股尚未决定，要求先分析；个股刷新/全量刷新/到期检查暂保留，方案见INDIVIDUAL_REFRESH_OPTIONS.md，未将建议伪报实现。

原8765无活动任务后自动重启，最终PID30504，schema40未变；主页模板及各页面实际访问正常，JS错误0。用户已保存的指数/国债21:00、拥挤度18:00与updated_at原样保留；快照data/verification/reports/collection_management_centralized_before.json，live报告更新了未裁切菜单、四期默认及无旧业务按钮检查。验收浏览器拦截来源更新并取消运行确认，未手动执行真实回填。

本次临时库/截图清理，保留当前后台、日志、报告及复测工具。
