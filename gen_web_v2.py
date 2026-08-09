# -*- coding: utf-8 -*-
"""Generate web_v2/index.html from web/index.html with two changes:
   1) model distribution: append cache hit-rate to each model's .val
   2) active sessions: replace model/tokens/hitrate/ctx columns with the
      latest user-sent message (s.last_user) "原文简略".
"""
from pathlib import Path

BASE = Path(__file__).resolve().parent
src = (BASE / "web" / "index.html").read_text(encoding="utf-8")
out = src

reps = []


# 1) model distribution .val -> add 命中 hitRate(m)
reps.append((
    '        <div class="val">${fmt(m.total)} · ${money(m.cost,cur)}</div></div>`;',
    '        <div class="val">${fmt(m.total)} · ${money(m.cost,cur)} · 命中 ${hitRate(m)}</div></div>`;',
))

# 2) sessions thead -> 4 columns (端/会话/最新消息/最近)
reps.append((
    '          <thead><tr><th>端</th><th>会话</th><th>模型</th>\n'
    '            <th class="num">Tokens</th><th class="num">命中率</th><th>上下文</th><th>最近</th></tr></thead>',
    '          <thead><tr><th>端</th><th>会话</th><th>最新消息（我发送）</th><th>最近</th></tr></thead>',
))

# 3) sessions tbody initial empty row -> colspan 4
reps.append((
    '          <tbody id="sessions"><tr><td colspan="7" class="empty">暂无数据</td></tr></tbody>',
    '          <tbody id="sessions"><tr><td colspan="4" class="empty">暂无数据</td></tr></tbody>',
))

# 4) drop ctx / shr computation block -> add last_user
reps.append((
    "    let ctx = '<span style=\"color:var(--tx3);font-size:11px\">—</span>';\n"
    "    if (s.ctx_used && s.ctx_size){\n"
    "      const p = Math.min(100, s.ctx_used/s.ctx_size*100);\n"
    "      const col = p>=85?'var(--bad)':p>=65?'var(--warn)':'var(--ok)';\n"
    "      ctx = `<div class=\"ctx\"><div class=\"track\"><div class=\"fill\" style=\"width:${p}%;background:${col}\"></div></div><span>${p.toFixed(0)}%</span></div>`;\n"
    "    }\n"
    "    const name = s.title || s.project || s.session_id.slice(0,8);\n"
    "    const shr = (s.fresh_in + s.cache_read) ? (s.cache_read/(s.fresh_in+s.cache_read)*100) : 0;\n"
    "    const shrCol = shr>=60 ? 'var(--ok)' : shr>=30 ? 'var(--warn)' : 'var(--tx3)';",
    "    const name = s.title || s.project || s.session_id.slice(0,8);\n"
    "    const last = s.last_user || '—';",
))

# 5) sessions row template -> 4 columns ending with last_user
reps.append((
    "    return `<tr>\n"
    "      <td><span class=\"badge ${s.source==='codex'?'c':'w'}\">${s.source==='codex'?'CX':'WB'}</span></td>\n"
    "      <td class=\"trunc\" title=\"${esc(name)}\">${esc(name)}</td>\n"
    "      <td class=\"mono\">${esc(s.model)}</td>\n"
    "      <td class=\"num\">${fmt(s.total)}</td>\n"
    "      <td class=\"num\" style=\"color:${shrCol};font-weight:600\">${shr.toFixed(0)}%</td>\n"
    "      <td>${ctx}</td>\n"
    "      <td class=\"mono\">${hhmmss(s.last_ts)}</td></tr>`;",
    "    return `<tr>\n"
    "      <td><span class=\"badge ${s.source==='codex'?'c':'w'}\">${s.source==='codex'?'CX':'WB'}</span></td>\n"
    "      <td class=\"trunc\" title=\"${esc(name)}\">${esc(name)}</td>\n"
    "      <td class=\"trunc\" style=\"max-width:520px\" title=\"${esc(last)}\">${esc(last)}</td>\n"
    "      <td class=\"mono\">${hhmmss(s.last_ts)}</td></tr>`;",
))

for i, (old, new) in enumerate(reps, 1):
    c = out.count(old)
    assert c == 1, f"replacement {i} matched {c} times (expected 1)"
    out = out.replace(old, new)

(WEB := BASE / "web_v2").mkdir(exist_ok=True)
(WEB / "index.html").write_text(out, encoding="utf-8")
print("OK: web_v2/index.html written,", out.count("\n")+1, "lines")
print("last_user occurrences:", out.count("last_user"))
print("命中 occurrences:", out.count("命中 ${hitRate(m)}"))
