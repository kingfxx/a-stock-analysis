# 进度

- 已完成20日BigQuant分母回退，Legu有效总额优先；中证amount_ratio与申万amount直接更新原行，实际公式及来源在紧凑溯源中可还原。
- 35项点位及读取测试通过，覆盖BigQuant/Legu优先级、范围限制、源修订、撤回、幂等、紧凑还原与不新增行情行。
- 已先备份日常库，补算320条宽基占比、3240条行业成交额，覆盖2026-07-31至08-27共20个交易日；行情总数353116不变，原始字段与市场事实哈希不变。无新批次／来源／网络请求，重复补算修改0行。
- 审计报告：data/verification/reports/20261018_index_bigquant_gap_recompute.json，维护工具：data/verification/tools/index_bigquant_gap_recompute.py。
- 已核对进程后重启8765；宿主机Edge及原库接口核验20天全部有值、无脚本错误，断线已消除。后台保持运行，日志在data/verification/logs/dashboard-8765-bigquant-gap-20261018*.log。
