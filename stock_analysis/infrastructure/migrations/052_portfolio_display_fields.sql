ALTER TABLE portfolio_snapshots ADD COLUMN broker_daily_pnl TEXT;
ALTER TABLE portfolio_snapshots ADD COLUMN broker_daily_pnl_rate TEXT;
ALTER TABLE portfolio_snapshot_positions ADD COLUMN holding_pnl_rate TEXT;
ALTER TABLE portfolio_snapshot_positions ADD COLUMN daily_pnl TEXT;
ALTER TABLE portfolio_snapshot_positions ADD COLUMN daily_pnl_rate TEXT;
