# 前端资源导航

| 目录 | 内容 |
|---|---|
| `pages/` | HTML 页面入口 |
| `shared/` | 公共侧栏、选股、日期处理和布局 |
| `features/` | 按财务、估值、指数、行业、市场、笔记、研究和维护功能组织的 JS／CSS |
| `features/research/sectors/shipping/` | 航运研究前端 |
| `vendor/` | 第三方资源及原许可证说明 |
| `samples/` | 现有示例资源 |

磁盘路径与浏览器 URL 分开。例如 `/dcf.js` 仍指向 `features/valuation/dcf.js`，`/indices` 仍由后端加载 `pages/indices.html`。

`asset-map.json` 显式记录原逻辑资源名到磁盘位置，Python 通过 `stock_analysis.http.assets.asset_path()` 读取。新增对外资源时同时更新映射及后端相应 HTTP 白名单；放进目录或加入映射不会自动公开文件。HTML 页面中的 API、资源 URL 与 DOM 契约保持现有行为。

本次仅迁移资源位置，未重写 HTML、JS、CSS，未增加构建工具或前端框架。
