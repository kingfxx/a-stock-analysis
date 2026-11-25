# 首批文件职责拆分

本批在 Python 与前端目录迁移均验收后进行，没有改动业务算法或前端内容。

| 原职责 | 新归属 | 保留的边界 |
|---|---|---|
| server 的监听器、调度器、恢复重载循环 | stock_analysis/runtime.py | server.serve 负责传入 Handler、初始化、状态清理及服务组装回调；runtime 不导入 HTTP Handler |
| server 公共侧栏 HTML | http/rendering.py | 以模板和页面类型为输入；server 保留原调用入口 |
| storage v32 财务来源升级 | domains/financials/schema_upgrade.py | 在原升级版本分支内延迟加载，SQL 不变 |
| storage v37 指数价格升级 | domains/indices/schema_upgrade.py | 在原升级版本分支内延迟加载，SQL 不变 |
| valuation 的纯计算 | domains/valuation/calculations.py | 无 requests/sqlite3 依赖；valuation.py 保留来源请求和原导出 |
| update_service 的共享状态、审计周期、锁和备份辅助 | domains/stocks/sync_support.py | 生产调用改为直接引用；ChipService 留在 update_service，原导出保留 |

估值计算、同步辅助及两个领域升级模块共提取函数的函数体已与阶段二提交逐一比较 AST，算法保持。证据见 data/verification/reports/structure-extraction-functions.json。同步测试的假时钟同时覆盖拆出后的模块，断言保持。

运行循环仍保持单实例锁先于绑定、初始化完成后服务、唯一调度器、异常关闭、恢复前连接排空与恢复后清理缓存；现有启动/恢复测试覆盖这些行为。

## 后续按需求细化

server 内的复杂数据组装和剩余路由、sources 中请求与解析的进一步分离，以及 index.html 内嵌前端代码，仍可逐项提取。它们不影响本次目录归属，不在一个提交中整文件重写；沿用当前计划追加具体接口与验证范围。
