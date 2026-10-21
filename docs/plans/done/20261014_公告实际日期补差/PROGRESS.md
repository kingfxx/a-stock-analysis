# 进展

已读取采集规则和现有实现，开始取消固定90日窗口并补充隔离验证。

执行完成：增量起点采用完整发布且分类 scope 为当前分类子集的最新公告实际 published_on，含当天；无适用公告记录时使用已成功查询终点。显式 start、full、首次、扩类和十年下限保留。data_watermark 改为真实适用公告日期（空目录为 null），coverage_end 仍为查询终点。

发现并修复同日重取未变化公告导致验证标记丢失的问题：只在完整保存字段一致时保留 _previously_validated，保证增量结果与日期判断幂等。文档已取消固定90日回看，明确历史内部修订需 full 或定向 start。

隔离测试：service、server integration、disclosure 共28项通过；根代理更广38项通过。测试覆盖长期无公告、同日补漏、空目录、幂等、实际水位、显式 start、full、扩类和不相关分类。执行阶段未采集真实来源，未改正式数据库或后台；根代理接续审查和重启。

Sol审查及部署完成：核对实际日期起点、分类来源过滤、未变化验证标记及实际data_watermark。8765身份核对后自动重启，当前进程18192，启动日志结构版本33、页面HTTP 200，沪电2020Q4披露日2021-03-25/前复权13.20保持正确。正式库仅只读核对样本最新公告日期：002463为2026-08-26、601919为2026-08-29；没有执行新的公告网络采集。报告在 `data/verification/reports/announcement_actual_date_acceptance_20261014.json`，日志在 `data/verification/logs/daily-8765-actual-date-20261014.out.log` 与同名err.log。隔离测试目录.ad已清理，任务完成。

2026-10-21 后续修复完成（单代理）：近十年外的旧 pending/missing 不再触发汇总警告，范围内缺口提示数量与报告期；不删除已有历史结果。普通增量不因财务新增更早报告期扩大网络范围，本地只对新增/变化证据关联期及覆盖中的新报告期重判，同日未变公告复用结果；full/start保留指定范围审计。新增4项回归并更新旧期测试，共42项通过；另1项服务器集成测试关于 allow_initial 的断言失败，用 HEAD 原始服务复跑同样失败，未修改无关行为。正式库只读核对601919、600900警告消失且原82/96条日期记录保留，601728近十年实际未确认仍提示；未执行公告补采或历史迁移。8765核对进程身份后重启为19104，页面HTTP200。验收报告 `data/verification/reports/announcement_incremental_20261021.json`，后台日志 `data/verification/logs/announcement-incremental-8765.out.log` / `.err.log`。
