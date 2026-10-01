"""One model worker; one waiting task. No inference during read requests."""
from __future__ import annotations

import json
from threading import Event, Lock, Thread

from .analysis_snapshot import capture, changes, encoded, SnapshotError
from .analysis_repository import AnalysisRepository, ACTIVE
from .analysis_validation import validate_output, prompt, ReportValidationError
from .ai_provider import ProviderError
from .sources import normalize_code


class AnalysisService:
    def __init__(self, db, provider, *, snapshotter=capture):
        self.db, self.provider, self.snapshotter = db, provider, snapshotter
        self.repository = AnalysisRepository(db)
        self.events = {}
        self.lock = Lock()
        self.worker = None

    def status(self):
        return {**self.provider.status(), **self.repository.preferences()}

    def overview(self, code):
        code = normalize_code(code)
        data = self.repository.latest(code)
        data.update(changed=False, change_types=[], quality=None, data_error=None)
        try:
            snapshot = self.snapshotter(self.db, code)
            data["quality"] = snapshot["quality"]
            data["current_snapshot_hash"] = snapshot["hash"]
            data["data_range"] = {"evidence_count": len(snapshot["input"]["evidence"]),
                                  "limitations": snapshot["input"]["limitations"], "input": snapshot["input"]}
            if data["report"]:
                data["change_types"] = changes(data["report"]["input"], snapshot["input"])
                data["changed"] = bool(data["change_types"])
        except SnapshotError as exc:
            data["data_error"] = str(exc)
        return data

    def create(self, command):
        if not isinstance(command, dict) or set(command) - {"code", "model", "request_key", "force"}:
            raise ValueError("研判请求字段无效")
        code = normalize_code(command.get("code", ""))
        request_key = command.get("request_key")
        if not isinstance(request_key, str) or not 16 <= len(request_key) <= 128:
            raise ValueError("研判请求必须提供幂等键")
        if type(command.get("force", False)) is not bool:
            raise ValueError("重新分析标记无效")
        status = self.status()
        if not status["connected"] or not status["plan_authorized"]:
            raise ProviderError("尚未连接或未授权使用 ChatGPT 额度，请打开 AI 设置")
        model = command.get("model") or status["model"]
        if not isinstance(model, str) or not model or len(model) > 128:
            raise ValueError("请先在 AI 设置中选择模型")
        # Catalog lookup is permitted only for explicit generation/settings operations.
        models = self.provider.catalog or self.provider.models()
        if model not in {m["slug"] for m in models}:
            raise ValueError("所选模型不在当前账号列表中，请打开 AI 设置重新选择")
        snapshot = self.snapshotter(self.db, code)
        if snapshot["quality"]["updating"]:
            raise ValueError("数据更新中，请稍候；已有报告仍可查看")
        run = self.repository.enqueue(snapshot, request_key, model, status["account_ref"], prompt(), force=command.get("force", False))
        accepted = run["status"] in ACTIVE
        if accepted:
            self._start_worker()
        result = self.repository.run(run["id"])
        result["accepted"] = accepted
        return result

    def _start_worker(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.worker = Thread(target=self._work, name="stock-ai-analysis", daemon=True)
            self.worker.start()

    def _work(self):
        while True:
            with self.lock:
                run = self.repository.claim()
                if not run:
                    self.worker = None
                    return
                event = self.events.setdefault(run["id"], Event())
            try:
                if self.provider.status()["account_ref"] != run["account_ref"]:
                    raise ProviderError("分析账号已断开")
                output = self.provider.infer(run["id"], run["model"], json.loads(run["prompt_json"])["instructions"],
                                             json.loads(run["input_json"]), event)
                if event.is_set() or not self.repository.transition(run["id"], "running", "validating"):
                    continue
                result = validate_output(output["text"], json.loads(run["input_json"]))
                self.repository.transition(run["id"], "validating", "succeeded", result_json=encoded(result),
                    verdict=result["verdict"], summary=result["summary"], validation_json=encoded({"version": "v2", "valid": True,
                        "provider": output.get("diagnostic")}),
                    response_id=output.get("response_id"), resolved_model=output.get("model"), usage_json=encoded(output.get("usage")))
            except Exception as exc:
                # Do not persist arbitrary exception strings (may contain network tokens).
                message = str(exc) if isinstance(exc, (ProviderError, ValueError)) and len(str(exc)) < 300 else "分析失败，请手动重试"
                if not isinstance(exc, ProviderError) and not isinstance(exc, ValueError):
                    message = "分析失败，请检查本地存储和网络后手动重试"
                for expected in ("running", "validating"):
                    self.repository.transition(run["id"], expected, "failed",
                        diagnostic_json=encoded({"message": message, "category": type(exc).__name__,
                            "provider": exc.diagnostic if isinstance(exc, ProviderError) else None,
                            "validation": exc.diagnostic if isinstance(exc, ReportValidationError) else None}))
            finally:
                with self.lock:
                    self.events.pop(run["id"], None)

    def cancel(self, run_id):
        self.repository.cancel(run_id)
        with self.lock:
            if run_id in self.events:
                self.events[run_id].set()
        self.provider.cancel(run_id)
        return self.repository.run(run_id)

    def disconnect(self):
        with self.db.connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM ai_analysis_runs WHERE status IN ('queued','running','validating')")]
        for run_id in ids:
            self.cancel(run_id)
        return self.provider.disconnect()
