#!/usr/bin/env python3
"""Local cost and model-governance monitor for Codex.

The monitor stores the original user prompt locally so the dashboard can show
which question produced each token-usage row. Response bodies are not stored.
It records token counts, hashes, model/agent identifiers, and policy outcomes.
Exact token usage comes from ``codex exec --json`` (or compatible JSONL).  The
Codex lifecycle hook adapter records a clearly-labelled estimate because hook
payloads do not expose billing usage directly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import http.server
import json
import os
import pathlib
import sqlite3
import sys
import time
from typing import Any, Iterable, Optional
from urllib.parse import urlparse


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "cost-monitor.json"
DEFAULT_DB_PATH = ROOT / "data" / "cost_monitor.db"
SCHEMA_VERSION = 2
LOCAL_SYNC_FINGERPRINTS: dict[str, tuple[int, int]] = {}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def compact_hash(value: Any) -> str:
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def load_config(path: Optional[pathlib.Path] = None) -> dict[str, Any]:
    config_path = pathlib.Path(path or os.environ.get("CODEX_COST_MONITOR_CONFIG", DEFAULT_CONFIG_PATH))
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def db_path(path: Optional[pathlib.Path] = None) -> pathlib.Path:
    return pathlib.Path(path or os.environ.get("CODEX_COST_MONITOR_DB", DEFAULT_DB_PATH))


def connect_db(path: Optional[pathlib.Path] = None) -> sqlite3.Connection:
    target = db_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL UNIQUE,
            observed_at TEXT NOT NULL,
            completed_at TEXT,
            source TEXT NOT NULL,
            agent_id TEXT,
            session_id TEXT,
            thread_id TEXT,
            turn_id TEXT,
            agent_type TEXT,
            model TEXT,
            priced_model TEXT,
            status TEXT,
            basis TEXT NOT NULL,
            billing_profile TEXT,
            unit TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
            cost REAL,
            task_class TEXT,
            policy_decision TEXT,
            label TEXT,
            cwd TEXT,
            prompt_hash TEXT,
            prompt_text TEXT,
            source_ref TEXT,
            metadata_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_runs_observed_at ON runs(observed_at);
        CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_id);
        CREATE INDEX IF NOT EXISTS idx_runs_model ON runs(model);
        CREATE INDEX IF NOT EXISTS idx_runs_policy ON runs(policy_decision);

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_hash TEXT NOT NULL UNIQUE,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            event_name TEXT,
            session_id TEXT,
            thread_id TEXT,
            turn_id TEXT,
            agent_id TEXT,
            agent_type TEXT,
            model TEXT,
            cwd TEXT,
            payload_summary TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_observed_at ON events(observed_at);
        """
    )
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "cache_write_input_tokens" not in run_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN cache_write_input_tokens INTEGER NOT NULL DEFAULT 0")
    if "prompt_text" not in run_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN prompt_text TEXT")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def canonical_model(model: Optional[str], config: dict[str, Any]) -> Optional[str]:
    if not model:
        return None
    value = str(model).strip().lower()
    aliases = config.get("pricing", {}).get("aliases", {})
    return str(aliases.get(value, value))


def profile_info(config: dict[str, Any], profile: Optional[str] = None) -> tuple[str, dict[str, Any]]:
    pricing = config.get("pricing", {})
    name = profile or config.get("billing_profile", "openai_api_usd")
    profiles = pricing.get("profiles", {})
    if name not in profiles:
        raise ValueError(f"Unknown billing profile: {name}")
    return name, profiles[name]


def normalise_usage(raw: Any) -> Optional[dict[str, int]]:
    """Accept Codex CLI, app-server, and Responses-style usage spellings."""
    if not isinstance(raw, dict):
        return None

    input_tokens = raw.get("input_tokens", raw.get("inputTokens"))
    cached = raw.get("cached_input_tokens", raw.get("cachedInputTokens"))
    cache_write = raw.get("cache_write_input_tokens", raw.get("cacheWriteInputTokens"))
    output_tokens = raw.get("output_tokens", raw.get("outputTokens"))
    reasoning = raw.get("reasoning_output_tokens", raw.get("reasoningOutputTokens"))

    input_details = raw.get("input_tokens_details") or raw.get("inputTokensDetails") or {}
    output_details = raw.get("output_tokens_details") or raw.get("outputTokensDetails") or {}
    if cached is None and isinstance(input_details, dict):
        cached = input_details.get("cached_tokens", input_details.get("cachedTokens"))
    if cache_write is None and isinstance(input_details, dict):
        cache_write = input_details.get("cache_write_tokens", input_details.get("cacheWriteTokens"))
    if reasoning is None and isinstance(output_details, dict):
        reasoning = output_details.get("reasoning_tokens", output_details.get("reasoningTokens"))

    values = {
        "input_tokens": safe_int(input_tokens),
        "cached_input_tokens": safe_int(cached),
        "cache_write_input_tokens": safe_int(cache_write),
        "output_tokens": safe_int(output_tokens),
        "reasoning_output_tokens": safe_int(reasoning),
    }
    if not any(values.values()):
        return None
    values["cached_input_tokens"] = min(values["cached_input_tokens"], values["input_tokens"])
    return values


def calculate_cost(
    model: Optional[str],
    usage: dict[str, Any],
    config: dict[str, Any],
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """Calculate a token-based estimate using a named pricing profile.

    The result is intentionally explicit about whether the model is priced. A
    missing price never becomes zero, which prevents silent under-reporting.
    """
    profile_name, profile_data = profile_info(config, profile)
    unit = profile_data.get("unit", profile_name)
    normalised = normalise_usage(usage) or {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    priced_model = canonical_model(model, config)
    rates = profile_data.get("per_million_tokens", {}).get(priced_model or "")
    result: dict[str, Any] = {
        **normalised,
        "model": model,
        "priced_model": priced_model,
        "billing_profile": profile_name,
        "unit": unit,
        "cost": None,
        "basis": "unpriced" if not rates else "exact",
        "partial_pricing": False,
    }
    if not rates:
        return result

    cached_tokens = normalised["cached_input_tokens"]
    uncached_tokens = max(0, normalised["input_tokens"] - cached_tokens)
    cache_write_tokens = normalised["cache_write_input_tokens"]
    input_rate = safe_float(rates.get("input"))
    cached_rate = rates.get("cached_input")
    cached_rate = input_rate if cached_rate is None else safe_float(cached_rate)
    cache_write_rate = rates.get("cache_write_input")
    # A missing cache-write price means the provider's price card does not
    # expose that billing category for this model. Keep it uncharged rather
    # than silently treating it as ordinary input.
    cache_write_missing = cache_write_tokens > 0 and cache_write_rate is None
    cache_write_rate = 0.0 if cache_write_rate is None else safe_float(cache_write_rate)
    output_rate = safe_float(rates.get("output"))
    result["cost"] = round(
        (
            uncached_tokens * input_rate
            + cached_tokens * cached_rate
            + cache_write_tokens * cache_write_rate
            + normalised["output_tokens"] * output_rate
        )
        / 1_000_000,
        8,
    )
    if cache_write_missing:
        result["basis"] = "exact_partial"
        result["partial_pricing"] = True
    return result


def estimate_tokens(text: Optional[str], config: dict[str, Any]) -> int:
    if not text:
        return 0
    chars_per_token = max(1.0, safe_float(config.get("estimation", {}).get("chars_per_token"), 4.0))
    return max(1, int(round(len(text) / chars_per_token)))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def classify_task(prompt: Optional[str], config: dict[str, Any]) -> dict[str, Any]:
    text = (prompt or "").strip().lower()
    routing = config.get("routing", {})
    routine_keywords = [str(x).lower() for x in routing.get("routine_keywords", [])]
    complex_keywords = [str(x).lower() for x in routing.get("complex_keywords", [])]
    routine_hits = sorted({word for word in routine_keywords if word and word in text})
    complex_hits = sorted({word for word in complex_keywords if word and word in text})

    if complex_hits and len(complex_hits) >= len(routine_hits):
        task_class = "complex"
    elif routine_hits:
        task_class = "routine"
    else:
        task_class = "normal"

    confidence = 0.45
    if task_class == "routine":
        confidence = clamp(0.52 + 0.08 * len(routine_hits), 0.52, 0.9)
    elif task_class == "complex":
        confidence = clamp(0.56 + 0.08 * len(complex_hits), 0.56, 0.95)

    recommended = {
        "routine": routing.get("routine_model", routing.get("default_model")),
        "normal": routing.get("default_model"),
        "complex": routing.get("complex_model", routing.get("default_model")),
    }[task_class]
    return {
        "class": task_class,
        "confidence": round(confidence, 2),
        "routine_hits": routine_hits,
        "complex_hits": complex_hits,
        "recommended_model": recommended,
    }


def approximate_savings(
    prompt: Optional[str], active_model: Optional[str], recommended_model: Optional[str], config: dict[str, Any]
) -> Optional[dict[str, Any]]:
    if not active_model or not recommended_model:
        return None
    input_tokens = estimate_tokens(prompt, config)
    output_tokens = safe_int(config.get("estimation", {}).get("default_output_tokens"), 600)
    current = calculate_cost(
        active_model,
        {"input_tokens": input_tokens, "output_tokens": output_tokens},
        config,
    )
    recommended = calculate_cost(
        recommended_model,
        {"input_tokens": input_tokens, "output_tokens": output_tokens},
        config,
    )
    if current.get("cost") is None or recommended.get("cost") is None:
        return None
    return {
        "current": current["cost"],
        "recommended": recommended["cost"],
        "savings": round(current["cost"] - recommended["cost"], 8),
        "unit": current["unit"],
        "assumption": "prompt estimate + default output token budget",
    }


def assess_task(prompt: Optional[str], active_model: Optional[str], config: dict[str, Any]) -> dict[str, Any]:
    classification = classify_task(prompt, config)
    routing = config.get("routing", {})
    active_canonical = canonical_model(active_model, config)
    flagship = {canonical_model(model, config) for model in routing.get("flagship_models", [])}
    action = str(routing.get("routine_flagship_action", "warn")).lower()
    threshold = safe_float(routing.get("routine_threshold"), 0.55)
    is_routine_flagship = (
        classification["class"] == "routine"
        and classification["confidence"] >= threshold
        and active_canonical in flagship
    )
    if is_routine_flagship and action == "block":
        decision = "block"
    elif is_routine_flagship:
        decision = "warn"
    else:
        decision = "allow"
    savings = approximate_savings(prompt, active_model, classification["recommended_model"], config)
    return {
        **classification,
        "active_model": active_model,
        "active_canonical_model": active_canonical,
        "is_flagship": active_canonical in flagship,
        "decision": decision,
        "action_configured": action,
        "estimated_savings": savings,
    }


def upsert_run(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    fields = [
        "run_key",
        "observed_at",
        "completed_at",
        "source",
        "agent_id",
        "session_id",
        "thread_id",
        "turn_id",
        "agent_type",
        "model",
        "priced_model",
        "status",
        "basis",
        "billing_profile",
        "unit",
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "cost",
        "task_class",
        "policy_decision",
        "label",
        "cwd",
        "prompt_hash",
        "prompt_text",
        "source_ref",
        "metadata_json",
    ]
    values = [record.get(field) for field in fields]
    placeholders = ",".join("?" for _ in fields)
    updates = ",".join(f"{field}=excluded.{field}" for field in fields[1:])
    conn.execute(
        f"INSERT INTO runs({','.join(fields)}) VALUES({placeholders}) "
        f"ON CONFLICT(run_key) DO UPDATE SET {updates}",
        values,
    )


def record_event(conn: sqlite3.Connection, payload: dict[str, Any], source: str, summary: dict[str, Any]) -> None:
    event_hash = compact_hash({"source": source, "payload": payload})
    conn.execute(
        """
        INSERT OR IGNORE INTO events(
            event_hash, observed_at, source, event_name, session_id, thread_id,
            turn_id, agent_id, agent_type, model, cwd, payload_summary
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_hash,
            utc_now(),
            source,
            summary.get("event_name"),
            summary.get("session_id"),
            summary.get("thread_id"),
            summary.get("turn_id"),
            summary.get("agent_id"),
            summary.get("agent_type"),
            summary.get("model"),
            summary.get("cwd"),
            json_dumps(summary.get("payload_summary", {})),
        ),
    )


def event_context(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
    return {
        "event_name": event.get("type") or event.get("method") or "unknown",
        "session_id": event.get("session_id") or params.get("sessionId") or params.get("session_id"),
        "thread_id": event.get("thread_id") or params.get("threadId") or params.get("thread_id"),
        "turn_id": event.get("turn_id") or params.get("turnId") or params.get("turn_id") or turn.get("id"),
        "agent_id": event.get("agent_id") or params.get("agentId") or params.get("agent_id"),
        "agent_type": event.get("agent_type") or params.get("agentType") or params.get("agent_type"),
        "model": event.get("model") or params.get("model") or turn.get("model"),
        "cwd": event.get("cwd") or params.get("cwd") or turn.get("cwd"),
    }


def usage_from_event(event: dict[str, Any]) -> Optional[dict[str, int]]:
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
    candidates = [
        event.get("usage"),
        event.get("tokenUsage"),
        event.get("turn", {}).get("usage") if isinstance(event.get("turn"), dict) else None,
        event.get("turn", {}).get("tokenUsage") if isinstance(event.get("turn"), dict) else None,
        turn.get("usage"),
        turn.get("tokenUsage"),
        params.get("usage"),
        params.get("tokenUsage"),
        params.get("token_usage"),
    ]
    for candidate in candidates:
        normalised = normalise_usage(candidate)
        if normalised:
            return normalised
    return None


def is_completed_usage_event(event: dict[str, Any]) -> bool:
    value = str(event.get("type") or event.get("method") or "").lower()
    return value in {"turn.completed", "turn/completed", "turn_completed"}


def import_jsonl(
    source_file: str,
    agent_id: Optional[str],
    model: Optional[str],
    label: Optional[str],
    source: str,
    config: dict[str, Any],
    conn: sqlite3.Connection,
) -> dict[str, int]:
    imported = 0
    usage_events = 0
    event_count = 0
    path = pathlib.Path(source_file) if source_file != "-" else None
    handle = sys.stdin if source_file == "-" else path.open("r", encoding="utf-8", errors="replace")
    try:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_count += 1
            context = event_context(event)
            usage = usage_from_event(event)
            turn_object = event.get("turn") if isinstance(event.get("turn"), dict) else {}
            summary = {
                **context,
                "payload_summary": {
                    "type": event.get("type"),
                    "method": event.get("method"),
                    "status": event.get("status") or turn_object.get("status"),
                    "has_usage": bool(usage),
                    "usage_keys": sorted(usage.keys()) if usage else [],
                },
            }
            record_event(conn, event, source, summary)
            if not (usage and is_completed_usage_event(event)):
                continue
            usage_events += 1
            chosen_model = context.get("model") or model
            chosen_agent = context.get("agent_id") or agent_id or context.get("thread_id") or "codex-exec"
            thread_id = context.get("thread_id")
            turn_id = context.get("turn_id")
            source_ref = f"{path.resolve()}:{line_number}" if path else f"stdin:{line_number}"
            # Prefer a cross-adapter logical key when the protocol supplies a
            # stable thread+turn pair. This prevents importing the same turn
            # once from exec JSONL and once from an app-server capture from
            # doubling the bill. If the IDs are absent, keep the source-line
            # key because identical usage can legitimately occur in separate
            # turns.
            if thread_id and turn_id:
                run_key = f"exact:{thread_id}:{turn_id}:{compact_hash(usage)}"
            else:
                run_key = f"exact:{compact_hash({'source_ref': source_ref, 'event': event})}"
            priced = calculate_cost(chosen_model, usage, config)
            record = {
                "run_key": run_key,
                "observed_at": utc_now(),
                "completed_at": utc_now(),
                "source": source,
                "agent_id": chosen_agent,
                "session_id": context.get("session_id"),
                "thread_id": thread_id,
                "turn_id": turn_id,
                "agent_type": context.get("agent_type"),
                "model": chosen_model,
                "priced_model": priced.get("priced_model"),
                "status": "completed",
                "basis": priced.get("basis", "unpriced"),
                "billing_profile": priced.get("billing_profile"),
                "unit": priced.get("unit"),
                "input_tokens": priced.get("input_tokens", 0),
                "cached_input_tokens": priced.get("cached_input_tokens", 0),
                "cache_write_input_tokens": priced.get("cache_write_input_tokens", 0),
                "output_tokens": priced.get("output_tokens", 0),
                "reasoning_output_tokens": priced.get("reasoning_output_tokens", 0),
                "cost": priced.get("cost"),
                "task_class": "unrated",
                "policy_decision": "unrated",
                "label": label,
                "cwd": context.get("cwd"),
                "prompt_hash": None,
                "source_ref": source_ref,
                "metadata_json": json_dumps({"event_type": event.get("type"), "event_method": event.get("method")}),
            }
            upsert_run(conn, record)
            imported += 1
    finally:
        if path:
            handle.close()
    conn.commit()
    return {"events": event_count, "usage_events": usage_events, "runs": imported}


def local_codex_home(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    configured = home or os.environ.get("CODEX_HOME")
    return pathlib.Path(configured) if configured else pathlib.Path.home() / ".codex"


def zero_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }


def usage_delta(current: Optional[dict[str, int]], baseline: Optional[dict[str, int]]) -> Optional[dict[str, int]]:
    if not current:
        return None
    baseline = baseline or zero_usage()
    result = {
        key: max(0, safe_int(current.get(key)) - safe_int(baseline.get(key)))
        for key in zero_usage()
    }
    if not any(result.values()):
        return None
    result["cached_input_tokens"] = min(result["cached_input_tokens"], result["input_tokens"])
    return result


def local_event_time(event: dict[str, Any], fallback: Optional[str] = None) -> str:
    value = event.get("timestamp")
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).replace(microsecond=0).isoformat()
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc).isoformat()
        except ValueError:
            pass
    return fallback or utc_now()


def local_thread_inventory(days: int = 30, home: Optional[pathlib.Path] = None) -> list[dict[str, Any]]:
    """Find recent Codex desktop/CLI rollout files without reading prompt bodies into the ledger."""
    codex_home = local_codex_home(home)
    since_ms = int((time.time() - max(1, days) * 86400) * 1000)
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    state_path = codex_home / "state_5.sqlite"
    if state_path.exists():
        try:
            state_conn = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True, timeout=0.5)
            rows = state_conn.execute(
                """
                SELECT id, rollout_path, source, model, cwd, agent_nickname, agent_role, updated_at_ms
                FROM threads
                WHERE COALESCE(updated_at_ms, 0) >= ?
                ORDER BY updated_at_ms DESC
                LIMIT 200
                """,
                (since_ms,),
            ).fetchall()
            state_conn.close()
        except sqlite3.Error:
            rows = []
        for row in rows:
            rollout_value = str(row[1] or "")
            if rollout_value.startswith("\\\\?\\"):
                rollout_value = rollout_value[4:]
            rollout_path = pathlib.Path(rollout_value)
            if not rollout_path.exists():
                continue
            key = str(rollout_path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            source = row[2]
            inventory.append(
                {
                    "thread_id": row[0],
                    "rollout_path": rollout_path,
                    "source": source,
                    "model": row[3],
                    "cwd": row[4],
                    "agent_nickname": row[5],
                    "agent_role": row[6],
                }
            )

    # Fallback for older installations that do not have the state index.
    if not inventory:
        sessions = codex_home / "sessions"
        if sessions.exists():
            for rollout_path in sessions.glob("**/rollout-*.jsonl"):
                try:
                    if rollout_path.stat().st_mtime * 1000 < since_ms:
                        continue
                except OSError:
                    continue
                key = str(rollout_path.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                inventory.append(
                    {
                        "thread_id": rollout_path.stem[-36:],
                        "rollout_path": rollout_path,
                        "source": "codex-local",
                        "model": None,
                        "cwd": None,
                        "agent_nickname": None,
                        "agent_role": None,
                    }
                )
    return inventory


def local_agent_identity(thread: dict[str, Any]) -> tuple[str, str]:
    source = thread.get("source")
    nickname = thread.get("agent_nickname")
    role = thread.get("agent_role")
    if isinstance(source, str) and source.startswith("{"):
        try:
            parsed = json.loads(source)
            spawn = parsed.get("subagent", {}).get("thread_spawn", {})
            if isinstance(spawn, dict):
                nickname = nickname or spawn.get("agent_nickname")
                role = role or spawn.get("agent_role")
        except (TypeError, json.JSONDecodeError):
            pass
    return str(nickname or role or "codex-main"), str(role or "main")


def local_run_record(
    thread: dict[str, Any],
    active: dict[str, Any],
    usage: Optional[dict[str, int]],
    config: dict[str, Any],
    status: str,
    source_line: int,
    observed_at: str,
    completed_at: Optional[str],
) -> Optional[dict[str, Any]]:
    if not usage:
        return None
    model = active.get("model") or thread.get("model")
    priced = calculate_cost(model, usage, config)
    prompt = active.get("prompt")
    assessment = assess_task(prompt, model, config) if prompt and model else None
    agent_id, agent_type = local_agent_identity(thread)
    basis = "local_snapshot" if priced.get("cost") is not None else "unpriced"
    rollout_path = pathlib.Path(thread["rollout_path"])
    return {
        "run_key": f"local:{thread.get('thread_id')}:{active.get('turn_id')}",
        "observed_at": observed_at,
        "completed_at": completed_at,
        "source": "codex-local-transcript",
        "agent_id": agent_id,
        "session_id": thread.get("thread_id"),
        "thread_id": thread.get("thread_id"),
        "turn_id": active.get("turn_id"),
        "agent_type": agent_type,
        "model": model,
        "priced_model": priced.get("priced_model"),
        "status": status,
        "basis": basis,
        "billing_profile": priced.get("billing_profile"),
        "unit": priced.get("unit"),
        "input_tokens": priced.get("input_tokens", 0),
        "cached_input_tokens": priced.get("cached_input_tokens", 0),
        "cache_write_input_tokens": priced.get("cache_write_input_tokens", 0),
        "output_tokens": priced.get("output_tokens", 0),
        "reasoning_output_tokens": priced.get("reasoning_output_tokens", 0),
        "cost": priced.get("cost"),
        "task_class": assessment.get("class") if assessment else "unrated",
        "policy_decision": assessment.get("decision") if assessment else "unrated",
        "label": "local Codex task",
        "cwd": active.get("cwd") or thread.get("cwd"),
        "prompt_hash": compact_hash(prompt) if prompt else None,
        "prompt_text": prompt,
        "source_ref": f"{rollout_path}:{source_line}",
        "metadata_json": json_dumps(
            {
                "collector": "codex_local_transcript",
                "in_progress": status == "running",
                "source_line": source_line,
                "rollout_path": str(rollout_path),
            }
        ),
    }


def import_local_rollout(thread: dict[str, Any], config: dict[str, Any], conn: sqlite3.Connection) -> dict[str, int]:
    """Import token_count snapshots from a Codex local rollout transcript."""
    path = pathlib.Path(thread["rollout_path"])
    latest_total: Optional[dict[str, int]] = None
    active: Optional[dict[str, Any]] = None
    current_model = thread.get("model")
    current_cwd = thread.get("cwd")
    records: list[dict[str, Any]] = []
    event_count = 0
    try:
        lines = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {"events": 0, "runs": 0, "running": 0}
    with lines:
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            event_count += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            event_type = payload.get("type")
            if event_type == "thread_settings_applied":
                settings = payload.get("thread_settings") if isinstance(payload.get("thread_settings"), dict) else {}
                current_model = settings.get("model") or current_model
                current_cwd = settings.get("cwd") or current_cwd
                continue
            if event_type == "task_started":
                if active:
                    interrupted_usage = usage_delta(active.get("latest_total") or latest_total, active.get("baseline"))
                    record = local_run_record(
                        thread,
                        active,
                        interrupted_usage,
                        config,
                        "interrupted",
                        active.get("source_line", line_number),
                        active.get("observed_at") or local_event_time(event),
                        None,
                    )
                    if record:
                        records.append(record)
                active = {
                    "turn_id": payload.get("turn_id"),
                    "model": current_model,
                    "cwd": current_cwd,
                    "baseline": dict(latest_total or zero_usage()),
                    "latest_total": None,
                    "prompt": None,
                    "observed_at": local_event_time(event),
                    "source_line": line_number,
                }
                continue
            if event_type == "user_message" and active and not active.get("prompt"):
                active["prompt"] = payload.get("message")
                continue
            if event_type == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                total = normalise_usage(info.get("total_token_usage"))
                if total:
                    latest_total = total
                    if active:
                        active["latest_total"] = total
                        active["observed_at"] = local_event_time(event, active.get("observed_at"))
                        active["source_line"] = line_number
                continue
            if event_type == "task_complete" and active:
                if payload.get("turn_id") and payload.get("turn_id") != active.get("turn_id"):
                    continue
                completed_usage = usage_delta(active.get("latest_total") or latest_total, active.get("baseline"))
                record = local_run_record(
                    thread,
                    active,
                    completed_usage,
                    config,
                    "completed",
                    line_number,
                    local_event_time(event, active.get("observed_at")),
                    local_event_time(event, active.get("observed_at")),
                )
                if record:
                    records.append(record)
                active = None

    if active:
        running_usage = usage_delta(active.get("latest_total") or latest_total, active.get("baseline"))
        record = local_run_record(
            thread,
            active,
            running_usage,
            config,
            "running",
            active.get("source_line", 0),
            active.get("observed_at") or utc_now(),
            None,
        )
        if record:
            records.append(record)

    for record in records:
        upsert_run(conn, record)
    return {
        "events": event_count,
        "runs": len(records),
        "running": sum(1 for record in records if record.get("status") == "running"),
    }


def sync_local_codex(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    days: int = 30,
    home: Optional[pathlib.Path] = None,
) -> dict[str, Any]:
    """Sync recent Codex desktop/CLI local transcripts into the ledger."""
    settings = config.get("local_sync", {})
    if settings.get("enabled", True) is False:
        return {"enabled": False, "threads": 0, "runs": 0, "running": 0}
    codex_home = local_codex_home(home or settings.get("codex_home"))
    inventory = local_thread_inventory(days, codex_home)
    result: dict[str, Any] = {
        "enabled": True,
        "home": str(codex_home),
        "threads": len(inventory),
        "runs": 0,
        "running": 0,
        "skipped": 0,
        "errors": [],
    }
    for thread in inventory:
        rollout_path = pathlib.Path(thread["rollout_path"])
        fingerprint_key = str(rollout_path.resolve()).lower()
        try:
            stat = rollout_path.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            result["errors"].append(f"{thread.get('thread_id')}: {exc}")
            continue
        if LOCAL_SYNC_FINGERPRINTS.get(fingerprint_key) == fingerprint:
            result["skipped"] += 1
            continue
        try:
            imported = import_local_rollout(thread, config, conn)
            result["runs"] += imported["runs"]
            LOCAL_SYNC_FINGERPRINTS[fingerprint_key] = fingerprint
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            result["errors"].append(f"{thread.get('thread_id')}: {exc}")
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    result["running"] = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE source='codex-local-transcript' AND status='running' AND observed_at >= ?",
        (since,),
    ).fetchone()[0]
    conn.commit()
    return result


def find_run(conn: sqlite3.Connection, run_key: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE run_key=?", (run_key,)).fetchone()


def process_hook(payload: dict[str, Any], config: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    event_name = str(payload.get("hook_event_name") or "unknown")
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    agent_id = payload.get("agent_id")
    agent_type = payload.get("agent_type")
    model = payload.get("model")
    cwd = payload.get("cwd")
    # Hook docs define session_id as the parent session for subagents; it is
    # useful for correlation but must not be presented as the child thread ID.
    hook_thread_id = None if event_name in {"SubagentStart", "SubagentStop"} else session_id
    summary = {
        "event_name": event_name,
        "session_id": session_id,
        "thread_id": hook_thread_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "model": model,
        "cwd": cwd,
        "payload_summary": {
            "permission_mode": payload.get("permission_mode"),
            "source": payload.get("source"),
            "reason": payload.get("reason"),
            "prompt_chars": len(payload.get("prompt") or "") if event_name == "UserPromptSubmit" else None,
            "last_message_chars": len(payload.get("last_assistant_message") or "")
            if event_name in {"Stop", "SubagentStop"}
            else None,
        },
    }
    record_event(conn, payload, "hook", summary)

    if event_name == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "")
        assessment = assess_task(prompt, model, config)
        run_key = f"hook:root:{session_id}:{turn_id or compact_hash(prompt)}"
        input_tokens = estimate_tokens(prompt, config)
        profile_name, profile_data = profile_info(config)
        priced = calculate_cost(model, {"input_tokens": input_tokens}, config)
        record = {
            "run_key": run_key,
            "observed_at": utc_now(),
            "completed_at": None,
            "source": "hook",
            "agent_id": session_id or "codex-session",
            "session_id": session_id,
            "thread_id": session_id,
            "turn_id": turn_id,
            "agent_type": None,
            "model": model,
            "priced_model": priced.get("priced_model"),
            "status": "blocked" if assessment["decision"] == "block" else "started",
            "basis": "not_run" if assessment["decision"] == "block" else "estimated",
            "billing_profile": profile_name,
            "unit": profile_data.get("unit"),
            "input_tokens": input_tokens,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "cost": 0.0 if assessment["decision"] == "block" else None,
            "task_class": assessment["class"],
            "policy_decision": assessment["decision"],
            "label": "interactive",
            "cwd": cwd,
            "prompt_hash": compact_hash(prompt),
            "prompt_text": prompt,
            "source_ref": payload.get("transcript_path"),
            "metadata_json": json_dumps(
                {
                    "estimate": True,
                    "prompt_chars": len(prompt),
                    "confidence": assessment["confidence"],
                    "recommended_model": assessment["recommended_model"],
                    "estimated_savings": assessment.get("estimated_savings"),
                }
            ),
        }
        upsert_run(conn, record)
        conn.commit()
        return assessment

    if event_name in {"Stop", "SubagentStop"}:
        if event_name == "SubagentStop":
            run_key = f"hook:subagent:{agent_id or 'unknown'}:{turn_id or 'unknown'}"
            chosen_agent = agent_id or "subagent"
            chosen_type = agent_type
        else:
            run_key = f"hook:root:{session_id}:{turn_id or 'unknown'}"
            chosen_agent = session_id or "codex-session"
            chosen_type = None
        existing = find_run(conn, run_key)
        last_message = str(payload.get("last_assistant_message") or "")
        output_tokens = estimate_tokens(last_message, config)
        input_tokens = safe_int(existing["input_tokens"]) if existing else 0
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        priced = calculate_cost(model or (existing["model"] if existing else None), usage, config)
        record = {
            "run_key": run_key,
            "observed_at": existing["observed_at"] if existing else utc_now(),
            "completed_at": utc_now(),
            "source": "hook",
            "agent_id": chosen_agent,
            "session_id": session_id,
            "thread_id": None if event_name == "SubagentStop" else session_id,
            "turn_id": turn_id,
            "agent_type": chosen_type,
            "model": model or (existing["model"] if existing else None),
            "priced_model": priced.get("priced_model") or (existing["priced_model"] if existing else None),
            "status": "completed",
            "basis": "estimated" if existing else "estimated_lower_bound",
            "billing_profile": priced.get("billing_profile"),
            "unit": priced.get("unit"),
            "input_tokens": input_tokens,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": 0,
            "cost": priced.get("cost"),
            "task_class": existing["task_class"] if existing else "unrated",
            "policy_decision": existing["policy_decision"] if existing else "unrated",
            "label": existing["label"] if existing else "subagent",
            "cwd": cwd or (existing["cwd"] if existing else None),
            "prompt_hash": existing["prompt_hash"] if existing else None,
            "prompt_text": existing["prompt_text"] if existing else None,
            "source_ref": payload.get("agent_transcript_path") or (existing["source_ref"] if existing else None),
            "metadata_json": json_dumps(
                {
                    "estimate": True,
                    "last_message_chars": len(last_message),
                    "missing_exact_usage": True,
                }
            ),
        }
        upsert_run(conn, record)
        conn.commit()
        return {"decision": "allow"}

    if event_name == "SubagentStart":
        run_key = f"hook:subagent:{agent_id or 'unknown'}:{turn_id or 'unknown'}"
        profile_name, profile_data = profile_info(config)
        upsert_run(
            conn,
            {
                "run_key": run_key,
                "observed_at": utc_now(),
                "completed_at": None,
                "source": "hook",
                "agent_id": agent_id or "subagent",
                "session_id": session_id,
                "thread_id": None,
                "turn_id": turn_id,
                "agent_type": agent_type,
                "model": model,
                "priced_model": canonical_model(model, config),
                "status": "started",
                "basis": "estimated_lower_bound",
                "billing_profile": profile_name,
                "unit": profile_data.get("unit"),
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "cost": None,
                "task_class": "unrated",
                "policy_decision": "allow",
                "label": "subagent",
                "cwd": cwd,
                "prompt_hash": None,
                "prompt_text": payload.get("prompt"),
                "source_ref": payload.get("transcript_path"),
                "metadata_json": json_dumps({"estimate": True, "subagent_start": True}),
            },
        )
        conn.commit()
    else:
        conn.commit()
    return {"decision": "allow"}


def hook_output(assessment: dict[str, Any]) -> Optional[dict[str, Any]]:
    decision = assessment.get("decision")
    if decision == "block":
        recommended = assessment.get("recommended_model")
        return {
            "decision": "block",
            "reason": (
                f"成本策略阻止了普通任务使用旗舰模型 {assessment.get('active_model') or 'unknown'}。"
                f"请切换到 {recommended}，或把任务明确为复杂/高风险工作后重试。"
            ),
        }
    if decision == "warn":
        savings = assessment.get("estimated_savings") or {}
        savings_text = ""
        if savings.get("savings") is not None:
            savings_text = f"；本次粗略可节省约 {savings['savings']:.4f} {savings.get('unit', '')}"
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    f"成本监控提醒：当前使用 {assessment.get('active_model') or 'unknown'}，"
                    f"任务被判定为 routine（置信度 {assessment.get('confidence'):.2f}）。"
                    f"建议使用 {assessment.get('recommended_model')}{savings_text}。"
                    "若任务实际涉及复杂架构、生产风险或多步骤调试，请在提示中明确说明。"
                ),
            }
        }
    return None


def format_cost(value: Optional[float], unit: Optional[str]) -> str:
    if value is None:
        return "—"
    if unit == "USD":
        return f"${value:,.4f}"
    return f"{value:,.2f} {unit or ''}".strip()


def rows_to_dict(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def run_precedence(row: dict[str, Any]) -> int:
    basis = str(row.get("basis") or "")
    if basis in {"exact", "exact_partial"}:
        return 0
    if basis == "local_snapshot":
        return 1
    if basis.startswith("estimated"):
        return 2
    return 3


def deduplicate_logical_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prefer exact usage, then local snapshots, over hook estimates per turn."""
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    passthrough: list[dict[str, Any]] = []
    for row in rows:
        thread_id = row.get("thread_id")
        turn_id = row.get("turn_id")
        if not thread_id or not turn_id:
            passthrough.append(row)
            continue
        key = (str(thread_id), str(turn_id))
        current = selected.get(key)
        if current is None or run_precedence(row) < run_precedence(current):
            selected[key] = row
    return passthrough + list(selected.values())


def build_summary(conn: sqlite3.Connection, config: dict[str, Any], days: int = 30) -> dict[str, Any]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    rows = deduplicate_logical_runs(
        rows_to_dict(conn.execute("SELECT * FROM runs WHERE observed_at >= ? ORDER BY observed_at DESC", (since,)).fetchall())
    )
    for row in rows:
        row["total_tokens"] = safe_int(row.get("input_tokens")) + safe_int(row.get("output_tokens"))
    total_tokens = sum(safe_int(row.get("total_tokens")) for row in rows)
    priced_rows = [row for row in rows if row.get("cost") is not None]
    units: dict[str, dict[str, Any]] = {}
    for row in priced_rows:
        key = row.get("unit") or row.get("billing_profile") or "unknown"
        bucket = units.setdefault(key, {"unit": key, "cost": 0.0, "runs": 0, "exact_runs": 0, "estimated_runs": 0})
        bucket["cost"] += safe_float(row.get("cost"))
        bucket["runs"] += 1
        if row.get("basis") == "exact":
            bucket["exact_runs"] += 1
        else:
            bucket["estimated_runs"] += 1
    for bucket in units.values():
        bucket["cost"] = round(bucket["cost"], 8)

    def aggregate(field: str) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in priced_rows:
            key = row.get(field) or "unknown"
            unit = row.get("unit") or "unknown"
            group_key = f"{key}:{unit}"
            item = grouped.setdefault(
                group_key,
                {field: key, "unit": unit, "cost": 0.0, "runs": 0, "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0},
            )
            item["cost"] += safe_float(row.get("cost"))
            item["runs"] += 1
            item["input_tokens"] += safe_int(row.get("input_tokens"))
            item["cached_input_tokens"] += safe_int(row.get("cached_input_tokens"))
            item["cache_write_input_tokens"] += safe_int(row.get("cache_write_input_tokens"))
            item["output_tokens"] += safe_int(row.get("output_tokens"))
        for item in grouped.values():
            item["cost"] = round(item["cost"], 8)
        return sorted(grouped.values(), key=lambda item: item["cost"], reverse=True)

    def aggregate_tokens(field: str) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get(field) or "unknown")
            item = grouped.setdefault(key, {field: key, "runs": 0, "total_tokens": 0})
            item["runs"] += 1
            item["total_tokens"] += safe_int(row.get("total_tokens"))
        return sorted(grouped.values(), key=lambda item: item["total_tokens"], reverse=True)

    exact_count = sum(1 for row in rows if row.get("basis") == "exact")
    partial_count = sum(1 for row in rows if row.get("basis") == "exact_partial")
    estimated_count = sum(1 for row in rows if str(row.get("basis", "")).startswith("estimated"))
    local_snapshot_count = sum(1 for row in rows if row.get("basis") == "local_snapshot")
    unpriced_count = sum(1 for row in rows if row.get("basis") == "unpriced" or row.get("cost") is None)
    warnings = [
        row
        for row in rows
        if row.get("policy_decision") in {"warn", "block"}
        or row.get("basis") == "unpriced"
    ]
    flagship_models = {
        canonical_model(model, config) for model in config.get("routing", {}).get("flagship_models", [])
    }
    unit_totals: dict[str, float] = {}
    unit_flagship: dict[str, float] = {}
    for row in priced_rows:
        unit = row.get("unit") or "unknown"
        value = safe_float(row.get("cost"))
        unit_totals[unit] = unit_totals.get(unit, 0.0) + value
        if canonical_model(row.get("model"), config) in flagship_models:
            unit_flagship[unit] = unit_flagship.get(unit, 0.0) + value
    flagship_share_by_unit = {
        unit: round((unit_flagship.get(unit, 0.0) / total) if total else 0.0, 4)
        for unit, total in unit_totals.items()
    }
    flagship_share = next(iter(flagship_share_by_unit.values())) if len(flagship_share_by_unit) == 1 else None
    budgets = config.get("routing", {}).get("budgets", {})
    budget_alerts: list[dict[str, Any]] = []
    daily_rows = [row for row in priced_rows if str(row.get("observed_at", ""))[:10] == utc_now()[:10]]
    daily_by_unit: dict[str, float] = {}
    for row in daily_rows:
        unit = row.get("unit") or "unknown"
        daily_by_unit[unit] = daily_by_unit.get(unit, 0.0) + safe_float(row.get("cost"))
    daily_budget = safe_float(budgets.get("daily"))
    for unit, value in daily_by_unit.items():
        if daily_budget and value > daily_budget:
            budget_alerts.append({"type": "daily", "unit": unit, "value": value, "limit": daily_budget})
    if flagship_share is not None and safe_float(budgets.get("flagship_share")) and flagship_share > safe_float(budgets.get("flagship_share")):
        budget_alerts.append(
            {
                "type": "flagship_share",
                "value": flagship_share,
                "limit": safe_float(budgets.get("flagship_share")),
            }
        )

    return {
        "generated_at": utc_now(),
        "window_days": days,
        "billing_profile": config.get("billing_profile"),
        "units": list(units.values()),
        "run_count": len(rows),
        "total_tokens": total_tokens,
        "exact_count": exact_count,
        "partial_count": partial_count,
        "estimated_count": estimated_count,
        "local_snapshot_count": local_snapshot_count,
        "unpriced_count": unpriced_count,
        "by_agent": aggregate("agent_id"),
        "by_model": aggregate("model"),
        "by_agent_tokens": aggregate_tokens("agent_id"),
        "by_model_tokens": aggregate_tokens("model"),
        "by_task_class": aggregate("task_class"),
        "recent_runs": rows[:50],
        "warning_count": len(warnings),
        "warnings": warnings[:50],
        "flagship_share": round(flagship_share, 4) if flagship_share is not None else None,
        "flagship_share_by_unit": flagship_share_by_unit,
        "budget_alerts": budget_alerts,
    }


def print_report(summary: dict[str, Any]) -> None:
    print(f"AI 成本监控（最近 {summary['window_days']} 天）")
    print(
        f"运行数: {summary['run_count']} | 精确: {summary['exact_count']} | "
        f"部分计价: {summary.get('partial_count', 0)} | 估算: {summary['estimated_count']} | "
        f"本地快照: {summary.get('local_snapshot_count', 0)} | 未定价: {summary['unpriced_count']}"
    )
    if summary["units"]:
        print("\n成本总计:")
        for unit in summary["units"]:
            print(f"  {unit['unit']}: {format_cost(unit['cost'], unit['unit'])} ({unit['runs']} runs)")
    else:
        print("\n成本总计: 暂无数据")

    for title, key in (("按 Agent", "by_agent"), ("按模型", "by_model"), ("按任务类型", "by_task_class")):
        print(f"\n{title}:")
        values = summary[key]
        if not values:
            print("  暂无数据")
            continue
        for item in values[:15]:
            name = item.get("agent_id") or item.get("model") or item.get("task_class") or "unknown"
            print(
                f"  {name}: {format_cost(item['cost'], item.get('unit'))} "
                f"| runs={item['runs']} | in={item['input_tokens']} | cached={item['cached_input_tokens']} | out={item['output_tokens']}"
            )
    flagship_share = summary.get("flagship_share")
    print(f"\n旗舰模型成本占比: {flagship_share:.1%}" if flagship_share is not None else "\n旗舰模型成本占比: 多计费单位，分别查看 dashboard")
    if summary["budget_alerts"]:
        print("预算告警:")
        for alert in summary["budget_alerts"]:
            print(f"  {alert}")
    if summary["warning_count"]:
        print(f"策略/数据告警: {summary['warning_count']} 条（dashboard 可查看明细）")


def html_dashboard() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex AI 成本监控</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151d33;--muted:#91a0bf;--text:#edf2ff;--accent:#8b9cff;--warn:#ffca65;--bad:#ff7e8a;--line:#293653}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0b1020,#111a2d);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1240px;margin:0 auto;padding:32px 22px 60px}h1{font-size:28px;margin:0 0 6px}h2{font-size:17px;margin:0 0 14px}.sub{color:var(--muted);margin:0 0 24px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0}.card,.panel{background:rgba(21,29,51,.94);border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:0 12px 30px #05081255}.metric{font-size:26px;font-weight:700;margin-top:6px}.label{color:var(--muted);font-size:12px}.layout{display:grid;grid-template-columns:1fr 1fr;gap:14px}.wide{grid-column:1/-1}.bar{height:9px;background:#202c48;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#70e1d1);border-radius:inherit}.notice{border-left:3px solid var(--warn);padding:10px 12px;background:#3b301633;margin:8px 0;border-radius:6px}.notice.bad{border-left-color:var(--bad);background:#4a202633}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;font-size:12px}.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;font-size:11px;color:var(--muted)}.exact{color:#71e0c2}.estimated{color:var(--warn)}.unpriced{color:var(--bad)}@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.layout{grid-template-columns:1fr}}@media(max-width:560px){.grid{grid-template-columns:1fr}main{padding:22px 12px}}
</style></head>
<body><main><h1>Codex AI 成本监控</h1><p class="sub">按 Agent、模型、任务类型归因；精确 token 与 hook 估算分开显示。</p>
<section id="metrics" class="grid"></section><section class="layout">
<div class="panel"><h2>按 Agent</h2><div id="agents"></div></div>
<div class="panel"><h2>按模型</h2><div id="models"></div></div>
<div class="panel wide"><h2>策略与预算</h2><div id="alerts"></div></div>
<div class="panel wide"><h2>最近运行</h2><div id="runs"></div></div>
</section></main>
<script>
const money=(n,u)=>n==null?'—':u==='USD'?('$'+Number(n).toFixed(4)):(Number(n).toFixed(2)+' '+(u||''));
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const table=(rows,cols)=>rows.length?`<table><thead><tr>${cols.map(c=>`<th>${c[1]}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c[2]?c[2](r):esc(r[c[0]])}</td>`).join('')}</tr>`).join('')}</tbody></table>`:'<p class="muted">暂无数据</p>';
fetch('/api/summary').then(r=>r.json()).then(s=>{
 const unit=s.units[0]?.unit||'USD', total=s.units.length===1?s.units[0].cost:null;
 document.querySelector('#metrics').innerHTML=[['总成本',total==null?'多单位':money(total,unit)],['运行数',s.run_count],['精确/估算',`${s.exact_count} / ${s.estimated_count}`],['旗舰占比',s.flagship_share==null?'—':(s.flagship_share*100).toFixed(1)+'%']].map(x=>`<div class="card"><div class="label">${x[0]}</div><div class="metric">${x[1]}</div></div>`).join('');
 document.querySelector('#agents').innerHTML=table(s.by_agent,[['agent_id','Agent'],['cost','成本',r=>money(r.cost,r.unit)],['runs','Runs'],['output_tokens','输出 tokens']]);
 document.querySelector('#models').innerHTML=table(s.by_model,[['model','模型'],['cost','成本',r=>money(r.cost,r.unit)],['runs','Runs'],['output_tokens','输出 tokens']]);
 const alerts=[...(s.budget_alerts||[]),...(s.warnings||[]).slice(0,12)];
 document.querySelector('#alerts').innerHTML=alerts.length?alerts.map(a=>`<div class="notice ${a.policy_decision==='block'?'bad':''}">${esc(a.type||a.policy_decision||'warning')} ${esc(a.agent_id||a.model||'')} ${a.cost!=null?money(a.cost,a.unit):''} ${a.limit!=null?`limit=${a.limit}`:''}</div>`).join(''):`<p class="exact">当前没有预算或策略告警。</p>`;
 document.querySelector('#runs').innerHTML=table(s.recent_runs,[['observed_at','时间'],['agent_id','Agent'],['model','模型'],['basis','依据',r=>`<span class="${r.basis==='exact'?'exact':r.basis?.startsWith('estimated')?'estimated':'unpriced'}">${esc(r.basis)}</span>`],['cost','成本',r=>money(r.cost,r.unit)],['task_class','任务']]);
}).catch(e=>document.body.innerHTML+='<pre>'+esc(e)+'</pre>');
</script></body></html>"""


def html_dashboard_simple() -> str:
    """A compact browser UI for the local launcher."""
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex AI 成本监控</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151d33;--panel2:#1b2742;--muted:#91a0bf;--text:#edf2ff;--accent:#8b9cff;--good:#71e0c2;--warn:#ffca65;--bad:#ff7e8a;--line:#293653}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 8% 0,#263d7655,transparent 33%),linear-gradient(135deg,#0b1020,#111a2d);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1240px;margin:0 auto;padding:30px 22px 60px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:24px}h1{font-size:29px;letter-spacing:-.02em;margin:0 0 6px}h2{font-size:17px;margin:0 0 14px}.sub,.hint,footer{color:var(--muted)}.sub{margin:0}.hint{font-size:12px;margin:-6px 0 14px}.status{display:flex;align-items:center;gap:8px;color:var(--muted);white-space:nowrap;font-size:12px}.dot{width:8px;height:8px;background:var(--good);border-radius:50%;box-shadow:0 0 12px var(--good)}button{border:1px solid var(--line);color:var(--text);background:var(--panel);border-radius:9px;padding:8px 12px;cursor:pointer}button:hover{background:var(--panel2)}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0}.card,.panel{background:rgba(21,29,51,.94);border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:0 12px 30px #05081255}.metric{font-size:26px;font-weight:700;margin-top:6px}.label{color:var(--muted);font-size:12px}.layout{display:grid;grid-template-columns:1fr 1fr;gap:14px}.wide{grid-column:1/-1}.bar{height:7px;background:#202c48;border-radius:99px;overflow:hidden;margin-top:5px}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#70e1d1);border-radius:inherit}.notice{border-left:3px solid var(--warn);padding:10px 12px;background:#3b301633;margin:8px 0;border-radius:6px}.notice.bad{border-left-color:var(--bad);background:#4a202633}.notice.good{border-left-color:var(--good);background:#1a493b33}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;font-size:12px}.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;font-size:11px;color:var(--muted)}.exact{color:var(--good)}.estimated{color:var(--warn)}.unpriced{color:var(--bad)}footer{margin-top:20px;font-size:12px}@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.layout{grid-template-columns:1fr}.wide{grid-column:auto}}@media(max-width:560px){.grid{grid-template-columns:1fr}header{display:block}.status{margin-top:14px}main{padding:22px 12px}}
</style></head>
<body><main>
<header><div><h1>Codex AI 成本监控</h1><p class="sub">看清每个 Agent 花费，普通任务自动避开旗舰模型。</p></div><div class="status"><span class="dot"></span>本地运行 <button id="refresh">刷新</button></div></header>
<section id="metrics" class="grid"></section>
<section class="layout"><div class="panel"><h2>Agent 成本排行</h2><p class="hint">按成本从高到低排列</p><div id="agents"></div></div><div class="panel"><h2>模型成本排行</h2><p class="hint">旗舰模型占比越高，越值得检查任务路由</p><div id="models"></div></div><div class="panel wide"><h2>策略与预算</h2><div id="alerts"></div></div><div class="panel wide"><h2>最近运行</h2><div id="runs"></div></div></section>
<footer>数据仅保存在本机；精确 token 与 hook 估算会分开标注。刷新时间：<span id="updated">—</span></footer>
</main><script>
const money=(n,u)=>n==null?'—':u==='USD'?('$'+Number(n).toFixed(4)):(Number(n).toFixed(2)+' '+(u||''));
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const table=(rows,cols)=>rows.length?`<table><thead><tr>${cols.map(c=>`<th>${c[1]}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c[2]?c[2](r):esc(r[c[0]])}</td>`).join('')}</tr>`).join('')}</tbody></table>`:'<p class="muted">暂无数据</p>';
const rankTable=(rows,key)=>rows.length?`<table><tbody>${rows.slice(0,8).map((r,i)=>{const max=rows[0].cost||1;return `<tr><td><span class="pill">${i+1}</span> ${esc(r[key])}</td><td>${money(r.cost,r.unit)}<div class="bar"><i style="width:${Math.min(100,Math.max(2,r.cost/max*100))}%"></i></div></td></tr>`}).join('')}</tbody></table>`:'<p class="muted">暂无数据</p>';
async function load(){const s=await fetch('/api/summary').then(r=>r.json());const unit=s.units[0]?.unit||'USD',total=s.units.length===1?s.units[0].cost:null,today=new Date().toISOString().slice(0,10);const todayCost=s.recent_runs.filter(r=>(r.observed_at||'').slice(0,10)===today).reduce((a,r)=>a+(Number(r.cost)||0),0);const agentCount=new Set(s.by_agent.map(r=>r.agent_id)).size;document.querySelector('#metrics').innerHTML=[['总成本',total==null?'多单位':money(total,unit)],['今日成本',money(todayCost,unit)],['Agent 数',agentCount],['旗舰占比',s.flagship_share==null?'—':(s.flagship_share*100).toFixed(1)+'%']].map(x=>`<div class="card"><div class="label">${x[0]}</div><div class="metric">${x[1]}</div></div>`).join('');document.querySelector('#agents').innerHTML=rankTable(s.by_agent,'agent_id');document.querySelector('#models').innerHTML=rankTable(s.by_model,'model');const alerts=[...(s.budget_alerts||[]),...(s.warnings||[]).slice(0,12)];document.querySelector('#alerts').innerHTML=alerts.length?alerts.map(a=>`<div class="notice ${a.policy_decision==='block'?'bad':''}">${esc(a.type||a.policy_decision||'warning')} · ${esc(a.agent_id||a.model||'')} ${a.cost!=null?money(a.cost,a.unit):''} ${a.limit!=null?`· limit ${a.limit}`:''}</div>`).join(''):`<div class="notice good">当前没有预算或策略告警。</div>`;document.querySelector('#runs').innerHTML=table(s.recent_runs,[['observed_at','时间',r=>esc((r.observed_at||'').replace('T',' ').replace('+00:00',''))],['agent_id','Agent'],['model','模型'],['basis','依据',r=>`<span class="${r.basis==='exact'?'exact':r.basis?.startsWith('estimated')?'estimated':'unpriced'}">${esc(r.basis)}</span>`],['cost','成本',r=>money(r.cost,r.unit)],['task_class','任务']]);document.querySelector('#updated').textContent=new Date().toLocaleString();}
document.querySelector('#refresh').addEventListener('click',()=>load().catch(showError));function showError(e){document.querySelector('#alerts').innerHTML=`<div class="notice bad">无法读取数据：${esc(e)}</div>`}load().catch(showError);
</script></body></html>"""


def html_dashboard_tokens() -> str:
    """Token-only browser UI with original user questions."""
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Token 消耗监控</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151d33;--panel2:#1b2742;--muted:#91a0bf;--text:#edf2ff;--accent:#8b9cff;--good:#71e0c2;--warn:#ffca65;--bad:#ff7e8a;--line:#293653}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 8% 0,#263d7655,transparent 33%),linear-gradient(135deg,#0b1020,#111a2d);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1440px;margin:0 auto;padding:30px 22px 60px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:24px}h1{font-size:29px;letter-spacing:-.02em;margin:0 0 6px}h2{font-size:17px;margin:0 0 14px}.sub,.hint,footer{color:var(--muted)}.sub{margin:0}.hint{font-size:12px;margin:-6px 0 14px}.status{display:flex;align-items:center;gap:8px;color:var(--muted);white-space:nowrap;font-size:12px}.dot{width:8px;height:8px;background:var(--good);border-radius:50%;box-shadow:0 0 12px var(--good)}button{border:1px solid var(--line);color:var(--text);background:var(--panel);border-radius:9px;padding:8px 12px;cursor:pointer}button:hover{background:var(--panel2)}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0}.card,.panel{background:rgba(21,29,51,.94);border:1px solid var(--line);border-radius:14px;padding:17px;box-shadow:0 12px 30px #05081255}.metric{font-size:26px;font-weight:700;margin-top:6px}.label{color:var(--muted);font-size:12px}.layout{display:grid;grid-template-columns:1fr 1fr;gap:14px}.wide{grid-column:1/-1}.bar{height:7px;background:#202c48;border-radius:99px;overflow:hidden;margin-top:5px}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#70e1d1);border-radius:inherit}.notice{border-left:3px solid var(--warn);padding:10px 12px;background:#3b301633;margin:8px 0;border-radius:6px}.notice.bad{border-left-color:var(--bad);background:#4a202633}.notice.good{border-left-color:var(--good);background:#1a493b33}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500;font-size:12px}.prompt{white-space:pre-wrap;word-break:break-word;min-width:260px;max-width:480px}.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 7px;font-size:11px;color:var(--muted)}.exact{color:var(--good)}.estimated{color:var(--warn)}.unpriced{color:var(--bad)}footer{margin-top:20px;font-size:12px}@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.layout{grid-template-columns:1fr}.wide{grid-column:auto}}@media(max-width:560px){.grid{grid-template-columns:1fr}header{display:block}.status{margin-top:14px}main{padding:22px 12px}}
</style></head>
<body><main>
<header><div><h1>Codex Token 消耗监控</h1><p class="sub">按万 Token 查看每个 Agent、模型和会话的实际消耗。</p></div><div class="status"><span class="dot"></span>本地运行 <button id="refresh">刷新</button></div></header>
<section id="metrics" class="grid"></section>
<section class="layout">
<div class="panel"><h2>Agent Token 排行</h2><p class="hint">按 Token 消耗从高到低排列</p><div id="agents"></div></div>
<div class="panel"><h2>模型 Token 排行</h2><p class="hint">统计输入与输出 Token 总和</p><div id="models"></div></div>
<div class="panel wide"><h2>策略告警</h2><div id="alerts"></div></div>
<div class="panel wide"><h2>消耗会话</h2><div id="runs"></div></div>
</section>
<footer>问题原文与 Token 数据仅保存在本机；精确 Token 与 Hook 估算会分开标注。刷新时间：<span id="updated">—</span></footer>
</main><script>
function tokenWan(value){return Math.round(Number(value||0)/10000).toLocaleString('zh-CN')+' 万';}
function esc(value){return String(value==null?'':value).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function table(rows,cols){
  if(!rows.length){return '<p class="muted">暂无数据</p>';}
  var out='<table><thead><tr>';
  cols.forEach(function(col){out+='<th>'+col[1]+'</th>';});
  out+='</tr></thead><tbody>';
  rows.forEach(function(row){
    out+='<tr>';
    cols.forEach(function(col){out+='<td>'+(col[2]?col[2](row):esc(row[col[0]]))+'</td>';});
    out+='</tr>';
  });
  return out+'</tbody></table>';
}
function rankTable(rows,key){
  if(!rows.length){return '<p class="muted">暂无数据</p>';}
  var max=Number(rows[0].total_tokens)||1;
  var out='<table><tbody>';
  rows.slice(0,8).forEach(function(row,index){
    var width=Math.min(100,Math.max(2,(Number(row.total_tokens)||0)/max*100));
    out+='<tr><td><span class="pill">'+(index+1)+'</span> '+esc(row[key])+'</td><td>'+tokenWan(row.total_tokens)+'<div class="bar"><i style="width:'+width+'%"></i></div></td></tr>';
  });
  return out+'</tbody></table>';
}
async function load(){
  var summary=await fetch('/api/summary').then(function(response){return response.json();});
  var today=new Date().toISOString().slice(0,10);
  var todayTokens=summary.recent_runs.filter(function(row){return String(row.observed_at||'').slice(0,10)===today;}).reduce(function(total,row){return total+(Number(row.total_tokens)||0);},0);
  var agentCount=new Set(summary.by_agent_tokens.map(function(row){return row.agent_id;})).size;
  var metrics=[['总 Token',tokenWan(summary.total_tokens)],['今日 Token',tokenWan(todayTokens)],['消耗会话',summary.run_count],['Agent 数',agentCount]];
  document.querySelector('#metrics').innerHTML=metrics.map(function(item){return '<div class="card"><div class="label">'+item[0]+'</div><div class="metric">'+item[1]+'</div></div>';}).join('');
  document.querySelector('#agents').innerHTML=rankTable(summary.by_agent_tokens,'agent_id');
  document.querySelector('#models').innerHTML=rankTable(summary.by_model_tokens,'model');
  var alerts=(summary.budget_alerts||[]).concat((summary.warnings||[]).slice(0,12));
  document.querySelector('#alerts').innerHTML=alerts.length?alerts.map(function(alert){return '<div class="notice '+(alert.policy_decision==='block'?'bad':'')+'">'+esc(alert.type||alert.policy_decision||'warning')+' · '+esc(alert.agent_id||alert.model||'')+'</div>';}).join(''):'<div class="notice good">当前没有策略告警。</div>';
  document.querySelector('#runs').innerHTML=table(summary.recent_runs,[
    ['observed_at','时间',function(row){return esc(String(row.observed_at||'').replace('T',' ').replace('+00:00',''));}],
    ['prompt_text','问题原文',function(row){return '<div class="prompt">'+esc(row.prompt_text||'—')+'</div>';}],
    ['agent_id','Agent'],
    ['model','模型'],
    ['basis','依据',function(row){var cls=row.basis==='exact'?'exact':String(row.basis||'').startsWith('estimated')?'estimated':'unpriced';return '<span class="'+cls+'">'+esc(row.basis)+'</span>';}],
    ['total_tokens','Token（万）',function(row){return tokenWan(row.total_tokens);}],
    ['task_class','任务']
  ]);
  document.querySelector('#updated').textContent=new Date().toLocaleString();
}
function showError(error){document.querySelector('#alerts').innerHTML='<div class="notice bad">无法读取数据：'+esc(error)+'</div>';}
document.querySelector('#refresh').addEventListener('click',function(){load().catch(showError);});
load().catch(showError);
</script></body></html>"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    config: dict[str, Any] = {}
    database: pathlib.Path = DEFAULT_DB_PATH
    days: int = 30

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json_dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/summary":
            conn = connect_db(self.database)
            try:
                sync = sync_local_codex(conn, self.config, self.days)
                summary = build_summary(conn, self.config, self.days)
                summary["local_sync"] = sync
                self.send_json(summary)
            finally:
                conn.close()
            return
        if path not in {"/", "/index.html"}:
            self.send_json({"error": "not found"}, 404)
            return
        body = html_dashboard_tokens().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_dashboard(config: dict[str, Any], database: pathlib.Path, host: str, port: int, days: int) -> None:
    DashboardHandler.config = config
    DashboardHandler.database = database
    DashboardHandler.days = days
    server = http.server.ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard: http://{host}:{port}/")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def seed_demo(config: dict[str, Any], conn: sqlite3.Connection) -> int:
    """Insert deterministic sample rows; useful for checking the dashboard."""
    samples = [
        ("agent-docs", "gpt-5.6-luna", {"input_tokens": 12000, "cached_input_tokens": 8000, "output_tokens": 900}, "routine"),
        ("agent-debug", "gpt-5.6-terra", {"input_tokens": 84000, "cached_input_tokens": 70000, "output_tokens": 4200}, "complex"),
        ("agent-architecture", "gpt-5.6-sol", {"input_tokens": 160000, "cached_input_tokens": 125000, "output_tokens": 9000}, "complex"),
    ]
    count = 0
    for index, (agent, model, usage, task_class) in enumerate(samples, 1):
        priced = calculate_cost(model, usage, config)
        upsert_run(
            conn,
            {
                "run_key": f"demo:{index}",
                "observed_at": utc_now(),
                "completed_at": utc_now(),
                "source": "demo",
                "agent_id": agent,
                "session_id": f"demo-session-{index}",
                "thread_id": f"demo-thread-{index}",
                "turn_id": f"demo-turn-{index}",
                "agent_type": "demo",
                "model": model,
                "priced_model": priced.get("priced_model"),
                "status": "completed",
                "basis": "exact",
                "billing_profile": priced.get("billing_profile"),
                "unit": priced.get("unit"),
                "input_tokens": priced.get("input_tokens", 0),
                "cached_input_tokens": priced.get("cached_input_tokens", 0),
                "cache_write_input_tokens": priced.get("cache_write_input_tokens", 0),
                "output_tokens": priced.get("output_tokens", 0),
                "reasoning_output_tokens": priced.get("reasoning_output_tokens", 0),
                "cost": priced.get("cost"),
                "task_class": task_class,
                "policy_decision": "allow",
                "label": "demo",
                "cwd": str(ROOT),
                "prompt_hash": None,
                "source_ref": "samples/demo",
                "metadata_json": json_dumps({"seed": True}),
            },
        )
        count += 1
    conn.commit()
    return count


def cmd_route(args: argparse.Namespace, config: dict[str, Any]) -> int:
    result = assess_task(args.task, args.model, config)
    if args.json:
        print(json_dumps(result))
    else:
        print(f"任务类型: {result['class']}（置信度 {result['confidence']:.2f}）")
        print(f"当前模型: {result.get('active_model') or '未指定'}")
        print(f"建议模型: {result['recommended_model']}")
        print(f"策略决策: {result['decision']}")
        if result.get("estimated_savings"):
            savings = result["estimated_savings"]
            print(f"粗略节省: {savings['savings']:.4f} {savings['unit']}（仅估算）")
        if result["routine_hits"]:
            print(f"普通任务信号: {', '.join(result['routine_hits'])}")
        if result["complex_hits"]:
            print(f"复杂任务信号: {', '.join(result['complex_hits'])}")
    return 0 if result["decision"] != "block" else 2


def cmd_check(summary: dict[str, Any], as_json: bool) -> int:
    failed = bool(summary.get("budget_alerts"))
    result = {"ok": not failed, "budget_alerts": summary.get("budget_alerts", []), "warning_count": summary.get("warning_count", 0)}
    if as_json:
        print(json_dumps(result))
    else:
        print("OK: no hard budget alert" if not failed else f"BLOCK: {len(summary['budget_alerts'])} budget alert(s)")
        if summary.get("warning_count"):
            print(f"Note: {summary['warning_count']} policy/data warning(s)")
    return 0 if not failed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex AI cost and model-governance monitor")
    parser.add_argument("--config", type=pathlib.Path, default=None, help="配置文件路径")
    parser.add_argument("--db", type=pathlib.Path, default=None, help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化 SQLite 数据库")
    sub.add_parser("demo", help="写入演示数据")

    ingest = sub.add_parser("ingest-jsonl", help="导入 codex exec --json 或 app-server JSONL")
    ingest.add_argument("--file", required=True, help="JSONL 文件；使用 - 从 stdin 读取")
    ingest.add_argument("--agent-id", default=None)
    ingest.add_argument("--model", default=None)
    ingest.add_argument("--label", default=None)
    ingest.add_argument("--source", default="codex-exec-jsonl")

    hook = sub.add_parser("ingest-hook", help=argparse.SUPPRESS)
    hook.add_argument("--file", default="-", help=argparse.SUPPRESS)

    route = sub.add_parser("route", help="评估任务并建议模型")
    route.add_argument("--task", required=True)
    route.add_argument("--model", default=None)
    route.add_argument("--json", action="store_true")

    report = sub.add_parser("report", help="输出成本报告")
    report.add_argument("--days", type=int, default=30)
    report.add_argument("--json", action="store_true")

    check = sub.add_parser("check", help="检查预算和策略告警")
    check.add_argument("--days", type=int, default=1)
    check.add_argument("--json", action="store_true")

    sync_local = sub.add_parser("sync-local", help="同步 Codex 本地会话 token 快照")
    sync_local.add_argument("--days", type=int, default=30)
    sync_local.add_argument("--json", action="store_true")

    dashboard = sub.add_parser("dashboard", help="启动本地 dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--days", type=int, default=30)

    doctor = sub.add_parser("doctor", help="检查安装与数据链路")
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    database = db_path(args.db)

    if args.command == "ingest-hook":
        raw = sys.stdin.read() if args.file == "-" else pathlib.Path(args.file).read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return 0
        if not isinstance(payload, dict):
            return 0
        conn = connect_db(database)
        try:
            assessment = process_hook(payload, config, conn)
            output = hook_output(assessment)
            if output:
                print(json_dumps(output))
            elif payload.get("hook_event_name") in {"Stop", "SubagentStop"}:
                # These hook events require JSON on stdout when the process
                # exits successfully; an empty object is intentionally a
                # no-op response.
                print("{}")
        finally:
            conn.close()
        return 0

    if args.command == "route":
        return cmd_route(args, config)

    conn = connect_db(database)
    try:
        if args.command == "init":
            print(f"Initialized {database}")
            return 0
        if args.command == "demo":
            print(f"Inserted {seed_demo(config, conn)} demo run(s) into {database}")
            return 0
        if args.command == "ingest-jsonl":
            result = import_jsonl(args.file, args.agent_id, args.model, args.label, args.source, config, conn)
            print(json_dumps(result))
            return 0
        if args.command == "sync-local":
            result = sync_local_codex(conn, config, args.days)
            if args.json:
                print(json_dumps(result))
            else:
                print(
                    f"Synced {result.get('runs', 0)} local run(s) from "
                    f"{result.get('threads', 0)} thread(s); "
                    f"running: {result.get('running', 0)}"
                )
            return 0
        if args.command in {"report", "check"}:
            sync = sync_local_codex(conn, config, args.days)
            summary = build_summary(conn, config, args.days)
            summary["local_sync"] = sync
            if args.command == "report":
                if args.json:
                    print(json_dumps(summary))
                else:
                    print_report(summary)
                return 0
            return cmd_check(summary, args.json)
        if args.command == "dashboard":
            conn.close()
            run_dashboard(config, database, args.host, args.port, args.days)
            return 0
        if args.command == "doctor":
            checks = {
                "config": DEFAULT_CONFIG_PATH.exists() if args.config is None else pathlib.Path(args.config).exists(),
                "database_parent": database.parent.exists(),
                "hooks_adapter": (ROOT / "hooks" / "cost_monitor_hook.py").exists(),
                "jsonl_wrapper": (ROOT / "bin" / "codex-cost.ps1").exists(),
                "python": sys.version.split()[0],
            }
            if args.json:
                print(json_dumps(checks))
            else:
                for key, value in checks.items():
                    print(f"{key}: {value}")
            return 0 if all(value is not False for value in checks.values()) else 2
    finally:
        conn.close()
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
