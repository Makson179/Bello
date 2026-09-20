"""An offline JSONL peer used to exercise real subprocess/pipe behavior."""
import json
import sys


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    if "method" not in request:
        if request["id"] == "host-1":
            send({"method": "tool_done", "params": request})
        continue
    method = request["method"]
    if method == "close":
        break
    if method == "invalid":
        print("not json", flush=True)
        continue
    if method == "ignore":
        continue
    if method == "tool":
        send({"id": "host-1", "method": "bello/tool", "params": {"name": "test", "arguments": {}}})
    if method == "cancel":
        send({"method": "bello/tool/cancel", "params": {"requestId": "host-1"}})
    send({"id": request["id"], "result": {"method": method}})
