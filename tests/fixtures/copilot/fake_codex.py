"""Stand-in for ``codex exec --json`` in tests: replays recorded events and logs its argv and stdin.

    python fake_codex.py <events.jsonl> <log.jsonl> [--sleep S] [--exit N] <codex argv...>
"""

import json
import sys
import time

events, logf = sys.argv[1], sys.argv[2]
rest = sys.argv[3:]
sleep, code = 0.0, 0
if rest[:1] == ["--sleep"]:
    sleep, rest = float(rest[1]), rest[2:]
if rest[:1] == ["--exit"]:
    code, rest = int(rest[1]), rest[2:]
prompt = sys.stdin.read()
with open(logf, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": rest, "stdin": prompt}) + "\n")
if sleep:
    time.sleep(sleep)
with open(events, "rb") as f:  # codex writes UTF-8 JSONL
    for line in f:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
if code:
    print("boom: something went wrong", file=sys.stderr)
sys.exit(code)
