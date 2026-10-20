# BigQuant缺口日期全字段下载

2026-10-17，用户授权只下载2026-07-31—2026-08-27缺失的20个交易日全市场全部字段。使用cn_stock_real_bar1d的SELECT *，仅按日期筛选，无股票名单/交易所过滤。主机端通过现有BigQuantSDK/RequestCollector共享预算、传输归档、拒绝停止与冷却。

一份全范围查询加一份逐日COUNT/COUNT DISTINCT/字段有效数查询，核对返回日期、代码唯一及总数/逐日数量；不计算拥挤度，不写业务事实。保存Parquet全字段原类型、schema/请求/来源引用与SHA256清单到data/collection_sources/bigquant/cn_stock_real_bar1d_20260731_20260827/。可复用范围与哈希一致的既有缓存，不重复下载。

全部传输证据继续使用现有SDK公共dataset，operation及SQL区分本次用途；业务表不新增，不把请求完成当作历史证券名单全覆盖证明。校验完成保存独立报告，原始文件与SQLite一起迁移。
