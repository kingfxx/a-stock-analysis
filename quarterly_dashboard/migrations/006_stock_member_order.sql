ALTER TABLE stock_group_members ADD COLUMN position INTEGER NOT NULL DEFAULT 0;

-- Preserve the previous code order when upgrading existing groups.
WITH ordered AS (
    SELECT m.group_id, m.instrument_id,
           row_number() OVER (PARTITION BY m.group_id ORDER BY i.code, i.id) - 1 AS position
    FROM stock_group_members m JOIN instruments i ON i.id = m.instrument_id
)
UPDATE stock_group_members AS m
SET position = (SELECT position FROM ordered
                WHERE ordered.group_id = m.group_id AND ordered.instrument_id = m.instrument_id);
