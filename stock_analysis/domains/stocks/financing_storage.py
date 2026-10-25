"""Sparse retained fields and the atomic version-4 financing conversion."""

import json


NUMERIC_FIELDS = {"RZYE": "margin_balance", "RQYE": "short_balance",
                  "RZRQYE": "total_balance", "RZJME": "net_buy", "SPJ": "close"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def object_json(text):
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Financing JSON must be an object")
    encoded(value)
    return value


def effective_fields(row):
    raw = object_json(row["raw_json"])
    retained = object_json(row["retained_fields_json"])
    for field, entry in retained.items():
        if (field in raw or not isinstance(entry, dict) or set(entry) != {"value", "run_id"}
                or type(entry["run_id"]) is not int or entry["run_id"] <= 0):
            raise ValueError(f"Invalid retained financing field: {field}")
    return raw, retained, {**{k: entry["value"] for k, entry in retained.items()}, **raw}


def _validate_origins(row, retained, runs):
    for field, entry in retained.items():
        run = runs.get(entry["run_id"])
        if (run is None or run["instrument_id"] != row["instrument_id"]
                or run["source"] != row["source"] or run["dataset"] != "financing"
                or run["adjustment"] != "raw" or run["status"] != "success"):
            raise ValueError(f"Invalid retained financing origin: {row['trade_date']} {field}")


def check_retained_fields(conn):
    runs = {r["id"]: r for r in conn.execute("SELECT * FROM sync_runs")}
    for row in conn.execute("SELECT * FROM financing_daily"):
        _, retained, _ = effective_fields(row)
        _validate_origins(row, retained, runs)


def migrate_financing(conn):
    """Caller owns the transaction; all failures roll back to the old schema."""
    runs = {r["id"]: r for r in conn.execute("SELECT * FROM sync_runs")}
    constraints = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='financing_daily' "
        "AND type IN ('trigger','index') AND sql IS NOT NULL")]
    codes = dict(conn.execute("SELECT id,code FROM instruments"))
    columns = ("instrument_id", "source", "trade_date", "margin_balance", "short_balance",
               "total_balance", "net_buy", "close", "raw_json", "retained_fields_json",
               "content_hash", "obtained_at", "run_id")
    conflicts, count = [], 0
    for row in conn.execute("SELECT * FROM financing_daily"):
        _validate_origins(row, {"row": {"run_id": row["run_id"]}}, runs)
        raw = object_json(row["raw_json"])
        old = object_json(row["canonical_extra_json"])
        origins = object_json(row["field_provenance_json"])
        if any(field not in old or encoded(old[field]) != encoded(value) for field, value in raw.items()):
            raise ValueError(f"Cannot reconstruct financing fields: {row['trade_date']}")
        retained = {field: {"value": value, "run_id": origins.get(field)}
                    for field, value in old.items() if field not in raw}
        candidate = {**dict(row), "retained_fields_json": encoded(retained)}
        _, retained, restored = effective_fields(candidate)
        _validate_origins(row, retained, runs)
        if encoded(restored) != encoded(old):
            raise ValueError(f"Financing reconstruction changed: {row['trade_date']}")
        for field, column in NUMERIC_FIELDS.items():
            if field not in restored and row[column] is not None:
                raise ValueError(f"Missing financing value history: {row['trade_date']} {field}")
        for field, origin in origins.items():
            if field in raw and origin != row["run_id"]:
                conflicts.append({"code": codes[row["instrument_id"]], "date": row["trade_date"],
                                  "field": field, "old_run_id": origin, "new_run_id": row["run_id"],
                                  "reason": "Field exists in the current raw response"})
        conn.execute(f"INSERT INTO financing_daily_compact ({','.join(columns)}) "
                     f"VALUES ({','.join('?' for _ in columns)})", [candidate[c] for c in columns])
        count += 1
    if conn.execute("SELECT count(*) FROM financing_daily_compact").fetchone()[0] != count:
        raise ValueError("Financing migration row count changed")
    conn.execute("DROP TABLE financing_daily")
    conn.execute("ALTER TABLE financing_daily_compact RENAME TO financing_daily")
    for statement in constraints:
        conn.execute(statement)
    check_retained_fields(conn)
    if [r[0] for r in conn.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("Financing migration integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("Financing migration foreign-key check failed")
    return {"rows": count, "conflicts": conflicts,
            "phase": "validated_before_commit; schema_migrations confirms commit"}
