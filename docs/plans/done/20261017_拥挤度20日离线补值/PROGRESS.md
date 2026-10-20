# 进度

- 已读取项目及采集规则，确认原库20个日期仍缺值，已有中证上证日频点数完整覆盖20日。
- 本地20日行情110861行，90个amount空值均对应volume=0，未补零；无负成交额或重复键。
- 完成45个本地重叠日期的12种范围/取整组合核对，报告data/verification/reports/20261017_congestion_formula_overlap.json。
- 已实现独立自算身份与离线准备/发布工具；前端仅按日期补空，Legu有效值优先，补值说明折叠展示。
- 21项隔离测试及Node采样测试通过。宿主机8766端口实测回环连接超时，8768/18760正常；测试入口改用可访问空闲端口，没有更改网络配置。过早在隔离环境启动的浏览器测试已中止，不以该环境的连接失败判断数据源状态。
- 2026-10-17一次事务写入20条，source=2926、batch=1367，inserted=20/revised=0；备份data/backups/before_congestion_gap_20261017_225707.sqlite3（1332404224字节）。来源标记LOCAL_DERIVATION、derived_json/gzip_file。
- 真实审计通过：四项数值逐日匹配准备资料与上证点位事实，Legu3692条原记录整体哈希未变，原指数点数及bigquant/legu提供方状态未变；来源双哈希/复合外键正常，20条补值version=1。
- 当前已知3662个日期均有有效值，缺口0；其中Legu原有3642日、自算补20日，源空值和未知全历史覆盖原样保留。
- 已自动重启主机8765，PID32656；真实API HTTP200，宿主机浏览器逐日核对20个图中拥挤度及“上证指数-legu”对应close均一致，无页面错误或外部请求，后台保持运行。
- 文档docs/data-sources/market-congestion-gap-backfill.md、集采说明及SQLite存储说明已同步；正式报告data/verification/reports/20261017_congestion_gap_backfill.json及UI验收报告20261017_congestion_gap_ui.json。
