"""Wake gate for the reviewer's review-queue cron job (see README.md): Hermes runs it before each
tick, and skips the model when its last line is {"wakeAgent": false}.

Prints that when no queue entry is free, and otherwise how many are, which wakes the model; the
github-review plugin then binds the session to the oldest free entry. An entry is free when it isn't
reviewed yet (posted.json), has had fewer than MAX_ATTEMPTS sessions, and no live session has it: its
owner's process is gone, or hasn't stamped it for OWNER_STALE_SECONDS. These are the plugin's rules
(_claim_next, _owner_alive); change both together. If either file can't be read, it says why and
skips the model: the plugin logs the same problem whenever it touches the queue.
"""

import json
import sys
import time
from pathlib import Path

QUEUE_FILE = Path("/opt/data/github-review/queue.json")
POSTED_FILE = Path("/opt/data/github-review/posted.json")
MAX_ATTEMPTS = 3
OWNER_STALE_SECONDS = 45 * 60
SKIP = json.dumps({"wakeAgent": False})


def start_time(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def owner_alive(owner):
    if not isinstance(owner, dict) or owner.get("start") is None:
        return False
    try:
        beat = float(owner.get("beat") or 0)
    except (TypeError, ValueError):
        return False
    return time.time() - beat < OWNER_STALE_SECONDS and start_time(owner.get("pid")) == owner["start"]


def read(path, kind, default):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        print(f"review-queue: can't read {path}: {e}", file=sys.stderr)
        print(SKIP)
        sys.exit(0)
    if not isinstance(data, kind):
        print(f"review-queue: {path} is not a {kind.__name__}", file=sys.stderr)
        print(SKIP)
        sys.exit(0)
    return data


queue = read(QUEUE_FILE, dict, {})
posted = set(read(POSTED_FILE, list, []))
free = [key for key, entry in queue.items()
        if isinstance(entry, dict) and f"{key}@{entry.get('sha')}" not in posted
        and not owner_alive(entry.get("owner")) and int(entry.get("attempts") or 0) < MAX_ATTEMPTS]
print(f"{len(free)} pull request(s) wait in the review queue." if free else SKIP)
