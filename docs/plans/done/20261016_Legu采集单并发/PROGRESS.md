# 进度

- 已核对用户授权、采集规则和共享调度的跨进程槽位机制。
- 已修改 Legu 工作池、共享策略和策略报告；新增共享调度串行及 Legu 工作项串行验证。
- 已同步采集规则与指数采集说明，明确 Legu 单并发及其余速度参数。
- `python -m pytest tests/test_index_parallel_collection.py tests/test_index_parallel_review.py tests/test_index_collection.py -q --basetemp tmp/legu_serial_tests_20261016`：55 passed。新增测试验证独立采集器实际 HTTP 峰值 1、完成后至少 2 秒间隔、PE/PB 工作项串行；原有中证并发 2 和跨来源并行回归通过。
- 核对旧 PID 23992 为本项目 `python -u app.py --port 8765`，且无运行中指数操作和公共采集批次，已在宿主机隐藏重启；新 PID 33000。首页及 `/api/indices/status` 均 HTTP 200，后台保持运行。
- 当前日志保留于 `data/verification/logs/legu-serial-8765-20261016.stdout.log` 和 `.stderr.log`；首次首页探测超时导致连接中断日志，随后重试成功。
- 已清理本轮隔离测试目录，`git diff --check` 通过。没有启动真实集采、清除预算或改写历史批次。
