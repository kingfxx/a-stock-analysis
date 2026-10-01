CREATE TABLE stock_groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL
);
CREATE TABLE stock_group_members (
    group_id INTEGER NOT NULL REFERENCES stock_groups(id) ON DELETE CASCADE,
    instrument_id INTEGER NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, instrument_id)
);
CREATE TABLE stock_recent_views (
    instrument_id INTEGER PRIMARY KEY REFERENCES instruments(id) ON DELETE CASCADE,
    viewed_at TEXT NOT NULL
);
CREATE TABLE stock_picker_preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    selected_group TEXT NOT NULL
);
INSERT INTO stock_picker_preferences VALUES (1, 'recent');
