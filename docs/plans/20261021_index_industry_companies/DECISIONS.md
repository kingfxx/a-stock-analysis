# 决策

- 按最新反馈去掉跳转图标；指数、个股入口共用 research-layout.css 中的 industry-navigation 按钮样式，青色实底、深色文字。
- 使用 catalog 的 navigation_taxonomy_code，不把指数代码当作行业分类代码，也不在前端猜测映射。未匹配时禁用并说明原因。
- 入口参数 tab=industry、industry、level、industry_view=companies；合法 stock 参数转为 code，保留个股上下文。
- 行业数据首次渲染后激活公司标签，并定位到标签栏；仅执行一次，不干扰后续筛选。
- 行业页默认显式请求 industry=all；个股按钮才显式请求 industry=stock，由本地成员分类解析三级行业，分类缺失回全市场。
- 全市场为运行时虚拟范围，不入分类表；公司和财务汇总覆盖本地已导入成员，市值沿用现有季度名单与来源规则，不将缺值补零。
- sessionStorage 的 investment-industry-selection-v1 独立保存行业与排行层级。跳转参数优先，完成后移除一次性行业参数，避免刷新覆盖手动新选择。排行层级切换不重置已选范围。
- 全市场性能修复：指标按钮用 Plotly.update 仅更新三条 trace 的 visible、左右轴显隐及空图／单点提示，不调用整页 render。保留现有数值、悬浮和图表口径。
- 公司表每页 100 家，先全量过滤／排序再切页，匹配数和总公司数分别保留；搜索、排序及新数据加载回第一页。切回已加载行业标签仅触发尺寸调整。
- 指数选择独立保存至 sessionStorage 的 investment-index-selection-v1，仅存 kind／code。在目录首次加载后校验恢复；行业父子关系从现有 catalog 读取，不保存冗余关系。无记忆、失效代码或损坏 JSON 不阻塞页面，沿用沪深300默认。
- 行业指数筛选栏移除固定的「数据来源：申万宏源」文字，宽基／策略原估值口径选择保留，底层来源记录不变。
- 个股普通估值内容由 valuation-standard 容器统一管理显隐，DCF 展开隐藏整块，防止异步估值刷新或局部 display 样式让旧指标重新露出。切回时发出 resize 以恢复图表尺寸。移除重复指标提示，状态字号统一 20px；不改指标公式或阈值。
