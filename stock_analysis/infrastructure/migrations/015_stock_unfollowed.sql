-- Attention state is independent of retained source facts and research artifacts.
CREATE TABLE stock_unfollowed (
    instrument_id INTEGER PRIMARY KEY REFERENCES instruments(id) ON DELETE CASCADE,
    unfollowed_at TEXT NOT NULL
);
CREATE TABLE stock_unfollowed_groups (
    instrument_id INTEGER NOT NULL REFERENCES stock_unfollowed(instrument_id) ON DELETE CASCADE,
    group_id INTEGER NOT NULL REFERENCES stock_groups(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, group_id)
);
