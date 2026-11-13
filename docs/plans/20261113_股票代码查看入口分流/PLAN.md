# 股票代码「查看」入口分流

## 目标

首页顶部提交股票代码时，已有本地个股业务数据就进入该股的完整个股页。缺少个股业务数据时，在当前页打开该代码的「公司快览 · 本地数据」浮层，默认 TTM。点击「查看」和输入框回车共用这条路径。

## 现有调用链

- `web/pages/index.html` 的 `.search` 表单直接 GET `/?code=…`。`http/routes/pages.py::_get_home` 经 `http/rendering.py` 调用 `data_service.py::page_payload`，把个股缓存和采集状态嵌入页面。页面启动时若 `needs_initial` 为真，会提交首次采集 POST。
- `http/routes/stocks.py::_get_stock_collection` 已提供只读状态接口。`collection_operations.py::page_status` 中的 `needs_initial` 表示是否允许首次采集，受既往同步及任务记录影响。
- `web/features/industries/industry.js` 从行业列表调用 `openIndustryCompanyPreview(code, list, mode, trigger)`。首页已加载该组件及依赖。`company-preview.js` 请求 `/api/industry-company-preview`，由 `domains/industries/company_preview.py` 读取全市场本地数据。

## 实施步骤

1. 在个股领域提供独立的数据存在性判断，复用现有个股事实查询及只读数据库句柄。按实际发布的财务、主营、行情、估值、分红、股东人数、融资融券或公告等个股业务记录判断，有一类即可。实施前逐项对照现行读取入口，覆盖仍可展示的旧版本数据及旧筹码文件回退。名称、关注关系、采集状态和全市场快览记录不计入。现有首次采集判定继续按原口径执行。
2. 在现有 `GET /api/stock-collection?code=…` 加入显式 `lookup=1` 查询分支，返回归一化代码与 `has_stock_data`。采集状态轮询仍使用原响应，顶部查看无需查任务历史。判定由个股领域负责，HTTP 层转发结果。用按个股索引的存在性查询，命中即结束，避免加载完整个股图表。SQLite 事实读取沿用只读快照。无需新增表、常驻缓存、锁或调度器。
3. 在首页顶部表单的 `submit` 事件里按提交的代码查询状态。有数据则走原 `/?code=…` 导航；无数据则传入该代码和单公司列表，复用 `openIndustryCompanyPreview`。查询期间显示状态并防止重复提交或过期响应打开错股。失败留在当前页并显示错误。快览请求成功后用返回的公司名称更新占位标题，单公司场景隐藏「上一家／下一家」及列表序号。关闭浮层后焦点回到表单，继续使用快览已有的「完整个股展示」确认入口。
4. 实施后同步 `docs/stock-collection.md` 和 `docs/enterprise-trend-metrics.md` 的入口说明。变更涉及首页、个股领域状态查询和快览外壳。行业列表入口、图表取数和绘制、股票库导航及手动／全量刷新继续走现有调用链。

## 验收

| 条件 | 预期 |
|---|---|
| 有任一类个股数据，包括部分历史数据 | 点击或回车后进入所查代码的完整个股页 |
| 只有全市场数据、股票资料空壳、失败或无数据的同步历史 | 打开该代码的公司快览，期间没有个股采集 POST、建档或来源请求 |
| 两处都无可用数据 | 快览显示现有空态，可关闭，可经「完整个股展示」确认后进入个股页 |
| 代码不合法、状态接口失败、查询期间改码或快速重复提交 | 页面给出相应反馈，过期响应不跳转、不打开错股，也不启动采集 |
| 从行业列表打开快览；从明确入口打开个股页 | 原列表切换、关闭回焦和首次初始化路径正常 |

用隔离库验证状态接口和数据判定，重点覆盖只有同步历史和只有全市场数据的反例。定向回归 `tests/test_stock_collection_management.py` 的首次采集用例和 `tests/test_company_preview.py` 的只读边界，并用浏览器走表单两条分支。实施通过后按项目规则核对宿主机 8765 的进程和活动任务，安全重启并验证日志及页面。
