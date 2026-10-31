"""Workspace supervisor: runs sshd for one agent and resets the workspace when it goes idle.

Hermes logs in as the unprivileged `workspace` user (its `ssh` terminal backend), so everything the
agent runs, including builds of untrusted pull requests, runs as that user. The supervisor runs as
root, out of that user's reach.

Reads from /config (written by the admin tool, unreadable by the workspace user):

  authorized_keys          the agent's public key, the only one allowed to log in
  ssh_host_ed25519_key     this workspace's host key, pinned by the agent
  litellm_key              the workspace's LiteLLM key, which only lets `gh` get GitHub tokens

A reset, at startup and after WORKSPACE_IDLE_MINUTES (default 180) with no logged-in SSH session,
kills every workspace-user process and deletes what it left in /work, /tmp, and /dev/shm. With
WORKSPACE_KEEP_FILES=true, /work (home, clones, TMPDIR) is kept, including across restarts but not
recreating the container; anything a malicious build planted there stays until removed. /cache
(Maven, npm, Yarn, pnpm, Corepack) is shared by all workspaces and kept so builds don't re-download
dependencies; a malicious build could poison it but finds nothing worth reading there.

Only logins and logged-in sessions count as activity, so a leftover build process can't block a
reset. Hermes
holds its connection open while commands run and for five minutes after.
"""

import os
import pwd
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

USER = "workspace"
UID = pwd.getpwnam(USER).pw_uid
GID = pwd.getpwnam(USER).pw_gid
IDLE_SECONDS = float(os.environ.get("WORKSPACE_IDLE_MINUTES", "180")) * 60
KEEP_FILES = os.environ.get("WORKSPACE_KEEP_FILES", "false").strip().lower() in {"1", "true", "yes"}
POLL_SECONDS = float(os.environ.get("WORKSPACE_POLL_SECONDS", "10"))
CONFIG = Path("/config")
RUN = Path("/run/workspace")
WORK = Path("/work")
HOME = WORK / "home"
CACHE = Path("/cache")
SWEPT = (Path("/tmp"), Path("/dev/shm")) if KEEP_FILES else (WORK, Path("/tmp"), Path("/dev/shm"))
JAVA_HOME = "/opt/java/21"

# Every login session's environment. GIT_* (the bot account commits are authored as) come from
# compose.yaml via the supervisor's environment.
SESSION_ENV = {
    "PATH": f"{JAVA_HOME}/bin:/opt/maven/bin:/usr/local/bin:/usr/bin:/bin",
    "JAVA_HOME": JAVA_HOME,
    "TMPDIR": str(WORK / "tmp"),
    "LANG": "C.UTF-8",
    "CI": "true",
    "GIT_TERMINAL_PROMPT": "0",
    # The Maven repository is shared, so concurrent builds lock artifacts while writing.
    "MAVEN_ARGS": f"-Dmaven.repo.local={CACHE}/m2 -Daether.syncContext.named.factory=file-lock "
                  "-Daether.syncContext.named.nameMapper=file-gav",
    "npm_config_cache": f"{CACHE}/npm",
    "npm_config_store_dir": f"{CACHE}/pnpm-store",
    "YARN_CACHE_FOLDER": f"{CACHE}/yarn",
    "YARN_ENABLE_GLOBAL_CACHE": "true",
    "YARN_GLOBAL_FOLDER": f"{CACHE}/yarn-berry",
    "COREPACK_HOME": f"{CACHE}/corepack",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    **{name: os.environ[name] for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                                            "GIT_COMMITTER_EMAIL") if os.environ.get(name)},
}

# Key logins as the workspace user only; no forwarding or client-supplied environment. ClientAlive
# drops connections from dead clients (e.g. a killed agent container), which would otherwise count as
# activity forever.
SSHD_CONFIG = f"""\
Port 2222
HostKey {RUN}/ssh_host_ed25519_key
AuthorizedKeysFile {RUN}/authorized_keys
PidFile {RUN}/sshd.pid
AllowUsers {USER}
PermitRootLogin no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitUserEnvironment no
AllowTcpForwarding no
AllowStreamLocalForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
PrintMotd no
MaxSessions 64
ClientAliveInterval 30
ClientAliveCountMax 3
"""


def log(message: str) -> None:
    print(f"workspace: {message}", flush=True)


def write(path: Path, content: str, mode: int, gid: int = 0) -> None:
    path.write_text(content)
    os.chown(path, 0, gid)
    os.chmod(path, mode)


def setup_sshd() -> None:
    """Copies the keys out of /config with the ownership and modes sshd requires."""
    RUN.mkdir(mode=0o755, exist_ok=True)
    Path("/run/sshd").mkdir(mode=0o755, exist_ok=True)
    write(RUN / "ssh_host_ed25519_key", (CONFIG / "ssh_host_ed25519_key").read_text(), 0o600)
    write(RUN / "authorized_keys", (CONFIG / "authorized_keys").read_text(), 0o644)
    # Read by the gh wrapper, as the workspace user.
    write(RUN / "litellm_key", (CONFIG / "litellm_key").read_text().strip() + "\n", 0o640, GID)
    set_env = "SetEnv " + " ".join(f'"{name}={value}"' for name, value in SESSION_ENV.items()) + "\n"
    write(RUN / "sshd_config", SSHD_CONFIG + set_env, 0o600)


def logged_in_sessions() -> int:
    """Counts sshd's root-owned monitor processes for the workspace user, which that user can't fake."""
    count = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != 0:
                continue
            title = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if title.startswith(("sshd: ", "sshd-session: ")) and f"{USER} [priv]" in title:
            count += 1
    return count


def user_pids() -> list[int]:
    """Live (non-zombie) processes of the workspace user."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = dict(line.split(":", 1) for line in (entry / "status").read_text().splitlines() if ":" in line)
        except OSError:
            continue
        if status.get("State", "").strip().startswith("Z"):
            continue
        if UID in {int(uid) for uid in status.get("Uid", "").split()}:
            pids.append(int(entry.name))
    return pids


def kill_user_processes() -> None:
    """SIGKILL every workspace-user process, however it was started, until none are left."""
    for _ in range(100):
        pids = user_pids()
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.1)
    raise RuntimeError(f"workspace processes survived SIGKILL: {user_pids()}")


def remove_user_files() -> None:
    """Delete everything the workspace user owns in /tmp and /dev/shm, and everything under /work
    unless KEEP_FILES."""
    for directory in SWEPT:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            try:
                if directory != WORK and entry.lstat().st_uid != UID:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except FileNotFoundError:
                pass


def reset() -> None:
    kill_user_processes()
    remove_user_files()
    for path in (HOME, WORK / "tmp"):
        if not path.is_dir():
            path.mkdir(mode=0o700)
            os.chown(path, UID, GID)


def prepare_cache() -> None:
    for name in ("", "m2", "npm", "pnpm-store", "yarn", "yarn-berry", "corepack"):
        path = CACHE / name
        path.mkdir(exist_ok=True)
        os.chown(path, UID, GID)


_last_login = 0.0
_login_lock = threading.Lock()


def relay_sshd_log(stream) -> None:
    """Copies sshd's log to ours, noting the time of each login. Polling logged_in_sessions() alone
    misses sessions shorter than POLL_SECONDS, and with them anything such a session left behind."""
    global _last_login
    for line in iter(stream.readline, b""):
        sys.stdout.buffer.write(line)
        sys.stdout.flush()
        if f"Accepted publickey for {USER} ".encode() in line:
            with _login_lock:
                _last_login = time.monotonic()


def main() -> int:
    setup_sshd()
    prepare_cache()
    reset()
    sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(RUN / "sshd_config")], stderr=subprocess.PIPE)
    threading.Thread(target=relay_sshd_log, args=(sshd.stderr,), daemon=True).start()
    log(f"ready; resets after {IDLE_SECONDS / 60:g} idle minutes{', keeping files' if KEEP_FILES else ''}")
    last_active, used, seen_login = time.monotonic(), False, 0.0
    while sshd.poll() is None:
        time.sleep(POLL_SECONDS)
        with _login_lock:
            login = _last_login
        if logged_in_sessions() or login > seen_login:
            last_active, used, seen_login = time.monotonic(), True, login
        elif used and time.monotonic() - last_active >= IDLE_SECONDS:
            reset()
            used = False
            log("reset after going idle")
    log(f"sshd exited with {sshd.returncode}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
