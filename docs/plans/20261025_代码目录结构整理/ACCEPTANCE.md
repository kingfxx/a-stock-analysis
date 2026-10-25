# 目录整理验收记录

## 阶段一：Python 迁移

- 原始快照（不含业务数据）：1573 passed、50 failed、2 skipped。
- 迁移版本：1574 passed、50 failed、1 skipped。失败节点集合完全相同，新增失败 0；原始快照中一个条件性跳过在迁移版通过。
- 两边均使用逐文件回归、明确临时数据库与禁止外部 socket 连接的验收插件；不使用原库做自动测试。
- 一个旧行业市值测试替换已停用的接口，会进入真实限速等待，单独标记跳过；未更改断言或业务限速让它通过。Windows 长路径及测试端口复用干扰通过缩短临时路径、关闭验收服务端口复用后复测排除。
- 101 个原模块均可在禁止数据库连接与网络连接的条件下导入；25 个 CLI 帮助入口通过。
- 77 个 SQL、提示词及资源文件 SHA256 全部一致；默认数据库仍为 data/stock_analysis.sqlite3，项目根不随领域目录深度变化。
- 旧后台依赖旧资源位置，迁移期间已保留临时旧源码/资源供旧进程使用；正式切换验证后清理，不作为永久兼容包。
- 完整逐文件结果见 data/verification/reports/structure-baseline/SUMMARY.json 与 structure-stage1/SUMMARY.json；命令及驱动保留在 data/verification/tools/run_structure_regression.py、structure_test_guard.py。

### 原始快照可复现的失败节点

- `tests.test_announcement_server_integration::test_financial_update_allows_initial_sync_only_for_explicit_refresh`
- `tests.test_async_loading::test_valuation_frequency_browser_controls_use_saved_rows_and_ignore_stale_responses`
- `tests.test_beijing_time::test_beijing_time_and_cooldown_presentation`
- `tests.test_checklist_product_tables::test_product_table_formats_without_stock_specific_logic`
- `tests.test_database_restore::test_old_schema_upgraded_in_copy_and_original_unchanged`
- `tests.test_dividend_service::test_proposal_becomes_implemented_without_duplicate_cash`
- `tests.test_fundamental_service::test_existing_three_statements_backfill_key_indicators_in_same_table`
- `tests.test_index_crowding::test_browser_crowding_switch_statistics_gaps_and_valuation`
- `tests.test_index_price_codec::test_failed_migration_rolls_back_and_can_resume_after_fix`
- `tests.test_index_prices::test_v36_migration_preserves_latest_id_and_is_immediately_idempotent`
- `tests.test_index_read_revision_migration::test_revision_migration_preserves_facts_and_tracks_transaction`
- `tests.test_index_series_migration::test_upgrade_preserves_revisions_and_sources`
- `tests.test_index_ui::test_browser_cooldown_time_is_beijing_to_seconds`
- `tests.test_index_ui::test_browser_explicit_series_filters_sources_and_no_auto_collection`
- `tests.test_index_ui::test_browser_metric_first_defaults_and_filtered_choices`
- `tests.test_index_ui::test_browser_toggles_real_index_points_on_right_axis`
- `tests.test_industries::test_industry_api_requires_explicit_task_stock_routes_stay_separate`
- `tests.test_industries::test_quarter_cap_default_skip_and_revision_only_changed_company`
- `tests.test_industries::test_versions_same_quarter_and_failed_import_preserve_old`
- `tests.test_industry_bulk::test_old_finance_visible_and_empty_cap_stays_null`
- `tests.test_industry_bulk::test_publish_versions_quarter_and_no_individual_tables`
- `tests.test_industry_memory::test_concurrent_cold_loads_build_once_and_refresh_rebuilds`
- `tests.test_industry_memory::test_legacy_sources_are_shared_without_merging_different_sources`
- `tests.test_industry_memory::test_provenance_is_shared_and_read_does_not_mutate_it`
- `tests.test_industry_prices::test_cap_task_calls_prices_after_saving_even_without_cap_gaps`
- `tests.test_industry_valuation::test_quarter_end_valuation_same_date_and_ttm_across_modes`
- `tests.test_market_congestion::test_browser_dual_axes_all_broad_ranges_and_read_only`
- `tests.test_market_congestion::test_browser_polling_preserves_zoom_and_filter_resets`
- `tests.test_market_financial::test_v32_rebuild_preserves_legacy_rows_and_adds_disjoint_public_references`
- `tests.test_shared_data::test_concurrent_failed_price_refresh_reuses_failure_and_valid_version`
- `tests.test_shared_data::test_daily_backup_preserves_state_before_first_update`
- `tests.test_shared_data::test_daily_backup_rolls_over_without_restart`
- `tests.test_shared_data::test_price_bundle_projects_market_cap_without_qfq`
- `tests.test_sources::test_financial_parser_uses_report_period_and_disclosure_date`
- `tests.test_stock_library::test_groups_order_multimembership_and_preferences_survive_reopen`
- `tests.test_stock_names::test_missing_name_is_backfilled_asynchronously_and_searchable_without_report_refresh`
- `tests.test_stock_names::test_name_failure_keeps_facts_and_retries_on_next_update[   ]`
- `tests.test_stock_names::test_name_failure_keeps_facts_and_retries_on_next_update[None]`
- `tests.test_stock_names::test_name_failure_keeps_facts_and_retries_on_next_update[]`
- `tests.test_stock_names::test_name_failure_keeps_facts_and_retries_on_next_update[offline]`
- `tests.test_stock_picker_drag::test_stock_picker_drag_with_real_browser[stock_cleanup.cjs-cleanup checks passed]`
- `tests.test_stock_picker_drag::test_stock_picker_drag_with_real_browser[stock_picker_drag.cjs-drag checks passed]`
- `tests.test_stock_picker_drag::test_stock_picker_drag_with_real_browser[stock_unfollow.cjs-unfollow checks passed]`
- `tests.test_valuation::test_dividend_yield_uses_paid_cash_in_trailing_year_and_raw_month_close`
- `tests.test_valuation_frequency::test_standard_deviation_uses_same_daily_population_as_percentiles[dividend_yield]`
- `tests.test_valuation_frequency::test_standard_deviation_uses_same_daily_population_as_percentiles[pb]`
- `tests.test_valuation_frequency::test_standard_deviation_uses_same_daily_population_as_percentiles[pe]`
- `tests.test_valuation_frequency::test_standard_deviation_uses_same_daily_population_as_percentiles[ps]`
- `tests.test_valuation_frequency::test_yield_summary_uses_last_yield_observation_when_baidu_has_a_later_month`
- `tests.test_valuation_service::test_initial_three_windows_then_one_year_update_preserves_dense_history`

## 阶段二

- 完整回归：1575 passed、49 failed、1 skipped，无新增失败；与阶段一相比，指数筛选的一项浏览器断言本次通过（视为波动，不宣称修复业务问题）。
- 49 个前端文件字节保持；39 个 HTTP 静态 URL 内容/MIME、4 个拒绝路径、两页面的无数据库访问检查通过。
- 部署 PID32140，主页/指数/航运三页面200，schema42不变，临时旧资源在切换后删除。
- 对照证据：structure-stage2/SUMMARY.json、structure-assets.json、structure-live-prestage2.json、structure-live-stage2.json。

## 后台与配置

阶段一切换进行中；最终状态以本文件后续记录为准。只读切换前快照为 data/verification/reports/structure-live-before.json。SQL schema 为 42；迁移本身未新增 SQL 或更改配置。

### 阶段一实际部署完成

新后台 PID33200；主页、指数、航运页面均200，日志无启动异常。schema_migrations、collection_schedules、collection_schedule_rules、collection_rate_limits、collection_provider_state 与切换前一致；没有活动采集、没有新增定时或真实回填。

## 第三阶段首批与最终结果

- runtime、侧栏、两个领域升级、估值纯计算及共享同步辅助已提取，详见 INTERNAL_EXTRACTIONS.md。29个函数体AST保持，纯估值模块不加载 requests/sqlite3。
- 最终1575 passed、49 failed、1 skipped；失败集合与阶段二完全相同，无新增失败，无超时或未完成文件。来源分页漏导入math问题已修复，24项来源契约复测通过；最终逐文件XML/日志与汇总已更新。
- 相对原始基线50失败，本次指数筛选断言通过一次，按既有波动处理，未宣称修复；原始50节点清单保留以便对照。
- 最终127个模块导入在禁止网络/SQLite连接下通过，原业务资源77项及前端49项哈希一致；39静态HTTP内容/MIME、4拒绝路径、2页无库访问验收再次通过。
- 最终8765部署：三页面200，schema42不变，全部记录的迁移/定时/限速/提供方状态与切换前一致，启动日志正常；证据 structure-live-prefinal.json / structure-live-final.json 与 structure-server-final.out.log / .err.log。
- 自动回归使用隔离库且禁止外部socket，生产验收只读；没有手动启动采集/回填或模型生成，没有新增定时。

最终后台 PID40764；清理已完成，保留当前后台及日志。原源码快照和测试临时库已删除；完整逐文件结果、原始文件哈希清单及可复用验收工具保留。
