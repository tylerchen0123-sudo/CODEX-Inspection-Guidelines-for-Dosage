# -*- coding: utf-8 -*-
"""
CODEX + WORKBUDDY Token 实时监控 · HTTP 服务
    python server.py [--port 8910] [--interval 3]

  /                 看板页面
  /api/summary      当前聚合快照 (JSON)
  /api/stream       SSE 实时推流，有新调用即刻推送
  /api/raw          最近调用明细 (JSON)
  /api/pause        暂停扫描（进程保留，数据停止刷新）
  /api/resume       恢复扫描
  /api/shutdown     停止监控服务（关闭本进程）
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from collector import Collector

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

collector = Collector()
subscribers: list[queue.Queue] = []
sub_lock = threading.Lock()
latest_payload: dict = {}
payload_lock = threading.Lock()
PAUSED = False


def build_payload() -> dict:
    p = collector.build_summary()
    p = dict(p)
    p["paused"] = PAUSED
    return p


def set_paused(state: bool) -> None:
    global PAUSED, latest_payload
    PAUSED = state
    latest_payload = build_payload()


def refresh_loop(interval: float) -> None:
    """后台扫描：未暂停时扫描并广播；暂停时仅空转。"""
    global latest_payload
    last_push = 0.0
    while True:
        try:
            if not PAUSED:
                added = collector.scan()
                with payload_lock:
                    latest_payload = build_payload()
                now = time.time()
                if added or now - last_push >= 10:
                    last_push = now
                    blob = json.dumps(latest_payload, ensure_ascii=False)
                    with sub_lock:
                        dead = []
                        for q in subscribers:
                            try:
                                q.put_nowait(blob)
                            except queue.Full:
                                dead.append(q)
                        for q in dead:
                            subscribers.remove(q)
        except Exception as exc:  # 守护线程绝不退出
            print(f"[scan error] {exc}")
        time.sleep(interval)


def shutdown_server() -> None:
    time.sleep(0.3)
    os._exit(0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            try:
                body = (WEB_DIR / "index.html").read_bytes()
                self._send(200, body, "text/html; charset=utf-8")
            except OSError:
                self._send(404, b"index.html missing", "text/plain; charset=utf-8")
            return

        if path == "/api/summary":
            with payload_lock:
                data = latest_payload or build_payload()
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/raw":
            with payload_lock:
                data = (latest_payload or {}).get("recent", [])
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if path == "/api/pause":
            set_paused(True)
            self._send(200, b'{"ok":true,"paused":true}', "application/json")
            return

        if path == "/api/resume":
            set_paused(False)
            self._send(200, b'{"ok":true,"paused":false}', "application/json")
            return

        if path == "/api/shutdown":
            self._send(200, b'{"ok":true,"msg":"stopping"}', "application/json")
            threading.Thread(target=shutdown_server, daemon=True).start()
            return

        if path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            q: queue.Queue = queue.Queue(maxsize=8)
            with sub_lock:
                subscribers.append(q)
            try:
                with payload_lock:
                    first = latest_payload or build_payload()
                self._sse(json.dumps(first, ensure_ascii=False))
                while True:
                    try:
                        blob = q.get(timeout=15)
                        self._sse(blob)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with sub_lock:
                    if q in subscribers:
                        subscribers.remove(q)
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/api/pause", "/api/resume", "/api/shutdown"):
            return self.do_GET()
        self._send(405, b"method not allowed", "text/plain; charset=utf-8")

    def _sse(self, blob: str) -> None:
        self.wfile.write(b"data: " + blob.encode("utf-8") + b"\n\n")
        self.wfile.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8910)
    ap.add_argument("--interval", type=float, default=3.0, help="扫描间隔秒")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    print("首次全量扫描中...")
    t0 = time.time()
    collector.scan()
    with payload_lock:
        globals()["latest_payload"] = build_payload()
    stats = latest_payload["stats"]
    print(f"完成：{stats['calls_indexed']:,} 次调用 / {stats['files_tracked']} 个文件 "
          f"/ {time.time() - t0:.1f}s")

    threading.Thread(target=refresh_loop, args=(args.interval,), daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    globals()["SERVER"] = srv
    url = f"http://127.0.0.1:{args.port}/"
    print(f"看板已启动 → {url}  (Ctrl+C 停止)")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
