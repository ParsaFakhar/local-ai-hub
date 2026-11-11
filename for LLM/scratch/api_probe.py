import json, sys, threading, time, urllib.request, urllib.error, tempfile, os
sys.path.insert(0, os.path.abspath('local-ai-hub'))
from hub.config import load_config
from hub import api, paths

tmp = tempfile.mkdtemp()
cfg = load_config({"ollama": {"mock": True}, "api": {"enabled": True, "port": 8799, "token_file": "probe-token"}, "cache": {"enabled": True}})
srv = api.make_server(cfg)
t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
token = api.ensure_token(cfg)
base = "http://127.0.0.1:8799"

def call(method, route, body=None, headers=None, tok=token):
    h = {"Authorization": f"Bearer {tok}"} if tok else {}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if data: h["Content-Type"] = "application/json"
    req = urllib.request.Request(base+route, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, raw

print("ping", call("GET","/v1/ping"))
print("noauth", call("GET","/v1/status", tok=None)[0])
print("badauth", call("GET","/v1/status", tok="nope")[0])
print("index", call("GET","/")[0])
print("openapi", call("GET","/v1/openapi.json")[0])
print("task", call("POST","/v1/task", {"plugin":"hello.python","input":{"name":"api"},"id":"probe-1"}))
print("tasks", call("GET","/v1/tasks")[0])
print("task get", call("GET","/v1/tasks/probe-1"))
print("task 404", call("GET","/v1/tasks/nope")[0])
print("domain get", call("GET","/v1/domains/example-board.com")[0])
print("cache clear", call("POST","/v1/cache/clear"))
print("cancel", call("POST","/v1/task/cancel", {"task_id":"*"}))
print("bad route", call("GET","/v1/nope")[0])
print("bad body", call("POST","/v1/task", {"plugin":123})[0])
print("origin blocked", call("GET","/v1/status", headers={"Origin":"https://evil.example"})[0])
# SSE
req = urllib.request.Request(base+f"/v1/events?access_token={token}", headers={"Accept":"text/event-stream"})
with urllib.request.urlopen(req, timeout=10) as r:
    print("sse first line:", r.readline().decode().strip(), r.readline().decode().strip()[:80])
srv.shutdown(); srv.server_close(); srv.hub_engine.shutdown()
