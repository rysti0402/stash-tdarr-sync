"""End-to-end check of the two-phase sync against a mock Stash.

Mimics Stash's job manager: jobs queued with metadataScan / runPluginTask do not run
until the current plugin process has exited, then run strictly in order.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "stash-plugin", "tdarrSync", "tdarrSync.py")

MEDIA = tempfile.mkdtemp()
OLD = os.path.join(MEDIA, "a.avi").replace("\\", "/")      # gone after the transcode
NEW = os.path.join(MEDIA, "a.mkv").replace("\\", "/")
open(NEW, "wb").write(b"x" * 4096)

state = {"scanned": False, "queue": [], "merge": None, "update": None, "deleted": None,
         "generate": None}

OLD_SCENE = {"id": "100", "title": "Original", "files": [
    {"id": "900", "path": OLD, "basename": "a.avi", "size": 1, "duration": 1200.0,
     "video_codec": "mpeg4", "fingerprints": []}]}
NEW_SCENE = {"id": "200", "title": "a", "files": [
    {"id": "901", "path": NEW, "basename": "a.mkv", "size": 1, "duration": 1200.0,
     "video_codec": "hevc", "fingerprints": []}]}


def handle(q, v):
    if "configuration { plugins }" in q:
        return {"configuration": {"plugins": {"tdarrSync": {"settleTimeout": 3}}}}
    if "findScenes" in q:
        val = v["value"]
        if val in (OLD, "a.avi"):
            return {"findScenes": {"scenes": [OLD_SCENE]}}
        if val in (NEW, "a.mkv") and state["scanned"]:
            return {"findScenes": {"scenes": [NEW_SCENE]}}
        return {"findScenes": {"scenes": []}}
    if "metadataScan" in q:
        state["queue"].append(("scan", v["input"]))
        return {"metadataScan": "scan-job"}
    if "runPluginTask" in q:
        state["queue"].append(("plugin", v))
        return {"runPluginTask": "plugin-job"}
    if "sceneMerge" in q:
        state["merge"] = v["input"]
        return {"sceneMerge": {"id": "100"}}
    if "sceneUpdate" in q:
        state["update"] = v["input"]
        return {"sceneUpdate": {"id": "100"}}
    if "deleteFiles" in q:
        state["deleted"] = v["ids"]
        return {"deleteFiles": True}
    if "metadataGenerate" in q:
        state["generate"] = v["input"]
        return {"metadataGenerate": "gen-job"}
    if "findJob" in q:
        raise AssertionError("plugin must never wait on a job it queued")
    return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            payload = {"data": handle(body["query"], body.get("variables") or {})}
        except AssertionError as exc:
            payload = {"errors": [{"message": str(exc)}]}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def run_plugin(args):
    stdin = json.dumps({
        "server_connection": {"Scheme": "http", "Host": "127.0.0.1", "Port": PORT,
                              "PluginDir": "/plugins/tdarrSync"},
        "args": args,
    })
    p = subprocess.run([sys.executable, PLUGIN], input=stdin, capture_output=True,
                       text=True, timeout=60)
    for line in p.stderr.splitlines():
        print("    " + line.replace("\x01", "[").replace("\x02", "] "))
    return p.returncode


print("--- phase 1: Tdarr's call ---")
rc1 = run_plugin({"mode": "sync", "originalPath": OLD, "newPath": NEW})

print("--- Stash runs the queue in order ---")
order = [kind for kind, _ in state["queue"]]
print("    queued:", order)
rc2 = None
for kind, payload in list(state["queue"]):
    if kind == "scan":
        state["scanned"] = True
    else:
        print("--- phase 2: finish ---")
        rc2 = run_plugin(payload["args"])

checks = [
    ("phase 1 exits without waiting", rc1 == 0),
    ("scan queued before finish", order == ["scan", "plugin"]),
    ("finish uses this plugin's id", state["queue"][1][1]["id"] == "tdarrSync"),
    ("phase 2 succeeds", rc2 == 0),
    ("merged new into original", state["merge"] and state["merge"]["destination"] == "100"
     and state["merge"]["source"] == ["200"]),
    ("primary file repointed", state["update"] == {"id": "100", "primary_file_id": "901"}),
    ("stale row removed", state["deleted"] == ["900"]),
    ("regeneration queued", state["generate"] and state["generate"]["sceneIDs"] == ["100"]),
]
failed = 0
print()
for label, ok in checks:
    failed += 0 if ok else 1
    print("  {} {}".format("PASS" if ok else "FAIL", label))
print("\n{} passed, {} failed".format(len(checks) - failed, failed))
srv.shutdown()
sys.exit(1 if failed else 0)
