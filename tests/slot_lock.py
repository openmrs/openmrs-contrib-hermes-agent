"""Claims a test slot for tests/run.sh, so concurrent runs get their own Compose project, bridge
names, ports, and images.

Usage: python3 slot_lock.py LOCK_DIR OWNER_PID [SLOT]

Prints the claimed slot (the first free one of 0-9, or SLOT when given) on a line of its own, then
holds the slot's lock until OWNER_PID exits. Exits 1, printing nothing to stdout, when no slot (or
SLOT) is free. The lock is an flock, so the kernel releases it whenever this process ends; a
killed run never leaves a slot claimed.
"""

import fcntl
import os
import sys
import time

SLOTS = range(10)


def owner_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main() -> int:
    lock_dir, owner = sys.argv[1], int(sys.argv[2])
    wanted = [int(sys.argv[3])] if len(sys.argv) > 3 else SLOTS
    os.makedirs(lock_dir, exist_ok=True)
    for slot in wanted:
        lock = open(os.path.join(lock_dir, f"{slot}.lock"), "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            continue
        print(slot, flush=True)
        while owner_alive(owner):
            time.sleep(1)
        return 0
    taken = f"slot {wanted[0]} is" if len(wanted) == 1 else "all test slots are"
    print(f"{taken} in use by other tests/run.sh runs", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
