# -*- coding: utf-8 -*-
"""抓取每个会话「我（用户）发送的最新一条消息」简略原文，供看板「活跃会话」模块展示。"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

_CACHE: dict = {}
_CACHE_TS: float = 0.0
_TTL: float = 30.0

# 注入式系统提示（非用户本人所写），需剔除
_REMINDER = re.compile(r"<system-reminder.*?</system-reminder>", re.S | re.I)
_USERQUERY = re.compile(r"<user_query>(.*?)</user_query>", re.S | re.I)


def _clean(text: str) -> str:
    """剥离系统注入的 system-reminder，只保留用户本人发送的内容。
    优先取 <user_query> 包裹的真实问句；否则去掉提醒块后取剩余文本。"""
    m = _USERQUERY.search(text)
    if m:
        return m.group(1).strip()
    cleaned = _REMINDER.sub("", text)
    return cleaned.strip()


def _extract_text(content):
    if not content:
        return ""
    if isinstance(content, str):
        return _clean(content)
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text") or p.get("input") or p.get("content")
                if isinstance(t, str):
                    parts.append(_clean(t))
                elif isinstance(t, list):
                    for q in t:
                        if isinstance(q, dict) and q.get("text"):
                            parts.append(_clean(q["text"]))
            elif isinstance(p, str):
                parts.append(_clean(p))
        return "\n".join(p for p in parts if p)
    return ""


def scan_last_user_messages() -> dict:
    out: dict = {}
    wb = Path.home() / ".workbuddy" / "projects"
    if not wb.exists():
        return out
    for proj in wb.iterdir():
        if not proj.is_dir():
            continue
        for path in proj.glob("*.jsonl"):
            try:
                with open(path, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 400000))
                    chunk = f.read().decode("utf-8", "ignore")
            except OSError:
                continue
            last_ts = -1
            last_text = None
            sid = path.stem
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("role") != "user":
                    continue
                ts = o.get("timestamp") or 0
                text = _extract_text(o.get("content")).strip()
                if not text:
                    continue
                s = o.get("sessionId") or sid
                if ts >= last_ts:
                    last_ts = ts
                    last_text = text[:160]
            if last_text is not None:
                prev = out.get(s)
                if prev is None or last_ts >= prev[0]:
                    out[s] = (last_ts, last_text)
    return {k: v[1] for k, v in out.items()}


def get_last_user_messages(ttl: float = 30.0) -> dict:
    global _CACHE, _CACHE_TS
    now = time.time()
    if now - _CACHE_TS < ttl and _CACHE:
        return _CACHE
    try:
        scanned = scan_last_user_messages()
    except Exception:
        scanned = None
    if scanned is not None:
        _CACHE = scanned
    _CACHE_TS = now
    return _CACHE
