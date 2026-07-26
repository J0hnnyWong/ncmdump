#!/usr/bin/env python3
"""NCM Dump GUI - web-based cross-platform frontend for ncmdump."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ── State ─────────────────────────────────────────────────────

_state = {
    "ncm_files": [],          # list of {"name": ..., "dir": ...}
    "converting": False,
    "progress": 0,
    "total": 0,
    "statuses": {},           # filename -> status string
    "output_dir": "",
}
_lock = threading.Lock()

# ── ncmdump binary ────────────────────────────────────────────

def _find_ncmdump() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "build" / "ncmdump",
        repo_root / "build" / "Release" / "ncmdump.exe",
        repo_root / "build" / "ncmdump.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return "ncmdump"

NCMDUMP = _find_ncmdump()

# ── Conversion worker ─────────────────────────────────────────

def _run_conversion(input_dir: str, output_dir: str) -> None:
    global _state
    files = list(Path(input_dir).rglob("*.ncm"))
    with _lock:
        _state["converting"] = True
        _state["total"] = len(files)
        _state["progress"] = 0
        _state["statuses"] = {f.name: "等待转换" for f in files}

    for f in sorted(files):
        filepath = str(f)
        with _lock:
            _state["statuses"][f.name] = "转换中..."

        try:
            result = subprocess.run(
                [NCMDUMP, filepath, "-o", output_dir],
                capture_output=True, text=True, timeout=120,
            )
            status = "完成" if result.returncode == 0 else "失败"
        except subprocess.TimeoutExpired:
            status = "超时"
        except Exception:
            status = "错误"

        with _lock:
            _state["statuses"][f.name] = status
            _state["progress"] += 1

    with _lock:
        _state["converting"] = False

# ── HTTP handler ──────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NCM Dump</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #1a1a2e; color: #e0e0e0; min-height: 100vh; display: flex; flex-direction: column; }
  .toolbar { display: flex; gap: 8px; padding: 16px 20px; background: #16213e; flex-wrap: wrap; align-items: center; }
  .toolbar button { padding: 8px 18px; border: none; border-radius: 6px; cursor: pointer;
                    font-size: 14px; font-weight: 500; transition: opacity 0.15s; }
  .toolbar button:hover { opacity: 0.85; }
  .btn-input { background: #0f3460; color: #e0e0e0; }
  .btn-output { background: #0f3460; color: #e0e0e0; }
  .btn-go { background: #e94560; color: #fff; }
  .btn-clear { background: #533483; color: #e0e0e0; }
  .btn-go:disabled { opacity: 0.4; cursor: not-allowed; }
  .path-row { display: flex; align-items: center; gap: 6px; padding: 0 20px 12px;
              background: #16213e; font-size: 13px; color: #888; }
  .path-row input { flex: 1; padding: 5px 10px; border: 1px solid #333; border-radius: 4px;
                    background: #1a1a2e; color: #ccc; font-size: 13px; }
  .path-row span { white-space: nowrap; min-width: 36px; }
  table { width: 100%; border-collapse: collapse; flex: 1; }
  th, td { padding: 8px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid #222; }
  th { background: #16213e; position: sticky; top: 0; font-weight: 600; color: #aaa; }
  td { color: #ccc; }
  td.status { text-align: center; }
  .status-done { color: #4ecca3; }
  .status-fail { color: #e94560; }
  .status-progress { color: #f0a500; }
  .progress-bar { height: 4px; background: #333; }
  .progress-bar .fill { height: 100%; background: #e94560; transition: width 0.3s; }
  .empty { text-align: center; padding: 60px 20px; color: #555; font-size: 15px; }
  .footer { padding: 6px 20px; font-size: 12px; color: #555; background: #16213e; }
  #progress-text { margin-left: 12px; font-size: 12px; color: #888; }
</style>
</head>
<body>
<div class="toolbar">
  <button class="btn-input" onclick="pickInput()">选择输入目录</button>
  <button class="btn-output" onclick="pickOutput()">选择输出目录</button>
  <button class="btn-go" id="btnGo" onclick="startConvert()" disabled>开始转换</button>
  <button class="btn-clear" onclick="clearList()">清空列表</button>
  <span id="progress-text"></span>
</div>
<div class="path-row">
  <span>输入:</span><input id="inputDir" type="text" placeholder="输入目录路径（包含 .ncm 文件）" readonly>
</div>
<div class="path-row">
  <span>输出:</span><input id="outputDir" type="text" placeholder="输出目录路径" readonly>
</div>
<div class="progress-bar"><div class="fill" id="progressFill" style="width:0%"></div></div>
<div style="flex:1; overflow:auto;">
  <table>
    <thead><tr><th>文件名</th><th>所在目录</th><th style="width:100px">状态</th></tr></thead>
    <tbody id="tbody"><tr><td colspan="3" class="empty">请选择输入目录以扫描 .ncm 文件</td></tr></tbody>
  </table>
</div>
<div class="footer" id="statusBar">就绪</div>
<script>
  const $ = id => document.getElementById(id);
  let pollingTimer = null;

  async function pickInput() {
    const path = prompt("请输入包含 .ncm 文件的目录路径:");
    if (!path) return;
    $('inputDir').value = path;
    await scan(path);
  }

  async function pickOutput() {
    const path = prompt("请输入输出目录路径:");
    if (!path) return;
    $('outputDir').value = path;
    checkReady();
  }

  async function scan(dir) {
    const resp = await fetch('/api/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({input_dir: dir})
    });
    const data = await resp.json();
    renderFiles(data.files);
    $('statusBar').textContent = `扫描完成，找到 ${data.files.length} 个 .ncm 文件`;
    checkReady();
  }

  function renderFiles(files) {
    const tbody = $('tbody');
    if (!files.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty">未找到 .ncm 文件</td></tr>';
      return;
    }
    tbody.innerHTML = files.map(f =>
      `<tr data-name="${f.name}"><td>${esc(f.name)}</td><td>${esc(f.dir)}</td><td class="status">等待转换</td></tr>`
    ).join('');
  }

  function esc(s) {
    const el = document.createElement('span');
    el.textContent = s;
    return el.innerHTML;
  }

  async function startConvert() {
    const inputDir = $('inputDir').value;
    const outputDir = $('outputDir').value;
    if (!inputDir || !outputDir) return;

    // Reset all statuses
    document.querySelectorAll('td.status').forEach(td => td.textContent = '等待转换');
    $('btnGo').disabled = true;
    $('statusBar').textContent = '转换中...';

    const resp = await fetch('/api/convert', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({input_dir: inputDir, output_dir: outputDir})
    });
    if (resp.ok) startPolling();
  }

  function clearList() {
    $('tbody').innerHTML = '<tr><td colspan="3" class="empty">请选择输入目录以扫描 .ncm 文件</td></tr>';
    $('progressFill').style.width = '0%';
    $('progressText').textContent = '';
    $('statusBar').textContent = '就绪';
    $('btnGo').disabled = true;
    stopPolling();
  }

  function checkReady() {
    $('btnGo').disabled = !($('inputDir').value && $('outputDir').value && $('tbody').rows.length > 0);
  }

  function startPolling() {
    stopPolling();
    pollingTimer = setInterval(pollStatus, 500);
  }

  function stopPolling() {
    if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
  }

  async function pollStatus() {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    for (const [name, status] of Object.entries(data.statuses)) {
      const row = document.querySelector(`tr[data-name="${CSS.escape(name)}"]`);
      if (!row) continue;
      const td = row.querySelector('td.status');
      td.textContent = status;
      td.className = 'status';
      if (status === '完成') td.classList.add('status-done');
      else if (status === '失败' || status === '超时' || status === '错误') td.classList.add('status-fail');
      else td.classList.add('status-progress');
    }
    if (data.total > 0) {
      const pct = Math.round(data.progress / data.total * 100);
      $('progressFill').style.width = pct + '%';
      $('progressText').textContent = `${data.progress}/${data.total}`;
    }
    if (!data.converting) {
      stopPolling();
      $('btnGo').disabled = false;
      $('statusBar').textContent = '转换完成';
    }
  }
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet

    def _send(self, code, body, content_type="application/json"):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        if isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif path == "/api/status":
            with _lock:
                self._send(200, {
                    "converting": _state["converting"],
                    "progress": _state["progress"],
                    "total": _state["total"],
                    "statuses": dict(_state["statuses"]),
                })
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/scan":
            input_dir = body.get("input_dir", "")
            if not input_dir or not os.path.isdir(input_dir):
                self._send(400, {"error": "invalid input_dir"})
                return
            files = sorted(Path(input_dir).rglob("*.ncm"))
            result = [{"name": f.name, "dir": str(f.parent)} for f in files]
            with _lock:
                _state["ncm_files"] = result
                _state["statuses"] = {f["name"]: "等待转换" for f in result}
                _state["progress"] = 0
                _state["total"] = len(result)
            self._send(200, {"files": result})

        elif path == "/api/convert":
            input_dir = body.get("input_dir", "")
            output_dir = body.get("output_dir", "")
            if not input_dir or not output_dir:
                self._send(400, {"error": "missing dirs"})
                return
            if _state["converting"]:
                self._send(409, {"error": "already converting"})
                return
            threading.Thread(target=_run_conversion, args=(input_dir, output_dir), daemon=True).start()
            self._send(200, {"ok": True})

        else:
            self._send(404, {"error": "not found"})

# ── Entry point ───────────────────────────────────────────────

def main():
    port = 8765
    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"NCM Dump GUI: {url}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()

if __name__ == "__main__":
    main()
