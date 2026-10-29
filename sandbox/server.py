"""sandbox: an MCP server that builds and tests openmrs repositories for agents.

Agents reach it only through LiteLLM, which grants it per key like any other MCP server. It holds
no secrets and mounts no agent data, so code from a pull request can run here without approval:
there is nothing in reach for it to steal. What a build must not do is affect later jobs, so:

- each build runs as an unprivileged user (BUILD_UID) in its own directory under /work;
- job state and output live in this server's memory, out of the build user's reach;
- when a job ends or times out, every process of the build user is killed and everything it left
  under /work, /tmp, and /dev/shm is deleted. The root filesystem is read-only.

The exception is /cache (the Maven, npm, Yarn, pnpm, and Corepack caches), which persists between
jobs so builds don't re-download their dependencies each time. A malicious build could poison it
and so skew later results, but finds nothing there worth reading.

One job runs at a time: `run_build` refuses while another is running. Job state is lost when the
server restarts.
"""

import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# Repositories are cloned from here; tests point it at a local git server.
GIT_BASE = os.environ.get("SANDBOX_GIT_BASE", "https://github.com/openmrs").rstrip("/")
BUILD_UID = int(os.environ.get("BUILD_UID", "20000"))
BUILD_GID = int(os.environ.get("BUILD_GID", "20000"))
WORK = Path(os.environ.get("SANDBOX_WORK", "/work"))
CACHE = Path(os.environ.get("SANDBOX_CACHE", "/cache"))
SWEPT = (WORK, Path("/tmp"), Path("/dev/shm"))
JAVA_VERSIONS = (8, 11, 17, 21)
MAX_TIMEOUT_MINUTES = 60
MAX_OUTPUT = 4 * 1024 * 1024
KEPT_JOBS = 50

REPOSITORY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
PULL_REF = re.compile(r"^pull/(\d+)$")
REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]*$")

# Runs as the build user in the job's directory: fetches just the requested ref, then runs the
# command in the repository root. Before the command runs, it prints the commit that was built.
SCRIPT = """set -eu
git init -q src
cd src
git remote add origin "$SANDBOX_REPO_URL"
git fetch -q --filter=blob:none origin "$SANDBOX_FETCH_REF"
git -c advice.detachedHead=false checkout -q FETCH_HEAD
echo "sandbox: checked out $(git rev-parse HEAD)"
exec bash -c "$SANDBOX_COMMAND"
"""


@dataclass
class Job:
    id: str
    repository: str
    ref: str
    command: str
    java_version: int
    timeout_minutes: int
    started: float = field(default_factory=time.time)
    finished: float | None = None
    state: str = "running"
    exit_code: int | None = None
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def summary(self) -> dict:
        # git may print warnings before the checkout line, but the build's own output comes after it.
        commit = re.search(rb"^sandbox: checked out ([0-9a-f]{40})$", bytes(self.output[:65536]), re.M)
        return {
            "job_id": self.id,
            "state": self.state,
            "repository": self.repository,
            "ref": self.ref,
            "commit": commit.group(1).decode() if commit else None,
            "command": self.command,
            "java_version": self.java_version,
            "exit_code": self.exit_code,
            "seconds": round((self.finished or time.time()) - self.started),
        }


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_running: Job | None = None

mcp = FastMCP("sandbox", host="0.0.0.0", port=8080, stateless_http=True)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok\n")


@mcp.tool()
def run_build(repository: str, ref: str, command: str, java_version: int = 21, timeout_minutes: int = 30) -> dict:
    """Start building or testing a repository in the openmrs GitHub organization, in an isolated
    sandbox. Returns at once with a job id; call job_status to follow it. One job runs at a time.

    The sandbox has JDKs 8, 11, 17, and 21, Maven, Node 22, npm, Yarn, and pnpm, and internet
    access for dependencies. It holds no credentials. Dependency caches are kept between jobs.

    Args:
        repository: Repository name in the openmrs organization, e.g. "openmrs-core".
        ref: Branch, tag, or commit SHA, or "pull/<number>" for a pull request's head.
        command: Shell command run in the repository root, e.g. "mvn -B verify" or
            "yarn install --immutable && yarn verify".
        java_version: JDK to use: 8, 11, 17, or 21.
        timeout_minutes: Wall-clock limit, 1 to 60; the job is killed when it's reached.
    """
    global _running
    if not REPOSITORY.match(repository) or ".." in repository:
        raise ValueError(f"repository must be a repository name in the openmrs organization, got {repository!r}")
    pull = PULL_REF.match(ref)
    if not pull and (not REF.match(ref) or ".." in ref or ref.endswith((".lock", "/"))):
        raise ValueError(f"ref must be a branch, tag, commit SHA, or pull/<number>, got {ref!r}")
    if java_version not in JAVA_VERSIONS:
        raise ValueError(f"java_version must be one of {JAVA_VERSIONS}, got {java_version}")
    if not 1 <= timeout_minutes <= MAX_TIMEOUT_MINUTES:
        raise ValueError(f"timeout_minutes must be between 1 and {MAX_TIMEOUT_MINUTES}, got {timeout_minutes}")
    if not command.strip():
        raise ValueError("command is empty")

    with _lock:
        if _running is not None:
            raise ValueError(f"job {_running.id} is still running; wait for it to finish (job_status)")
        job = Job(uuid.uuid4().hex[:12], repository, ref, command, java_version, timeout_minutes)
        _running = job
        _jobs[job.id] = job
        for old in list(_jobs)[:-KEPT_JOBS]:
            del _jobs[old]
    try:
        process = _start(job, f"pull/{pull.group(1)}/head" if pull else ref)
    except Exception:
        with _lock:
            job.state, job.finished, _running = "error", time.time(), None
        raise
    threading.Thread(target=_supervise, args=(job, process), daemon=True).start()
    return job.summary()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def job_status(job_id: str, tail_lines: int = 200) -> dict:
    """Report a sandbox job's state ("running", "succeeded", "failed", "timed_out", or "error"),
    exit code, the commit it built, and the last `tail_lines` lines of its combined output."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise ValueError(f"no job {job_id!r} (jobs are forgotten when the sandbox restarts)")
        lines = bytes(job.output).decode(errors="replace").splitlines()
        result = job.summary()
    tail_lines = max(1, min(tail_lines, 2000))
    result["output_lines"] = len(lines)
    result["output_truncated"] = job.truncated
    result["output_tail"] = "\n".join(lines[-tail_lines:])
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_jobs() -> list[dict]:
    """List recent sandbox jobs, newest first, without their output."""
    with _lock:
        return [job.summary() for job in reversed(_jobs.values())]


def _start(job: Job, fetch_ref: str) -> subprocess.Popen:
    home = WORK / job.id
    for path in (home, home / "home", home / "tmp"):
        path.mkdir()
        os.chown(path, BUILD_UID, BUILD_GID)
    java_home = f"/opt/java/{job.java_version}"
    env = {
        "PATH": f"{java_home}/bin:/opt/maven/bin:/usr/local/bin:/usr/bin:/bin",
        "JAVA_HOME": java_home,
        "HOME": str(home / "home"),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8",
        "CI": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "MAVEN_ARGS": f"-Dmaven.repo.local={CACHE}/m2",
        "npm_config_cache": f"{CACHE}/npm",
        "npm_config_store_dir": f"{CACHE}/pnpm-store",
        "YARN_CACHE_FOLDER": f"{CACHE}/yarn",
        "YARN_ENABLE_GLOBAL_CACHE": "true",
        "YARN_GLOBAL_FOLDER": f"{CACHE}/yarn-berry",
        "COREPACK_HOME": f"{CACHE}/corepack",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "SANDBOX_REPO_URL": f"{GIT_BASE}/{job.repository}",
        "SANDBOX_FETCH_REF": fetch_ref,
        "SANDBOX_COMMAND": job.command,
    }
    return subprocess.Popen(
        ["bash", "-c", SCRIPT], cwd=home, env=env, user=BUILD_UID, group=BUILD_GID, extra_groups=[],
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def _supervise(job: Job, process: subprocess.Popen) -> None:
    global _running
    reader = threading.Thread(target=_collect, args=(job, process.stdout), daemon=True)
    reader.start()
    exit_code, state = None, "error"
    try:
        exit_code = process.wait(timeout=job.timeout_minutes * 60)
        state = "succeeded" if exit_code == 0 else "failed"
    except subprocess.TimeoutExpired:
        state = "timed_out"
    finally:
        try:
            # Background processes the build started still hold the output pipe open; killing
            # them closes it.
            _kill_build_processes()
            reader.join(timeout=10)
            _remove_build_files()
        finally:
            with _lock:
                job.exit_code, job.state, job.finished = exit_code, state, time.time()
                _running = None


def _collect(job: Job, stream) -> None:
    while chunk := stream.read1(65536):
        with _lock:
            job.output += chunk
            if len(job.output) > MAX_OUTPUT:
                del job.output[:len(job.output) - MAX_OUTPUT]
                job.truncated = True


def _build_pids() -> list[int]:
    """Live processes of the build user. Zombies are already dead, so they don't count."""
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
        if BUILD_UID in {int(uid) for uid in status.get("Uid", "").split()}:
            pids.append(int(entry.name))
    return pids


def _reap() -> None:
    """Reap orphaned build processes when this server is the container's PID 1 (no init)."""
    if os.getpid() != 1:
        return
    try:
        while os.waitpid(-1, os.WNOHANG)[0] > 0:
            pass
    except ChildProcessError:
        pass


def _kill_build_processes() -> None:
    """SIGKILL every process running as the build user, however it was started, until none are left."""
    for _ in range(100):
        _reap()
        pids = _build_pids()
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.1)
    raise RuntimeError(f"build processes survived SIGKILL: {_build_pids()}")


def _remove_build_files() -> None:
    """Delete the job directories under /work and everything the build user owns in /tmp and /dev/shm."""
    for directory in SWEPT:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            try:
                if directory != WORK and entry.lstat().st_uid != BUILD_UID:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except FileNotFoundError:
                pass


def _prepare() -> None:
    """Start from a clean /work, with /cache writable by the build user."""
    _kill_build_processes()
    _remove_build_files()
    for name in ("", "m2", "npm", "pnpm-store", "yarn", "yarn-berry", "corepack"):
        path = CACHE / name
        path.mkdir(exist_ok=True)
        os.chown(path, BUILD_UID, BUILD_GID)


if __name__ == "__main__":
    _prepare()
    mcp.run(transport="streamable-http")
