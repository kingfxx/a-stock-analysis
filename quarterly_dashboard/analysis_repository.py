"""Reports, immutable snapshots and transactional task admission/deletion."""
from uuid import uuid4
import json

from .analysis_snapshot import INPUT_VERSION, CALCULATION_VERSION, PROFILE, encoded, digest
from .storage import utc_now

ACTIVE = ("queued", "running", "validating")


class AnalysisRepository:
    def __init__(self, db, *, output_version=None):
        self.db = db
        self.output_version = output_version

    def preferences(self):
        with self.db.connection() as conn:
            row = conn.execute("SELECT model FROM ai_analysis_preferences WHERE id=1").fetchone()
        profile=PROFILE
        if self.output_version == 'business_judgment_v1':
            from .business_judgment import PROFILE as profile
        elif self.output_version:
            from .checklist_snapshot import PROFILE as checklist_profile
            profile=checklist_profile
        return {"model": row[0] if row else None, "analysis_profile": profile}

    def set_model(self, model):
        with self.db.connection(write=True) as conn:
            conn.execute("INSERT INTO ai_analysis_preferences(id,model,updated_at) VALUES (1,?,?) "
                         "ON CONFLICT(id) DO UPDATE SET model=excluded.model,updated_at=excluded.updated_at", (model, utc_now()))
        return self.preferences()

    def enqueue(self, snapshot, request_key, model, account_ref, prompt, *, force=False):
        identity = snapshot["instrument_id"]
        input_version = snapshot["input"].get("schema_version", INPUT_VERSION)
        calc_version = snapshot["input"].get("calculation_version", CALCULATION_VERSION)
        with self.db.connection(write=True) as conn:
            previous = conn.execute("SELECT * FROM ai_analysis_runs WHERE request_key=?", (request_key,)).fetchone()
            if previous:
                if previous["instrument_id"] != identity or previous["model"] != model or previous["account_ref"] != account_ref or previous['output_schema_version'] != prompt['output_version']:
                    raise ValueError("幂等键已经用于另一分析请求")
                return dict(previous)
            active = conn.execute("SELECT * FROM ai_analysis_runs WHERE instrument_id=? AND status IN ('queued','running','validating')",
                                  (identity,)).fetchone()
            if active:
                if active['output_schema_version'] != prompt['output_version']:
                    raise ValueError('当前股票已有其他类型的 AI 任务，请完成或取消后再生成')
                return dict(active)
            if not force:
                successful = conn.execute("SELECT r.* FROM ai_analysis_runs r JOIN ai_analysis_snapshots s ON s.id=r.snapshot_id "
                    "WHERE r.instrument_id=? AND r.status='succeeded' AND s.snapshot_hash=? AND r.model=? AND r.prompt_hash=? "
                    "ORDER BY r.created_at DESC LIMIT 1", (identity, snapshot["hash"], model, digest(prompt))).fetchone()
                if successful:
                    return dict(successful)
            count = conn.execute("SELECT count(*) FROM ai_analysis_runs WHERE status IN ('queued','running','validating')").fetchone()[0]
            if count >= 2:
                raise ValueError("已有分析和等待任务，请稍后再试")
            conn.execute("INSERT INTO ai_analysis_snapshots(instrument_id,input_schema_version,calculation_version,snapshot_hash,"
                "input_json,source_manifest_json,quality_json,captured_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (identity, input_version, calc_version, snapshot["hash"], encoded(snapshot["input"]),
                 encoded(snapshot["manifest"]), encoded(snapshot["quality"]), snapshot["captured_at"]))
            sid = conn.execute("SELECT id FROM ai_analysis_snapshots WHERE instrument_id=? AND input_schema_version=? "
                "AND calculation_version=? AND snapshot_hash=?", (identity, input_version, calc_version, snapshot["hash"])).fetchone()[0]
            run_id = uuid4().hex
            conn.execute("INSERT INTO ai_analysis_runs(id,instrument_id,snapshot_id,request_key,account_ref,model,prompt_version,"
                "prompt_hash,prompt_json,output_schema_version,analysis_profile_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, identity, sid, request_key, account_ref, model, prompt["version"], digest(prompt), encoded(prompt),
                 prompt["output_version"], encoded(snapshot["input"].get("analysis_profile", PROFILE)), "queued", utc_now()))
            return dict(conn.execute("SELECT * FROM ai_analysis_runs WHERE id=?", (run_id,)).fetchone())

    def transition(self, run_id, expected, status, **fields):
        allowed = {"started_at", "completed_at", "result_json", "validation_json", "response_id", "resolved_model",
                   "usage_json", "diagnostic_json", "verdict", "summary"}
        if set(fields) - allowed:
            raise ValueError("Unknown run fields")
        if status not in ACTIVE:
            fields["completed_at"] = utc_now()
        with self.db.connection(write=True) as conn:
            changed = conn.execute("UPDATE ai_analysis_runs SET status=?" + "".join(f",{k}=?" for k in fields)
                + " WHERE id=? AND status=?", [status, *fields.values(), run_id, expected]).rowcount
        return bool(changed)

    def cancel(self, run_id):
        self.run(run_id)
        with self.db.connection(write=True) as conn:
            return bool(conn.execute("UPDATE ai_analysis_runs SET status='cancelled',completed_at=? "
                "WHERE id=? AND status IN ('queued','running','validating')", (utc_now(), run_id)).rowcount)

    def recover(self):
        with self.db.connection(write=True) as conn:
            return conn.execute("UPDATE ai_analysis_runs SET status='interrupted',completed_at=?,diagnostic_json=? "
                "WHERE status IN ('queued','running','validating')", (utc_now(), encoded({"message": "后台已中断，请手动重新分析"}))).rowcount

    def claim(self):
        with self.db.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM ai_analysis_runs WHERE status IN ('running','validating')").fetchone():
                return None
            row = conn.execute("SELECT id FROM ai_analysis_runs WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE ai_analysis_runs SET status='running',started_at=? WHERE id=? AND status='queued'", (utc_now(), row[0]))
        return self.run(row[0], internal=True)

    def run(self, run_id, *, internal=False):
        with self.db.connection() as conn:
            row = conn.execute("SELECT r.*,i.code,i.name,s.input_json,s.snapshot_hash,s.quality_json FROM ai_analysis_runs r "
                "JOIN instruments i ON i.id=r.instrument_id JOIN ai_analysis_snapshots s ON s.id=r.snapshot_id WHERE r.id=?", (run_id,)).fetchone()
        if not row or (not internal and self.output_version and row["output_schema_version"] != self.output_version):
            raise ValueError("研判任务不存在")
        if internal:
            return dict(row)
        data = {k: row[k] for k in ("id", "code", "name", "status", "model", "resolved_model", "prompt_version", "verdict", "summary", "snapshot_hash",
                                    "created_at", "started_at", "completed_at", "output_schema_version")}
        for key, column in (("result", "result_json"), ("input", "input_json"), ("quality", "quality_json"), ("usage", "usage_json")):
            data[key] = json.loads(row[column]) if row[column] else None
        if data.get('result') and data.get('input'):
            data['input']['evidence'].extend(data['result'].get('supplemental_evidence', []))
        diagnostic = json.loads(row["diagnostic_json"]) if row["diagnostic_json"] else {}
        data["error"] = diagnostic.get("message")
        if data['output_schema_version'] == 'business_judgment_v1':
            validation = diagnostic.get('validation') or {}
            unmatched = validation.get('unmatched_url')
            from .business_judgment import public_url
            if public_url(unmatched):
                from difflib import get_close_matches
                from urllib.parse import urlsplit
                candidates = [source.get('url') for source in validation.get('retrieved_sources', [])
                              if public_url(source.get('url')) and urlsplit(source['url']).hostname == urlsplit(unmatched).hostname]
                data['source_error'] = {'model_url': unmatched,
                                       'retrieved_candidates': get_close_matches(unmatched, candidates, n=3, cutoff=0.75)}
        if data['output_schema_version']=='stock_checklist_output_v1' and data.get('result') and data.get('input'):
            from .checklist_valuation import correct_valuation
            data['result']=correct_valuation(data['result'],data['input'])
            data['summary']=data['result']['summary']
        if data.get("input"):
            from .report_business_metrics import product_display_corrections
            data["product_display_corrections"] = product_display_corrections(data["input"])
        return data

    def latest(self, code):
        with self.db.connection() as conn:
            rows = [dict(r) for r in conn.execute("SELECT r.id,r.status FROM ai_analysis_runs r JOIN instruments i ON i.id=r.instrument_id "
                "WHERE i.code=? AND (? IS NULL OR r.output_schema_version=?) ORDER BY r.created_at DESC,r.id DESC", (code,self.output_version,self.output_version))]
        success = next((r["id"] for r in rows if r["status"] == "succeeded"), None)
        active = next((r["id"] for r in rows if r["status"] in ACTIVE), None)
        return {"report": self.run(success) if success else None, "active": self.run(active) if active else None,
                "latest_attempt": self.run(rows[0]["id"]) if rows else None}

    def history(self, code, cursor=None):
        with self.db.connection() as conn:
            anchor = conn.execute("SELECT r.created_at,r.id FROM ai_analysis_runs r JOIN instruments i ON i.id=r.instrument_id "
                "WHERE r.id=? AND i.code=? AND (? IS NULL OR r.output_schema_version=?)", (cursor, code,self.output_version,self.output_version)).fetchone() if cursor else None
            if cursor and not anchor:
                raise ValueError("历史游标无效")
            sql = "SELECT r.id,r.status,r.verdict,r.summary,r.model,r.created_at,r.started_at,r.completed_at FROM ai_analysis_runs r JOIN instruments i ON i.id=r.instrument_id WHERE i.code=?"
            sql += " AND (? IS NULL OR r.output_schema_version=?)"
            args = [code,self.output_version,self.output_version]
            if anchor:
                sql += " AND (r.created_at,r.id)<(?,?)"
                args.extend(anchor)
            rows = [dict(r) for r in conn.execute(sql + " ORDER BY r.created_at DESC,r.id DESC LIMIT 21", args)]
        return {"items": rows[:20], "next_cursor": rows[19]["id"] if len(rows) > 20 else None}

    def delete_history(self, code, run_ids):
        if (not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 200
                or any(not isinstance(value, str) or len(value) != 32 for value in run_ids)
                or len(set(run_ids)) != len(run_ids)):
            raise ValueError("请选择 1–200 条不同的历史记录")
        placeholders = ','.join('?' for _ in run_ids)
        with self.db.connection(write=True) as conn:
            rows = conn.execute("SELECT r.id,r.status,r.snapshot_id FROM ai_analysis_runs r "
                "JOIN instruments i ON i.id=r.instrument_id WHERE i.code=? AND (? IS NULL OR r.output_schema_version=?) AND r.id IN (" + placeholders + ")",
                [code,self.output_version,self.output_version, *run_ids]).fetchall()
            if len(rows) != len(run_ids):
                raise ValueError("部分记录已不存在或不属于当前股票，请刷新历史记录")
            if any(row['status'] in ACTIVE for row in rows):
                raise ValueError("不能删除正在分析或等待中的任务，请先取消分析")
            snapshots = list({row['snapshot_id'] for row in rows})
            conn.execute("DELETE FROM ai_analysis_runs WHERE id IN (" + placeholders + ")", run_ids)
            removed = conn.execute("DELETE FROM ai_analysis_snapshots WHERE id IN (" +
                ','.join('?' for _ in snapshots) + ") AND NOT EXISTS "
                "(SELECT 1 FROM ai_analysis_runs r WHERE r.snapshot_id=ai_analysis_snapshots.id)", snapshots).rowcount
        return {"deleted_runs": len(rows), "deleted_snapshots": removed}
