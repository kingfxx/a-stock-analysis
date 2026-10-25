"""Existing domain data upgrade; SQL migration history remains unchanged."""

def migrate_single_row_prices(conn):
    """Upgrade current CSI/SWS facts in place; migration never fetches source data."""
    from stock_analysis.collection.collection import encoded
    from stock_analysis.domains.indices.index_prices import DEFINITIONS, FIELDS, install_definitions, price_hash

    # Keep the current observation for each business date; version history is
    # represented by the row's version counter and archived collection sources.
    conn.execute("DELETE FROM index_price_observations WHERE is_latest=0")
    install_definitions(conn)
    conn.execute("DROP TRIGGER IF EXISTS index_price_series_immutable")
    conn.execute("""UPDATE index_price_series SET definition_version='v2'
        WHERE provider IN ('csindex','sws') AND definition_version='v1'""")
    conn.execute("""CREATE TRIGGER index_price_series_immutable BEFORE UPDATE ON index_price_series
        BEGIN SELECT RAISE(ABORT,'insert a new index price series identity'); END""")

    for row in conn.execute("""SELECT f.*,s.provider,s.series_key FROM index_price_observations f
        JOIN index_price_series s ON s.id=f.series_id WHERE s.provider IN ('csindex','sws')""").fetchall():
        provider, key = row['provider'], row['series_key']
        definition = DEFINITIONS[(provider, key)]
        lineage = {'_raw': {'revision': int(row['version']), 'fields': {
            name: {'source_id': row['source_id'], 'batch_id': row['batch_id'], 'obtained_at': row['obtained_at']}
            for name in FIELDS if row[name] is not None
        }}}
        values = {name: row[name] for name in FIELDS}
        content_hash = price_hash(values, definition, row['withdrawal_reason'], lineage)
        conn.execute("UPDATE index_price_observations SET field_lineage_json=?,content_hash=? WHERE id=?",
                     (encoded(lineage), content_hash, row['id']))
    conn.execute("CREATE UNIQUE INDEX index_price_one_row_per_date ON index_price_observations(series_id,observation_date)")

