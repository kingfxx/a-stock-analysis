# 个股主营构成采集设计

状态：2026-10-16协作实施、主代理审查、隔离测试、日常库迁移及两样本真实验收完成。8765宿主后台运行中；本轮未实施图表及跨期分类映射。

## 目标与范围

- 只处理用户访问／明确刷新的个股，不建立全市场名单、扫描任务或定时任务。
- 数据源为 `https://datacenter.eastmoney.com/securities/api/data/v1/get`，`reportName=RPT_F10_FN_MAINOP`、`columns=ALL`，按唯一 `SECUCODE` 查询。
- 首次、显式 full、周期审计获取上海当天前十个日历年至当天的实际报告期；下限含当天，闰日回退到目标年份二月末。例如 2026-10-16 的范围为 2016-10-16 至 2026-10-16。范围过滤对象为 REPORT_DATE，不是取得时间。
- 不使用固定返回 200 条的 BusinessAnalysis/PageAjax 或当前 AKShare stock_zygc_em 作为历史采集入口。
- 本轮设计不改财务三表口径、行业数据、Checklist、模型或正式 PDF 链；暂不规划图表和跨公司产品归类。

## 现有实现依据

- `quarterly_dashboard/fundamental_service.py`：财务数据集独立 SyncKey，24 小时检查；增量覆盖最近两个实际报告期；首次、full、审计走全量，跨页复核首页，丢失既有字段时拒绝发布。
- `quarterly_dashboard/update_service.py`：financial 审计周期 180 天，自动失败抑制 600 秒，手动刷新不能绕过来源冷却。
- `quarterly_dashboard/stock_collection.py`：begin_stock_batch、routed_stock_requests、metadata_only、stock_fast 和共享 eastmoney 提供方。
- 实施遵守 `docs/agent-rules/collection.md`、`docs/agent-rules/finance-and-reports.md`、`docs/stock-collection.md` 及维护页规则。

## 触发与更新

1. 提供 `read(code)` 与 `update(code, refresh=False, full=False)`；read 只读本地。接入个股既有财务采集操作，在财务子任务完成后独立判断主营构成是否到期，不以财务同步成功作为主营构成成功。
2. 财务子任务失败不能阻断已有主营构成缓存读取或独立到期检查；公共 eastmoney 冷却仍可能阻断请求。保持既有财务操作返回结果，不因本模块失败撤销其成功。
3. 无历史成功检查记录、显式 full 或 180 天审计到期，查询完整十年窗口。已有成功空结果也缓存 24 小时，不能每次访问重新建档。
4. 普通自动更新成功后 24 小时内复用结果；refresh 强制检查一次，full 强制十年核对。自动失败后十分钟内抑制重试；手动请求仍遵守提供方冷却。
5. 增量查询起点为当前十年窗口内最近两个已存实际报告期中较早者（含当天），终点为上海今天；只有一期时从该期开始。无窗口内记录时查询十年窗口。所有结果逐报告期完整获取，不能仅取得两条明细。
6. 增量窗口包含晚于主营构成水位的全部日期，因此同一期较晚补齐的成本／地区明细、新出现的一季报和三季报均有机会被获取。窗口之前的修订／补登由十年审计或显式 full 核对，不承诺日常增量发现。
7. 本地财报／公告报告期作为“来源可能尚未更新”的辅助提示，不能强制主营构成每季度存在，不能因新浪水位已最新就跳过东财到期检查。未出现的主营构成报告期保持未知，不填零、不复制旧期、不推进主营构成数据水位。
8. 年报、中报、一季报、三季报均按来源实际记录接受；不推算财报发布日期，不新增公告请求来确定主营构成更新时间，不做单季拆分或 TTM。
9. checked_at、成功查询范围 coverage_start/end、实际最新报告期 data_watermark 分开登记。成功空查询更新检查时间，不伪造实际报告期水位。
10. 十年窗口随上海日期滚动；不自动删除之前已采集事实或修订。默认读取限制当前十年窗口，历史保留数据可由明确历史查询读取。

## 请求、分页与发布

- 默认 pageSize=500，显式 pageNumber；过滤包含该股票 SECUCODE 与报告期上下限。
- 固定排序 `REPORT_DATE,MAINOP_TYPE,RANK,ITEM_NAME,ITEM_CODE`；最后一组同名同码冲突必须被校验发现，不能用来源行序掩盖重复。
- 核验 success、result、count/pages、预期页长、股票身份、日期范围、分类、金额类型、唯一项目及跨页重复；每页显式传 pageSize，不能依赖 RoutedSession 默认容量。
- 所有分页总数保持稳定，跨页后重查首页并比较规范化业务内容；冲突或截断整段失败。查询全部页结束前不发布任一报告期，避免一个报告期分布在多页时留下半期。
- 第一版一次该股票查询窗口为一个发布段；短事务统一提交新版本、明细、sync run、公共 batch 与水位。网络、重试和等待不持写事务。
- 结构有效的空结果可登记 no_data，但不得解释为公司不存在或业务为零；有旧记录的重叠期突然消失，或已有有效字段变空，保留旧事实并报待核实，不能按成功空结果撤销。
- 新增行／原空字段补齐可形成新版本；项目消失、有效值变空或无法识别的分类变化先拒绝自动发布，需定向核实，避免临时缺数覆盖历史。普通有效数值修订允许版本化。
- 新数据集复用 sync_runs/sync_state，不另建任务控制表；来源标识固定 eastmoney。个股链使用 stock_fast=True、metadata_only，共享锁、请求预算、拒绝停止及至少一小时冷却。
- HTTP JSON 仅在内存解析，不保存响应文件、片段、BLOB、整行 raw_json 或原始响应哈希。业务内容哈希不等于原始响应哈希。

## 存储草案

新增两张业务表，复用 instruments、sync_runs、sync_state 和 collection_*。正式迁移号实施时从当时最新版本分配。

### business_segment_versions：公司报告期版本

| 字段 | 设计 |
| --- | --- |
| id | INTEGER PRIMARY KEY |
| instrument_id | FK instruments.id，与现有个股身份一致 |
| source | 固定 eastmoney:RPT_F10_FN_MAINOP，保留来源序列身份 |
| report_date / report_name | ISO 报告期与来源报告名 |
| revision / supersedes_id / is_current | 同一公司、来源、报告期的递增版本、前版引用、当前标记 |
| content_hash / definition_version | 规范化结构化业务内容哈希及定义版本 |
| row_count | 当前版本明细数 |
| run_id / batch_id | FK sync_runs、collection_batches，发布时核验属于同一股票数据集 |
| obtained_at | 形成此业务版本的响应实际取得时间，取参与该版本数据的最后一个有效响应时间 |

唯一约束 `(instrument_id, source, report_date, revision)`；当前版本部分唯一索引 `(instrument_id, source, report_date) WHERE is_current=1`。不得对 content_hash 做跨版本唯一约束：允许 A→B→A 的真实修订。

### business_segment_items：某版本的项目明细

| 字段 | 设计 |
| --- | --- |
| id / version_id | INTEGER PRIMARY KEY、FK business_segment_versions.id |
| dimension_type | 来源 MAINOP_TYPE；1 行业、2 产品、3 地区 |
| item_key / item_code / item_name | 版本内项目键、可空来源 ITEM_CODE、原始名称 |
| revenue / cost / gross_profit | MAIN_BUSINESS_INCOME / MAIN_BUSINESS_COST / MAIN_BUSINESS_RPOFIT |
| revenue_share / cost_share / profit_share / gross_margin | MBI_RATIO / MBC_RATIO / MBR_RATIO / GROSS_RPOFIT_RATIO |
| display_rank | RANK，仅用于排序，不作身份 |
| source_id / batch_id | 公共来源页及批次引用，使用 `(source_id,batch_id)` 复合外键；核验与父版本 batch 一致 |

唯一约束 `(version_id, dimension_type, item_key)`。item_key 使用“非空来源代码＋原始名称”的规范编码；代码缺失用原始名称，空名称失败，不用随机序号消除冲突。仅作为版本内身份，不承诺同码／同名跨期为同一产品。保留名称和代码变化的原貌，不自动跨期映射。

金额与比例均为可空 TEXT 十进制值，解析时用 Decimal 保留来源精度、拒绝 NaN/Infinity，计算后转换为 API 数值；不依赖 SQLite NUMERIC 自动精确计算。SQL 数值筛选或汇总须显式转换并说明浮点限制。定义及单位未验证时不臆造币种；两只样本金额以人民币元解释，其他报告币种接入时核验。比例保存小数，展示乘 100，不限制在 0—1，保留抵销项、零值和负值。

不存整份 JSON。columns=ALL 中其余尚未定义字段只在内存用于校验，第一版不发布未核定含义的 TOTAL_*、RANKNEW 等指标。归一化哈希包含明确业务字段、名称、代码、报告名、定义版本，不含 run/source ID、取得时间或请求排序变化；RANK 作为展示元数据保存，不单独触发业务修订。

相同业务内容不新增版本、不复制明细或改写旧来源；最新检查时间由 sync_state 表达。若业务内容变化，新版本复制完整当前期明细并引用本次各来源页，旧版本保留。页来源精确引用是这两张新表的设计，不宣称旧个股事实表已具备此能力。

## 读取与后续接口

- 返回股票、实际可用报告期、每期三个维度、真实更新／检查时间、水位、修订号和 warnings；仅返回已发布版本。
- 行业／产品／地区互为不同拆分维度，不跨维度加总，不假设每期都有三种分类。
- 毛利字段保存来源值，不能当净利润；缺失不按收入减成本静默补齐；如未来新增计算结果，单独标明计算口径。
- 第一版提供本地读取及个股财务刷新接入点；具体页面位置另行设计，不顺带改动企业经营趋势指标。

## 实施与验收顺序

1. 编写结构化解析、身份／数值／分页校验；实现两表迁移与版本存储。
2. 实现个股独立服务及同步状态，接入现有财务操作；完善股票移除、备份恢复、重启失败批次处理与数据锁，不影响其他数据集。
3. 同步 docs/stock-collection.md、docs/sqlite-storage.md、docs/database-maintenance.md。maintenance.DESCRIPTIONS 注册两表为个股数据；版本表 TIME_COLUMNS 识别 obtained_at，明细表 PARENT_TIMES 指向版本 obtained_at，空表／缺时间不从报告期推断。
4. 使用隔离临时库和模拟来源测试首次十年窗口、闰日、24小时／180天／600秒策略、空结果缓存、两期重叠、来源迟到、年中新增季度、跨页截断／总数变化／首页变化、错股、重复项目、字段撤回、A→B→A、幂等、冷却及事务回滚。
5. 核对维护页表发现、用途、分类、记录数、大小、空表和父表缺时间规则；验证实际 run/batch/source 外键一致。
6. 有限真实验收仅使用 601919、605499 的十年窗口，metadata_only，不生成原始样本文件；验收统计保存 data/verification/reports/。本次已有只读探测结果只作背景，不充当未来采集行的来源记录。
7. 代码检查及主代理审查通过后，核对宿主机 8765 进程并自动重启，检查日志与访问；有限真实验收使用正常迁移后的日常服务，不另开持续运行测试后台。自动化测试只用隔离库。

## 已知边界

- 当前来源无可靠逐记录披露／修订时间，报告期水位不能证明历史内部无遗漏；metadata_only 不能重放响应。
- 来源实际首期不等于公司最早业务或最早披露，十年范围内缺季度也不能直接认定漏采。
- 原始返回分页校验完成只证明查询段传输完整；未核实逐字段／报告期覆盖前，公共 coverage_status 维持 unknown，并在 result_json 保存查询范围、总数、页数、实际报告期与缺字段统计。
