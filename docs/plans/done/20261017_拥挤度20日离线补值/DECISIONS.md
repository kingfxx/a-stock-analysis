# 决策

- 无联网请求，不重新访问BigQuant或Legu，不改变每日HTML采集。
- 本地三季度与Legu45个重叠日期比较：全市场有效amount数量、前5%人数向上取整的算法平均绝对误差0.00975个百分点、最大0.07127个百分点。该样本支持近似补值，不能称为Legu原值或证明所有口径完全一致。
- 以当日非空amount证券为样本（沪深北），N取有效amount个数、K=ceil(N/20)，成交额降序取K只，拥挤度=前K成交额/有效amount总额。空amount排除并记录，不能补零；零amount保留为有效数据。
- 记录未四舍五入的比例和亿元金额，界面仍显示百分数两位；close取index_price_series=37的中证上证指数日频实际close，逐日保留价格事实ID/version/source/batch。
- 补值provider=bigquant，channel=bigquant_calculated，definition_version=amount_rank_top5_v1；仍用既有单表，不新增表或改schema。
- 计算证据通过公共来源归档登记为LOCAL_DERIVATION，明确不是新外部请求；包含原始行情/独立计数及点位引用。
- 展示优先级网页有效值、API有效值、自算补值。API/HTML身份和历史观察原样保留；仅自算有效值填补已知空位。
