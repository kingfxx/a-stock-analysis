# 进度

- 确认主研究页面和 `/indices` 使用不同 HTML；指数页独立复制了股票选择器及研究导航。
- 已抽取 `web/research-sidebar.html`，由服务端分别注入股票研究页的 ARIA tabs 及指数页链接导航；股票选择器和页面标题区域使用同一份结构。
- 已抽取 `web/research-layout.css`，两页共用同一侧栏宽度、间距、选择器和窄屏布局。
- `python -m py_compile quarterly_dashboard/server.py` 和共享模板渲染断言通过。
- 已按身份核验重启8765；`/indices`、主营构成主页面和共享 CSS 均返回200。浏览器中确认指数页出现“主营构成”，点击后打开主营构成视图。
