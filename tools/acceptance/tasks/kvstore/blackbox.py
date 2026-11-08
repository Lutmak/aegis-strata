#!/usr/bin/env python3
"""Black-box check of kvstore.py: start it, drive every route, restart, compare. Exit 0 when it all holds."""
import json, os, pathlib, socket, subprocess, sys, time, urllib.request, urllib.error
work = pathlib.Path(sys.argv[1]); exe = work / "kvstore.py"; data = work / "_bb.json"
if data.exists(): data.unlink()
def port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def start(p):
    proc = subprocess.Popen([sys.executable, str(exe), "--port", str(p), "--data", str(data)], cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try: urllib.request.urlopen(f"http://127.0.0.1:{p}/health", timeout=1); return proc
        except Exception: time.sleep(0.1)
    proc.kill(); raise SystemExit("server did not come up: " + proc.stderr.read().decode()[-500:])
def req(p, method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{p}{path}", data=body, method=method)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp: return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e: return e.code, e.read(), e.headers
fails = []
def check(cond, what):
    if not cond: fails.append(what)
p = port(); proc = start(p)
try:
    check(req(p, "GET", "/kv/a")[0] == 404, "GET missing -> 404")
    check(req(p, "PUT", "/kv/a", b"one")[0] == 201, "PUT new -> 201")
    check(req(p, "PUT", "/kv/a", b"uno")[0] == 204, "PUT existing -> 204")
    st, body, hdr = req(p, "GET", "/kv/a"); check(st == 200 and body == b"uno", "GET value"); check(hdr.get("Content-Type", "").startswith("application/octet-stream"), "octet-stream content type")
    check(req(p, "PUT", "/kv/sp%20ace", b"x")[0] == 201, "percent-decoded key")
    check(req(p, "PUT", "/kv/big", b"z" * (1024 * 1024 + 1))[0] == 413, "1 MiB + 1 -> 413")
    check(req(p, "PUT", "/kv/b", b"\x00\xff" * 1000)[0] == 201, "binary value")
    st, body, _ = req(p, "GET", "/kv"); check(st == 200 and json.loads(body) == ["a", "b", "sp ace"], f"key list sorted: {body[:80]!r}")
    st, body, _ = req(p, "GET", "/health"); check(st == 200 and json.loads(body) == {"ok": True, "keys": 3}, f"health: {body[:80]!r}")
    check(req(p, "DELETE", "/kv/b")[0] == 204, "DELETE -> 204"); check(req(p, "DELETE", "/kv/b")[0] == 404, "DELETE missing -> 404")
finally:
    proc.terminate(); proc.wait(timeout=5)
p = port(); proc = start(p)
try:
    st, body, _ = req(p, "GET", "/kv/a"); check(st == 200 and body == b"uno", "value survives restart")
    st, body, _ = req(p, "GET", "/kv"); check(json.loads(body) == ["a", "sp ace"], "keys survive restart")
finally:
    proc.terminate(); proc.wait(timeout=5)
print(json.dumps({"pass": not fails, "failures": fails}))
sys.exit(0 if not fails else 1)
