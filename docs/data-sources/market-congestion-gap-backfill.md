# 大盘拥挤度20日离线补值

2026-10-17 按用户授权，使用本地 BigQuant `cn_stock_real_bar1d` 计算 **2026-07-31 至 2026-08-27 的20个缺值交易日**，以本地点数库上证指数同日收盘点位作为 `close`，一次事务写入既有 `market_indicator_observations`。

本次已完成：新增20条独立自算观察，修订0条；Legu原始记录保留。有效日期由3642增至3662，已返回日期中的已知缺值由20降至0。历史证券名单和来源未返回日期的覆盖仍未独立核实，不宣称全历史覆盖率100%。

## 输入与计算口径

- 行情：[20日下载清单](../../data/collection_sources/bigquant/cn_stock_real_bar1d_20260731_20260827/manifest.json)，110861行、16字段；本次使用 `date/instrument/amount/volume`。本地Parquet与公共Arrow来源2892逐值一致，逐日独立计数来源2890及文件双哈希通过。
- 点位：`index_price_series.id=37`，`cn:000001 / csindex / history.points / daily / unspecified / v1`，20日全部有同日实际close。没有新增或修订价格事实，没有使用股票close或跨日期替代。
- 样本含沪深北A股，按当日非空 `amount` 的证券数记作N，取K=`ceil(N/20)`。金额降序排列，取前K只；相同金额按证券代码排序，结果可复算。
- `value = 前K只amount之和 / 当日有效amount之和`；两类金额除以1e8转为亿元。value及金额不提前四舍五入，界面百分比显示两位。
- 零成交额是有效记录，计入N；空成交额排除，不补零。20日共有90条空amount，均对应volume=0。出现空amount但volume不为零时停止计算，避免忽略实际成交导致分母失真。
- 日期/证券重复、未知A股代码、负数/非有限金额、零总额、同日点位缺失均拒绝发布。

本地三季度归档与Legu有值日期进行45日重叠核对：该算法平均绝对差约 **0.00975个百分点**，最大约 **0.07127个百分点**。这是独立近似补值，不等同于Legu原值；金额直接按BigQuant字段汇总，未通过拟合或调整金额强行匹配。完整核对见 [公式重叠报告](../../data/verification/reports/20261017_congestion_formula_overlap.json)。

## 存储与追溯

完整身份为：

```text
indicator_key:       ashares_congestion
scope_key:           all_a
provider:            bigquant
channel:             bigquant_calculated
frequency:           daily
definition_version: amount_rank_top5_v1
```

未新增表或改schema。四个数值字段均保存：`value / close / top5_amount_yi / total_amount_yi`；`precision_digits=NULL`表示没有声称来源固定的小数精度。`value_status=observed`表示该独立身份有有效数值，不表示它是Legu实测值。

公共批次 **1367**、来源 **2926**，数据集 `market.congestion.bigquant_calculated`，方法明确为 `LOCAL_DERIVATION`、`network_request=false`，哈希依据 `derived_json / gzip_file`。这是本地衍生证据，没有发起新SDK或HTML请求，没有消耗来源请求预算。

归档计算证据保留20条结果、逐日N/K及空值数、前K证券清单、行情/独立计数原来源和双哈希、上证点位每条事实的ID/version/source/batch/content_hash。补值source的obtained_at是本次计算证据生成时间；原行情取得时间另在上游引用中保留，updated_at是实际发布时刻。

| 材料 | 位置 |
|---|---|
| 整理后的计算资料 | [20261017_congestion_gap_prepared.json](../../data/verification/reports/20261017_congestion_gap_prepared.json) |
| 写入及审计报告 | [20261017_congestion_gap_backfill.json](../../data/verification/reports/20261017_congestion_gap_backfill.json) |
| 写入前审计摘要 | [20261017_congestion_gap_before.json](../../data/verification/reports/20261017_congestion_gap_before.json) |
| 写入前原库备份 | `data/backups/before_congestion_gap_20261017_225707.sqlite3` |
| 实现 | [market_congestion_backfill.py](../../quarterly_dashboard/market_congestion_backfill.py) |

补值首末日示例：2026-07-31拥挤度51.3648140694%，close=3832.26；2026-08-27拥挤度48.3042204425%，close=3956.57。

## 发布与展示规则

先核验本地原文和点位，生成计算证据，再备份原库和归档证据，最后在**一个事务**内发布20条，并完成批次。事务内再次核对点位事实仍为当前版本，防止整理后被修订。

只允许填空：任一目标日期已有Legu有效值即拒绝整段写入；已有自算值不同也拒绝改写。同样输入重复执行返回 `already_present`，不创建重复事实/来源/备份。所有Legu原始空值观察和版本均保留；以后取得Legu有效值时，展示自动优先原值，不撤销或冒改自算历史。

趋势图只显示一条拥挤度曲线，按“网页有效值 → API有效值 → 自算补值”取值。`上证指数-legu`选项也仅在这20日使用补值观察的close填空，其他日期仍取原Legu配套close；折叠说明提示补值来自BigQuant计算、close来自本地中证上证日频。

API保留原始三条独立通道，并增加 `supplemented_dates`（实际由自算填空的日期）、`source_missing_dates`（Legu原始缺值日期）。原Legu API/HTML采集、每天检查日程及会员缺值处理不变；不自动为其他日期启动自算。

## 验收

21项隔离测试及前端采样测试通过，覆盖取整、零/空值、异常金额、重复键、同日点位、来源身份、只填空、幂等、单事务回滚、点位修订拒绝、API和曲线优先级。宿主机8766端口本地连接超时，按项目规则改用实际可访问的8768等空闲测试端口，未修改主机网络设置。

真实写入后核验：20条四项数值与准备结果一致，close逐日匹配原点数事实；原3692条Legu观察整体哈希未变，指数点数记录数及Legu/BigQuant共享预算未变；计算证据双哈希、来源复合外键通过，版本均为1。业务库写入前已有完整备份，资料迁移仍须同时复制来源归档。
