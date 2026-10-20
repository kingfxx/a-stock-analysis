# 决策

- 只降低 Legu 并发；中证、申万继续使用同源上限 2，来源之间仍并行。
- `RequestCollector.policy_for('legu', index_parallel=True)` 返回上限 1；现有共享调度因此独占同源两个 OS 槽位，实现跨采集器/进程串行。
- Legu 工作池读取同一策略，避免线程池与实际 HTTP 限额不一致。运行报告增加 `index_legu`，明确披露该例外。
- 不将单并发视为不再触发限流的保证，继续保留 429 停止和冷却。
