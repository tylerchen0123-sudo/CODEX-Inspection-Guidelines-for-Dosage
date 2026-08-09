# -*- coding: utf-8 -*-
"""
CODEX + WORKBUDDY 双端 Token 实时采集器
------------------------------------------------
数据源（只读，绝不修改）：
  Codex CLI  : ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
               ~/.codex/archived_sessions/**/*.jsonl
               -> event_msg.token_count 事件，含 last_token_usage / rate_limits
  WorkBuddy  : ~/.workbuddy/projects/<slug>/<session_id>.jsonl
               -> 每条含 message.usage 的行 = 一次 API 调用
               ~/.workbuddy/workbuddy.db (sessions / session_usage) 补齐元数据

增量策略：按文件记录 (mtime, size, offset)，只解析新增字节。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
WB_HOME = HOME / ".workbuddy"
CODEX_SESSION_DIRS = [CODEX_HOME / "sessions", CODEX_HOME / "archived_sessions"]
WB_PROJECTS = WB_HOME / "projects"
WB_DB = WB_HOME / "workbuddy.db"

BASE_DIR = Path(__file__).resolve().parent
PRICING_FILE = BASE_DIR / "pricing.json"
STATE_FILE = BASE_DIR / ".scan-state.json"
STORE_DB = BASE_DIR / "monitor.db"

LOCAL_TZ = datetime.now().astimezone().tzinfo


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _to_epoch_ms(value) -> int:
    """把 ISO 字符串 / epoch 秒 / epoch 毫秒 统一成 epoch 毫秒。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v if v > 1e11 else v * 1000)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        if s.isdigit():
            return _to_epoch_ms(int(s))
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _day_key(epoch_ms: int) -> str:
    if not epoch_ms:
        return "unknown"
    return datetime.fromtimestamp(epoch_ms / 1000, LOCAL_TZ).strftime("%Y-%m-%d")


def _hour_key(epoch_ms: int) -> str:
    if not epoch_ms:
        return "unknown"
    return datetime.fromtimestamp(epoch_ms / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:00")


def load_pricing() -> dict:
    try:
        with open(PRICING_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"currency": "CNY", "models": {},
                "default": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}}


def price_of(pricing: dict, model: str) -> dict:
    models = pricing.get("models") or {}
    if model in models:
        return models[model]
    # 前缀模糊匹配，兼容带日期后缀的模型名
    for name, cfg in models.items():
        if model and (model.startswith(name) or name.startswith(model)):
            return cfg
    return pricing.get("default") or {}


def calc_cost(pricing: dict, model: str, fresh_in: int, cache_read: int,
              cache_write: int, out: int) -> float:
    p = price_of(pricing, model)
    return (
        fresh_in / 1e6 * float(p.get("input", 0) or 0)
        + cache_read / 1e6 * float(p.get("cache_read", 0) or 0)
        + cache_write / 1e6 * float(p.get("cache_write", 0) or 0)
        + out / 1e6 * float(p.get("output", 0) or 0)
    )


# --------------------------------------------------------------------------
# 一次 API 调用的原子记录
# --------------------------------------------------------------------------
class Call:
    __slots__ = ("source", "ts", "model", "session_id", "project",
                 "fresh_in", "cache_read", "cache_write", "out", "reasoning", "total")

    def __init__(self, source, ts, model, session_id, project,
                 fresh_in, cache_read, cache_write, out, reasoning):
        self.source = source
        self.ts = ts
        self.model = model or "unknown"
        self.session_id = session_id
        self.project = project
        self.fresh_in = max(0, fresh_in)
        self.cache_read = max(0, cache_read)
        self.cache_write = max(0, cache_write)
        self.out = max(0, out)
        self.reasoning = max(0, reasoning)
        self.total = self.fresh_in + self.cache_read + self.cache_write + self.out


# --------------------------------------------------------------------------
# 采集器
# --------------------------------------------------------------------------
class Collector:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[Call] = []
        self.file_state: dict[str, dict] = {}       # path -> {size, offset, mtime}
        self.session_meta: dict[str, dict] = {}     # session_id -> meta
        self.rate_limits: dict | None = None
        self.rate_limits_ts: int = 0
        self.last_scan_ms: int = 0
        self.scan_duration_ms: int = 0
        self.db_meta: dict = {}
        self.ctx_usage: dict = {}
        self._init_store()
        self._load_state()
        self._load_calls_from_store()

    # ---------- 本地持久化：解析结果落 SQLite，重启不丢历史 ----------
    def _init_store(self) -> None:
        con = sqlite3.connect(STORE_DB)
        con.execute("""CREATE TABLE IF NOT EXISTS calls (
            source TEXT, ts INTEGER, model TEXT, session_id TEXT, project TEXT,
            fresh_in INTEGER, cache_read INTEGER, cache_write INTEGER,
            out_tokens INTEGER, reasoning INTEGER,
            PRIMARY KEY (source, session_id, ts, out_tokens, fresh_in)
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)")
        con.execute("""CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)""")
        con.commit()
        con.close()

    def _load_calls_from_store(self) -> None:
        try:
            con = sqlite3.connect(STORE_DB)
            rows = con.execute(
                "SELECT source, ts, model, session_id, project, fresh_in,"
                " cache_read, cache_write, out_tokens, reasoning FROM calls ORDER BY ts"
            ).fetchall()
            self.calls = [Call(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9])
                          for r in rows]
            row = con.execute("SELECT v FROM kv WHERE k='rate_limits'").fetchone()
            if row:
                blob = json.loads(row[0])
                self.rate_limits = blob.get("data")
                self.rate_limits_ts = blob.get("ts", 0)
            con.close()
        except Exception:
            self.calls = []

    def _persist_calls(self, calls: list) -> None:
        if not calls:
            return
        try:
            con = sqlite3.connect(STORE_DB)
            con.executemany(
                "INSERT OR IGNORE INTO calls VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(c.source, c.ts, c.model, c.session_id, c.project, c.fresh_in,
                  c.cache_read, c.cache_write, c.out, c.reasoning) for c in calls],
            )
            if self.rate_limits:
                con.execute(
                    "INSERT OR REPLACE INTO kv VALUES ('rate_limits', ?)",
                    (json.dumps({"data": self.rate_limits, "ts": self.rate_limits_ts}),),
                )
            con.commit()
            con.close()
        except Exception:
            pass

    # ---------- 扫描状态持久化（只存 offset，重启不必全量重扫） ----------
    def _load_state(self) -> None:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                self.file_state = json.load(fh).get("files", {})
        except Exception:
            self.file_state = {}

    def _save_state(self) -> None:
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"files": self.file_state}, fh)
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass

    # ---------------------------- Codex ----------------------------
    def _scan_codex_file(self, path: Path) -> list[Call]:
        out: list[Call] = []
        key = str(path)
        st = self.file_state.get(key) or {}
        size = path.stat().st_size
        offset = st.get("offset", 0)
        if offset > size:      # 文件被截断/重写，全量重读
            offset = 0
        if offset == size:
            return out

        session_id = path.stem
        cwd = st.get("cwd", "")
        model = st.get("model", "")
        prev_total = st.get("prev_total", 0)

        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(offset)
            for line in fh:
                if not line.endswith("\n"):        # 半行，留到下次
                    break
                offset += len(line.encode("utf-8", "ignore"))
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                typ = obj.get("type")
                payload = obj.get("payload") or {}
                if not isinstance(payload, dict):
                    continue

                if typ == "session_meta":
                    session_id = payload.get("id") or payload.get("session_id") or session_id
                    cwd = payload.get("cwd") or cwd
                    self.session_meta[session_id] = {
                        "cwd": cwd,
                        "cli_version": payload.get("cli_version"),
                        "originator": payload.get("originator"),
                        "thread_source": payload.get("thread_source"),
                    }
                    continue

                if typ == "turn_context":
                    model = payload.get("model") or model
                    cwd = payload.get("cwd") or cwd
                    continue

                if typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    rl = payload.get("rate_limits")
                    ts = _to_epoch_ms(obj.get("timestamp"))
                    if rl and ts >= self.rate_limits_ts:
                        self.rate_limits = rl
                        self.rate_limits_ts = ts

                    last = info.get("last_token_usage") or {}
                    total_usage = info.get("total_token_usage") or {}
                    cur_total = int(total_usage.get("total_tokens") or 0)

                    inp = int(last.get("input_tokens") or 0)
                    cached = int(last.get("cached_input_tokens") or 0)
                    cwrite = int(last.get("cache_write_input_tokens") or 0)
                    outp = int(last.get("output_tokens") or 0)
                    reasoning = int(last.get("reasoning_output_tokens") or 0)

                    if inp == 0 and outp == 0:
                        # 少数事件只带 total，用差值兜底
                        if cur_total > prev_total:
                            inp = cur_total - prev_total
                        else:
                            prev_total = max(prev_total, cur_total)
                            continue
                    prev_total = max(prev_total, cur_total)

                    out.append(Call(
                        "codex", ts, model, session_id, cwd,
                        fresh_in=inp - cached, cache_read=cached,
                        cache_write=cwrite, out=outp, reasoning=reasoning,
                    ))

        self.file_state[key] = {
            "offset": offset, "size": size, "mtime": path.stat().st_mtime,
            "cwd": cwd, "model": model, "prev_total": prev_total,
        }
        return out

    # -------------------------- WorkBuddy --------------------------
    def _scan_wb_file(self, path: Path, model_hint: str, project: str) -> list[Call]:
        out: list[Call] = []
        key = str(path)
        st = self.file_state.get(key) or {}
        size = path.stat().st_size
        offset = st.get("offset", 0)
        if offset > size:
            offset = 0
        if offset == size:
            return out

        session_id = path.stem
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(offset)
            for line in fh:
                if not line.endswith("\n"):
                    break
                offset += len(line.encode("utf-8", "ignore"))
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                outp = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                cread = int(usage.get("cache_read_input_tokens")
                            or usage.get("prompt_cache_hit_tokens")
                            or usage.get("cached_tokens") or 0)
                cwrite = int(usage.get("cache_creation_input_tokens")
                             or usage.get("prompt_cache_write_tokens") or 0)
                details = usage.get("completion_tokens_details") or {}
                reasoning = int(details.get("reasoning_tokens")
                                or usage.get("completion_thinking_tokens") or 0)

                if inp == 0 and outp == 0:
                    continue

                out.append(Call(
                    "workbuddy", _to_epoch_ms(obj.get("timestamp")),
                    msg.get("model") or model_hint, session_id, project,
                    fresh_in=inp - cread - cwrite, cache_read=cread,
                    cache_write=cwrite, out=outp, reasoning=reasoning,
                ))

        self.file_state[key] = {"offset": offset, "size": size, "mtime": path.stat().st_mtime}
        return out

    def _wb_db_meta(self) -> tuple[dict, dict]:
        """返回 (session_id -> {model,title,cwd,...}, session_id -> {used,size})"""
        meta, ctx = {}, {}
        if not WB_DB.exists():
            return meta, ctx
        try:
            con = sqlite3.connect(f"file:{WB_DB.as_posix()}?mode=ro", uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            for r in con.execute(
                "select id, cwd, title, custom_title, model, mode, created_at,"
                " last_activity_at, is_background_automation from sessions"
            ):
                meta[r["id"]] = {
                    "model": r["model"], "cwd": r["cwd"],
                    "title": r["custom_title"] or r["title"],
                    "mode": r["mode"],
                    "created_at": r["created_at"],
                    "last_activity_at": r["last_activity_at"],
                    "automation": bool(r["is_background_automation"]),
                }
            for r in con.execute("select session_id, used, size, updated_at from session_usage"):
                ctx[r["session_id"]] = {
                    "used": r["used"], "size": r["size"], "updated_at": r["updated_at"]
                }
            con.close()
        except Exception:
            pass
        return meta, ctx

    # ---------------------------- 主扫描 ----------------------------
    def scan(self) -> int:
        t0 = time.time()
        new_calls: list[Call] = []

        # Codex
        for root in CODEX_SESSION_DIRS:
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                try:
                    if self._unchanged(path):
                        continue
                    new_calls += self._scan_codex_file(path)
                except Exception:
                    continue

        # WorkBuddy
        db_meta, ctx_usage = self._wb_db_meta()
        if WB_PROJECTS.exists():
            for proj_dir in WB_PROJECTS.iterdir():
                if not proj_dir.is_dir():
                    continue
                for path in proj_dir.glob("*.jsonl"):
                    try:
                        if self._unchanged(path):
                            continue
                        info = db_meta.get(path.stem) or {}
                        new_calls += self._scan_wb_file(
                            path, info.get("model") or "unknown",
                            info.get("cwd") or proj_dir.name,
                        )
                    except Exception:
                        continue

        with self.lock:
            self.calls.extend(new_calls)
            self.calls.sort(key=lambda c: c.ts)
            self.db_meta = db_meta
            self.ctx_usage = ctx_usage
            self.last_scan_ms = int(time.time() * 1000)
            self.scan_duration_ms = int((time.time() - t0) * 1000)
        self._persist_calls(new_calls)
        self._save_state()
        return len(new_calls)

    def _unchanged(self, path: Path) -> bool:
        st = self.file_state.get(str(path))
        if not st:
            return False
        try:
            stat = path.stat()
        except OSError:
            return True
        return st.get("size") == stat.st_size and st.get("mtime") == stat.st_mtime

    # ---------------------------- 聚合输出 ----------------------------
    def build_summary(self, days: int = 14) -> dict:
        pricing = load_pricing()
        now_ms = int(time.time() * 1000)
        today = _day_key(now_ms)
        cutoff = now_ms - days * 86400_000

        with self.lock:
            calls = list(self.calls)
            rate_limits = self.rate_limits
            db_meta = getattr(self, "db_meta", {})
            ctx_usage = getattr(self, "ctx_usage", {})

        def blank():
            return {"calls": 0, "fresh_in": 0, "cache_read": 0, "cache_write": 0,
                    "out": 0, "reasoning": 0, "total": 0, "cost": 0.0}

        def add(bucket, c, cost):
            bucket["calls"] += 1
            bucket["fresh_in"] += c.fresh_in
            bucket["cache_read"] += c.cache_read
            bucket["cache_write"] += c.cache_write
            bucket["out"] += c.out
            bucket["reasoning"] += c.reasoning
            bucket["total"] += c.total
            bucket["cost"] += cost

        overall = {"codex": blank(), "workbuddy": blank()}
        today_bucket = {"codex": blank(), "workbuddy": blank()}
        by_day: dict[str, dict] = defaultdict(lambda: {"codex": blank(), "workbuddy": blank()})
        by_hour: dict[str, dict] = defaultdict(lambda: {"codex": blank(), "workbuddy": blank()})
        by_model: dict[str, dict] = {}
        by_session: dict[str, dict] = {}
        recent: list[dict] = []

        h24 = now_ms - 86400_000
        h1 = now_ms - 3600_000
        m5 = now_ms - 300_000
        live = {"h1": blank(), "m5": blank()}

        for c in calls:
            cost = calc_cost(pricing, c.model, c.fresh_in, c.cache_read, c.cache_write, c.out)
            add(overall[c.source], c, cost)
            dk = _day_key(c.ts)
            if dk == today:
                add(today_bucket[c.source], c, cost)
            if c.ts >= cutoff:
                add(by_day[dk][c.source], c, cost)
            if c.ts >= h24:
                add(by_hour[_hour_key(c.ts)][c.source], c, cost)
            if c.ts >= h1:
                add(live["h1"], c, cost)
            if c.ts >= m5:
                add(live["m5"], c, cost)

            mk = f"{c.source}::{c.model}"
            if mk not in by_model:
                by_model[mk] = {"source": c.source, "model": c.model, **blank()}
            add(by_model[mk], c, cost)

            sk = c.session_id
            if sk not in by_session:
                info = db_meta.get(sk) or self.session_meta.get(sk) or {}
                ctx = ctx_usage.get(sk) or {}
                by_session[sk] = {
                    "session_id": sk, "source": c.source, "model": c.model,
                    "project": c.project or info.get("cwd") or "",
                    "title": info.get("title") or "",
                    "first_ts": c.ts, "last_ts": c.ts,
                    "ctx_used": ctx.get("used"), "ctx_size": ctx.get("size"),
                    **blank(),
                }
            s = by_session[sk]
            s["last_ts"] = max(s["last_ts"], c.ts)
            s["first_ts"] = min(s["first_ts"], c.ts)
            s["model"] = c.model or s["model"]
            add(s, c, cost)

        for c in calls[-60:]:
            recent.append({
                "source": c.source, "ts": c.ts, "model": c.model,
                "session_id": c.session_id[:8], "project": _short_path(c.project),
                "fresh_in": c.fresh_in, "cache_read": c.cache_read,
                "out": c.out, "total": c.total,
                "cost": round(calc_cost(pricing, c.model, c.fresh_in,
                                        c.cache_read, c.cache_write, c.out), 6),
            })
        recent.reverse()

        # Codex 官方额度
        quota = None
        if rate_limits:
            quota = {"plan": rate_limits.get("plan_type"), "windows": []}
            for slot in ("primary", "secondary"):
                w = rate_limits.get(slot)
                if not isinstance(w, dict):
                    continue
                quota["windows"].append({
                    "slot": slot,
                    "used_percent": w.get("used_percent"),
                    "window_minutes": w.get("window_minutes"),
                    "resets_at": w.get("resets_at"),
                })
            credits = rate_limits.get("credits") or {}
            quota["credits"] = {
                "balance": credits.get("balance"),
                "unlimited": credits.get("unlimited"),
                "has_credits": credits.get("has_credits"),
            }
            quota["updated_at"] = self.rate_limits_ts

        day_rows = []
        for dk in sorted(by_day.keys()):
            row = {"day": dk}
            for src in ("codex", "workbuddy"):
                row[src] = by_day[dk][src]
            day_rows.append(row)

        hour_rows = []
        for hk in sorted(by_hour.keys()):
            row = {"hour": hk}
            for src in ("codex", "workbuddy"):
                row[src] = by_hour[hk][src]
            hour_rows.append(row)

        sessions = sorted(by_session.values(), key=lambda x: x["last_ts"], reverse=True)[:40]
        for s in sessions:
            s["project"] = _short_path(s["project"])

        return {
            "generated_at": now_ms,
            "today": today,
            "currency": pricing.get("currency", "CNY"),
            "overall": overall,
            "today_usage": today_bucket,
            "live": live,
            "by_day": day_rows,
            "by_hour": hour_rows,
            "by_model": sorted(by_model.values(), key=lambda x: x["total"], reverse=True),
            "sessions": sessions,
            "recent": recent,
            "codex_quota": quota,
            "stats": {
                "files_tracked": len(self.file_state),
                "calls_indexed": len(calls),
                "last_scan_ms": self.last_scan_ms,
                "scan_duration_ms": self.scan_duration_ms,
            },
        }


def _short_path(p: str) -> str:
    if not p:
        return ""
    p = str(p).replace("\\", "/")
    parts = [x for x in p.split("/") if x]
    return "/".join(parts[-2:]) if len(parts) > 2 else p


if __name__ == "__main__":
    col = Collector()
    col.scan()
    print(json.dumps(col.build_summary(), ensure_ascii=False, indent=2)[:4000])
