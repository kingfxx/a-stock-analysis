# 进度

- 已核对现有 Legu 单并发及共享调度，修改策略与针对性测试。
- 已同步采集规则及指数采集说明。
- 三个相关测试文件共 56 项通过（35.29 秒），使用 `tmp/legu_spacing_tests_20261017` 隔离数据库；验证 3 秒下限、7 秒上限、每 20 次额外休息及单并发，其他来源回归通过。测试目录已清理。
- 确认无运行中指数任务及公共批次，核对宿主机旧 PID 3076 为本项目 `app.py --port 8765` 后隐藏重启，新 PID 13684。首页及 `/api/indices/status` 均 HTTP 200，后台继续运行。
- 当前策略核对为 `index_legu`：interval=3、jitter=4、max_concurrency=1。启动及访问日志正常，保留在 `data/verification/logs/legu-spacing-8765-20261017.stdout.log` 和 `.stderr.log`。
- 本轮文件 `git diff --check` 通过；没有发起真实采集、清除冷却或改写历史批次。工作区其他既有修改未触碰。
