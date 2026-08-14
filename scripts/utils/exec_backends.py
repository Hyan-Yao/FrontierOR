"""
Execution backends for running LLM-generated code with resource limits.

Three modes:
  - "bare":        Direct subprocess, no resource limits (default, for debugging)
  - "systemd":     systemd-run with CPU/memory cgroups (lightweight, Linux only)
  - "docker":      Docker container with resource limits (fully isolated, reproducible)
  - "bubblewrap":  Filesystem/env isolation for hosts without user-systemd/Docker

All backends share the same interface:
    (success, output, elapsed) = run(code_path, instance_path, solution_path,
                                     time_limit, log_path, cfg)
"""

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

# Default resource limits
DEFAULT_CPUS = 1          # number of CPU cores
DEFAULT_MEMORY = "16G"    # per-candidate address-space / cgroup cap
DEFAULT_MEMORY_RESERVE = "16G"  # host MemAvailable kept free at admission time
DEFAULT_DOCKER_IMAGE = "frontier-or"
_MEMORY_LOCK_PATH = "/tmp/frontieror_memory_admission.lock"
_MEMORY_STATE_PATH = "/tmp/frontieror_memory_admission.json"


def parse_memory_bytes(value):
    """Parse Docker/systemd-style memory sizes (e.g. ``16G`` or ``512MiB``)."""
    if isinstance(value, bool):
        raise ValueError("memory limit must be a byte count or size string")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("memory limit must be positive")
        return value
    text = str(value).strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?)(?:i?[bB])?", text, re.I)
    if not match:
        raise ValueError(f"invalid memory size: {value!r}")
    number = float(match.group(1))
    unit = match.group(2).upper()
    exponent = "KMGTPE".find(unit) + 1 if unit else 0
    result = int(number * (1024 ** exponent))
    if result <= 0:
        raise ValueError("memory limit must be positive")
    return result


def _mem_available_bytes():
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _process_start_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().split()[21]
    except (OSError, IndexError):
        return None


@contextlib.contextmanager
def _memory_admission(memory_bytes, reserve_bytes):
    """Reserve candidate capacity across concurrent FrontierOR processes.

    This admission guard is intentionally conservative: active reservations are
    subtracted from the current ``MemAvailable`` value even when a candidate has
    not consumed its full limit yet.  It protects unrelated host workloads from
    a burst of paper/candidate workers all passing an independent free-memory
    check at the same time.
    """
    token = uuid.uuid4().hex
    pid = os.getpid()
    start_ticks = _process_start_ticks(pid)
    lock_fd = os.open(_MEMORY_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    admitted = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with open(_MEMORY_STATE_PATH, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError, TypeError):
            state = []

        live = []
        for item in state if isinstance(state, list) else []:
            try:
                item_pid = int(item["pid"])
                item_bytes = int(item["bytes"])
            except (KeyError, TypeError, ValueError):
                continue
            if _process_start_ticks(item_pid) == str(item.get("start_ticks")):
                live.append({**item, "bytes": item_bytes})

        available = _mem_available_bytes()
        reserved = sum(item["bytes"] for item in live)
        required = reserved + memory_bytes + reserve_bytes
        if available is not None and required > available:
            raise RuntimeError(
                "memory admission denied: "
                f"MemAvailable={available // (1024 ** 2)}MiB, "
                f"active_reservations={reserved // (1024 ** 2)}MiB, "
                f"candidate_limit={memory_bytes // (1024 ** 2)}MiB, "
                f"host_reserve={reserve_bytes // (1024 ** 2)}MiB"
            )

        live.append({
            "token": token,
            "pid": pid,
            "start_ticks": start_ticks,
            "bytes": memory_bytes,
            "created": time.time(),
        })
        with open(_MEMORY_STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(live, handle)
        os.chmod(_MEMORY_STATE_PATH, 0o600)
        admitted = True
        fcntl.flock(lock_fd, fcntl.LOCK_UN)

        yield
    finally:
        if admitted:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                with open(_MEMORY_STATE_PATH, encoding="utf-8") as handle:
                    state = json.load(handle)
            except (OSError, ValueError, TypeError):
                state = []
            state = [item for item in state if item.get("token") != token]
            with open(_MEMORY_STATE_PATH, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.chmod(_MEMORY_STATE_PATH, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


@contextlib.contextmanager
def _instance_sandbox(instance_path):
    """Isolate the candidate program from the ground-truth tree.

    The candidate receives ``--instance_path`` and routinely derives
    ``paper_dir = dirname(dirname(instance_path))`` to reach sibling
    directories. Against the real benchmark tree that lets a program read or
    overwrite ``<paper>/gurobi_solution/<inst>.json`` (the Gurobi reference the
    evaluator compares against) or walk up to the repo-root
    ``gurobi_solving_results*.csv`` -- a reference-leak exploit that fakes
    ``gap≈0``.

    We copy ONLY the instance JSON into a throwaway ``/tmp`` tree that mirrors
    the ``<root>/instance/<file>`` layout, so the program's derived
    ``paper_dir`` is the sandbox root -- which has no ``gurobi_solution/`` and
    is not inside the repo, so walking up never finds the results CSV either.
    The instance basename is preserved (programs parse the ``large_instance_N``
    suffix). The trusted evaluator keeps using the real paths for the
    feasibility check and gap computation; only the program's view is sandboxed.

    docker already achieves this via volume mounts, so only the bare/systemd
    backends route through here.

    Yields the sandboxed instance path; the temp tree is removed on exit.
    """
    real = os.path.abspath(instance_path)
    tmp_root = tempfile.mkdtemp(prefix="eob_sbx_")
    try:
        inst_dir = os.path.join(tmp_root, "instance")
        os.makedirs(inst_dir, exist_ok=True)
        sandboxed = os.path.join(inst_dir, os.path.basename(real))
        if os.path.exists(real):
            shutil.copy2(real, sandboxed)
        yield sandboxed, tmp_root
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _build_args(code_path, instance_path, solution_path, time_limit, log_path):
    """Build the common argparse arguments for the generated code."""
    args = [
        "--instance_path", instance_path,
        "--solution_path", solution_path,
        "--time_limit", str(time_limit),
    ]
    if log_path:
        args.extend(["--log_path", log_path])
    return args


def build_bare_cmd(code_path, instance_path, solution_path, time_limit,
                   log_path=None, cfg=None):
    """Build a ``python code.py ...`` command, optionally pinned to N cores
    via ``taskset -c`` (util-linux, no systemd required). No CPU quota or
    memory cap — use systemd / docker backend for those."""
    _ensure_logger(code_path)
    cfg = cfg or {}
    cpus = cfg.get("cpus", DEFAULT_CPUS)
    core_set = _allocate_cores(cpus)
    inner = [sys.executable, code_path] + _build_args(
        code_path, instance_path, solution_path, time_limit, log_path
    )
    if core_set:
        inner = ["taskset", "-c", core_set] + inner
    return inner


def run_bare(code_path, instance_path, solution_path, time_limit,
             log_path=None, cfg=None):
    """Run directly via subprocess. No resource limits.

    Routes through the instance sandbox so the program cannot reach the
    ground-truth ``gurobi_solution/`` or ``gurobi_solving_results*.csv`` via
    path derivation or cwd-relative access (see ``_instance_sandbox``).
    """
    code_path = os.path.abspath(code_path)
    solution_path = os.path.abspath(solution_path)
    log_path = os.path.abspath(log_path) if log_path else log_path
    with _instance_sandbox(instance_path) as (sb_instance, sb_root):
        cmd = build_bare_cmd(code_path, sb_instance, solution_path,
                             time_limit, log_path, cfg)
        return _exec(cmd, time_limit, cwd=sb_root)


_core_counter = 0
_core_lock = threading.Lock()


def _allocate_cores(n):
    """Allocate n cores within the host CPU range. Returns a comma-separated CPU list."""
    if n <= 0:
        raise ValueError("cpus must be positive")
    total = os.cpu_count()
    if not total:
        return None
    global _core_counter
    with _core_lock:
        start = _core_counter % total
        _core_counter += n
    cores = [(start + offset) % total for offset in range(n)]
    return ",".join(str(c) for c in cores)


def build_systemd_cmd(code_path, instance_path, solution_path, time_limit,
                      log_path=None, cfg=None):
    """Build a systemd-run scope + taskset command enforcing 1 pinned core,
    a memory cap, and network isolation.

    Layers (each is independent so a missing delegation still leaves the others):
      - ``systemd-run --scope --user -p MemoryMax=<mem>`` — hard memory cap via
        cgroup ``memory.max`` (memory controller is delegated to user slices by
        default on modern systemd).
      - ``-p IPAddressDeny=any`` — no network (eBPF egress filter, Linux ≥ 4.19).
      - ``-p AllowedCPUs=<core>`` — cpuset pinning (only if cpuset controller is
        delegated to user slice; otherwise silently ignored).
      - ``taskset -c <core>`` — userspace CPU pinning via ``sched_setaffinity``.
        Works without any cgroup delegation; this is the guaranteed pin.
    """
    cfg = cfg or {}
    cpus = cfg.get("cpus", DEFAULT_CPUS)
    memory = cfg.get("memory", DEFAULT_MEMORY)

    _ensure_logger(code_path)
    core_set = _allocate_cores(cpus)
    properties = [
        "systemd-run", "--scope", "--user", "-q",
        "-p", f"CPUQuota={cpus * 100}%",
        "-p", f"MemoryMax={memory}",
        "-p", "IPAddressDeny=any",
    ]
    if core_set:
        properties += ["-p", f"AllowedCPUs={core_set}"]
    inner = [sys.executable, code_path] + _build_args(
        code_path, instance_path, solution_path, time_limit, log_path
    )
    if core_set:
        inner = ["taskset", "-c", core_set] + inner
    return properties + inner


def run_systemd(code_path, instance_path, solution_path, time_limit,
                log_path=None, cfg=None):
    """Run via systemd-run with cgroup resource limits and pinned cores.

    Routes through the instance sandbox (see ``_instance_sandbox``):
    systemd-run --scope enforces cpu/memory/network but NOT filesystem
    isolation, so without this the program could still read/overwrite the
    ground-truth ``gurobi_solution/`` and ``gurobi_solving_results*.csv``.
    """
    code_path = os.path.abspath(code_path)
    solution_path = os.path.abspath(solution_path)
    log_path = os.path.abspath(log_path) if log_path else log_path
    with _instance_sandbox(instance_path) as (sb_instance, sb_root):
        cmd = build_systemd_cmd(code_path, sb_instance, solution_path,
                                time_limit, log_path, cfg)
        return _exec(cmd, time_limit, cwd=sb_root)


def build_docker_cmd(code_path, instance_path, solution_path, time_limit,
                     log_path=None, cfg=None):
    """Build the ``docker run`` command for an isolated single-core run.

    Enforces:
      - ``--cpuset-cpus=<core>`` (pinned single core, round-robin across workers)
      - ``--cpus=<n>`` (hard CPU quota, matches cpuset size)
      - ``--memory=<m>`` (hard RAM cap)
      - ``--network=none`` (no network access)
    Mounts: paper code dir (ro), instance (ro), solution dir (rw), Gurobi license (ro).
    """
    cfg = cfg or {}
    cpus = cfg.get("cpus", DEFAULT_CPUS)
    memory = cfg.get("memory", DEFAULT_MEMORY)
    image = cfg.get("docker_image", DEFAULT_DOCKER_IMAGE)
    gurobi_lic = cfg.get("gurobi_lic", os.environ.get("GRB_LICENSE_FILE", ""))
    core_set = _allocate_cores(cpus)

    _ensure_logger(code_path)
    code_dir = os.path.dirname(os.path.abspath(code_path))

    c_instance = "/workspace/instance.json"
    volumes = [
        "-v", f"{code_dir}:/workspace/codedir:ro",
        "-v", f"{os.path.abspath(instance_path)}:{c_instance}:ro",
    ]
    sol_dir = os.path.dirname(os.path.abspath(solution_path))
    volumes += ["-v", f"{sol_dir}:/workspace/output"]
    c_solution = f"/workspace/output/{os.path.basename(solution_path)}"
    c_log = None
    if log_path:
        log_dir = os.path.dirname(os.path.abspath(log_path))
        if log_dir != sol_dir:
            volumes += ["-v", f"{log_dir}:/workspace/logs"]
            c_log = f"/workspace/logs/{os.path.basename(log_path)}"
        else:
            c_log = f"/workspace/output/{os.path.basename(log_path)}"

    if gurobi_lic and os.path.exists(gurobi_lic):
        volumes += ["-v", f"{gurobi_lic}:/opt/gurobi/gurobi.lic:ro"]

    docker_flags = [
        "docker", "run", "--rm",
        f"--cpus={cpus}",
        f"--memory={memory}",
        "--network=none",
    ]
    if core_set:
        docker_flags += [f"--cpuset-cpus={core_set}"]

    cmd = docker_flags + volumes + [
        "-e", "PYTHONPATH=/workspace/codedir:/opt/bench",
        image,
        "python", f"/workspace/codedir/{os.path.basename(code_path)}",
        "--instance_path", c_instance,
        "--solution_path", c_solution,
        "--time_limit", str(time_limit),
    ]
    if c_log:
        cmd += ["--log_path", c_log]
    return cmd


def run_docker(code_path, instance_path, solution_path, time_limit,
               log_path=None, cfg=None):
    """Run inside a Docker container with resource limits (pinned 1 core by default)."""
    cmd = build_docker_cmd(code_path, instance_path, solution_path,
                           time_limit, log_path, cfg)
    return _exec(cmd, time_limit)


def bubblewrap_network_isolation_available():
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-net", "/bin/true"],
        capture_output=True, text=True, check=False,
    )
    return probe.returncode == 0


def run_bubblewrap(code_path, instance_path, solution_path, time_limit,
                   log_path=None, cfg=None):
    """Run a candidate with filesystem isolation and a fail-closed RAM cap.

    This backend is intended for workstations where user-systemd is unavailable
    and the benchmark Docker image is not installed.  It exposes only the
    project virtualenv, candidate directory, one instance, and its output
    directory.  Network unsharing is best-effort because some kernels disable
    unprivileged network namespaces; the empty environment and hidden ``/home``
    still prevent API credentials and Codex auth files from reaching candidates.

    Memory is enforced with inherited ``RLIMIT_AS`` via ``prlimit``.  Unlike a
    cgroup this is a per-process virtual-address-space limit, but it reliably
    constrains the single-process Python/Gurobi solvers required by this
    benchmark.  A cross-process admission ledger also preserves host headroom
    when multiple paper workers start candidates concurrently.
    """
    if shutil.which("bwrap") is None:
        return False, "bubblewrap executable not found", 0.0
    if shutil.which("prlimit") is None:
        return False, "prlimit executable not found; refusing unbounded bubblewrap run", 0.0

    cfg = cfg or {}
    cpus = cfg.get("cpus", DEFAULT_CPUS)
    try:
        memory_bytes = parse_memory_bytes(cfg.get("memory", DEFAULT_MEMORY))
        reserve_raw = cfg.get("memory_reserve", DEFAULT_MEMORY_RESERVE)
        reserve_bytes = 0 if str(reserve_raw).strip().upper() in {"0", "0B"} else parse_memory_bytes(reserve_raw)
    except ValueError as exc:
        return False, f"invalid bubblewrap memory configuration: {exc}", 0.0
    core_set = _allocate_cores(cpus)
    _ensure_logger(code_path)

    code_path = os.path.abspath(code_path)
    code_dir = os.path.dirname(code_path)
    instance_path = os.path.abspath(instance_path)
    solution_path = os.path.abspath(solution_path)
    output_dir = os.path.dirname(solution_path)
    os.makedirs(output_dir, exist_ok=True)

    can_unshare_network = bubblewrap_network_isolation_available()

    venv_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".venv"))
    base_python = os.path.realpath(os.path.join(venv_dir, "bin", "python"))
    base_python_root = os.path.dirname(os.path.dirname(base_python))
    command = [
        "bwrap", "--die-with-parent", "--new-session",
        "--ro-bind", "/", "/",
        "--dev", "/dev", "--proc", "/proc",
        "--tmpfs", "/tmp", "--tmpfs", "/home",
        "--dir", "/tmp/home",
        "--dir", os.path.dirname(base_python_root),
        "--dir", base_python_root,
        "--ro-bind", base_python_root, base_python_root,
        "--dir", "/tmp/frontieror-venv",
        "--ro-bind", venv_dir, "/tmp/frontieror-venv",
        "--dir", "/tmp/workspace",
        "--ro-bind", code_dir, "/tmp/workspace/code",
        "--ro-bind", instance_path, "/tmp/workspace/instance.json",
        "--bind", output_dir, "/tmp/workspace/output",
        "--clearenv",
        "--setenv", "HOME", "/tmp/home",
        "--setenv", "PATH", "/tmp/frontieror-venv/bin:/usr/bin:/bin",
        "--setenv", "VIRTUAL_ENV", "/tmp/frontieror-venv",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "OMP_NUM_THREADS", "1",
        "--setenv", "OPENBLAS_NUM_THREADS", "1",
        "--setenv", "MKL_NUM_THREADS", "1",
        "--setenv", "NUMEXPR_NUM_THREADS", "1",
        "--chdir", "/tmp/workspace",
    ]
    if can_unshare_network:
        command.insert(3, "--unshare-net")

    license_path = os.environ.get("GRB_LICENSE_FILE")
    if license_path and os.path.isfile(license_path):
        command += [
            "--dir", "/tmp/gurobi",
            "--ro-bind", os.path.abspath(license_path), "/tmp/gurobi/gurobi.lic",
            "--setenv", "GRB_LICENSE_FILE", "/tmp/gurobi/gurobi.lic",
        ]

    inner = [
        "prlimit",
        f"--as={memory_bytes}:{memory_bytes}",
        "--core=0:0",
        "--nofile=1024:1024",
        "--",
        "/tmp/frontieror-venv/bin/python",
        f"/tmp/workspace/code/{os.path.basename(code_path)}",
    ] + _build_args(
        code_path,
        "/tmp/workspace/instance.json",
        f"/tmp/workspace/output/{os.path.basename(solution_path)}",
        time_limit,
        f"/tmp/workspace/output/{os.path.basename(log_path)}" if log_path else None,
    )
    if core_set:
        python_index = inner.index("/tmp/frontieror-venv/bin/python")
        inner[python_index:python_index] = ["taskset", "-c", core_set]
    try:
        with _memory_admission(memory_bytes, reserve_bytes):
            success, output, elapsed = _exec(command + inner, time_limit)
    except RuntimeError as exc:
        return False, str(exc), 0.0
    if not success and any(marker in output.lower() for marker in (
        "memoryerror", "out of memory", "cannot allocate memory", "std::bad_alloc",
    )):
        output = f"Candidate exceeded or could not operate within memory limit {memory_bytes} bytes:\n{output}"
    return success, output, elapsed


def _ensure_logger(code_path):
    """Copy solution_logger.py next to the generated code if not already there."""
    code_dir = os.path.dirname(os.path.abspath(code_path))
    dest = os.path.join(code_dir, "solution_logger.py")
    if not os.path.exists(dest):
        src = os.path.join(os.path.dirname(__file__), "solution_logger.py")
        if os.path.exists(src):
            shutil.copy2(src, dest)


def _exec(cmd, time_limit, cwd=None):
    """Execute a command with timeout. Returns (success, output, elapsed).

    ``cwd`` (when set) runs the subprocess from that working directory; the
    bare/systemd backends point it at the instance sandbox so a program doing
    ``open("gurobi_solving_results.csv")`` or globbing the cwd finds nothing.

    Uses Popen + ``start_new_session=True`` so the spawned process is the
    leader of a new process group (its pgid = its pid). On timeout we call
    ``os.killpg(pgid, SIGKILL)`` to kill **the entire process group** rather
    than just the immediate child.

    Why this matters: ``subprocess.run(timeout=...)`` only sends SIGKILL to
    the direct child. With ``systemd-run --scope``, the actual python
    script is a grandchild that runs inside the scope's cgroup. When
    systemd-run dies, the python grandchild gets reparented to init and
    keeps running — bypassing the timeout entirely.

    Killing the process group guarantees taskset + python all die together.
    The scope cgroup auto-cleans once empty.
    """
    grace_seconds = 30  # buffer over time_limit for cleanup tasks
    deadline = time.time() + time_limit + grace_seconds
    start = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            cwd=cwd,
        )
    except (OSError, ValueError) as e:
        return False, f"Failed to launch subprocess: {e}", 0.0
    try:
        out, err = proc.communicate(timeout=max(1.0, deadline - time.time()))
    except subprocess.TimeoutExpired:
        # Hard kill the whole process group (systemd-run + taskset + python script)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        elapsed = round(time.time() - start, 2)
        return False, f"Execution timed out after {time_limit} seconds", elapsed
    elapsed = round(time.time() - start, 2)
    if proc.returncode != 0:
        error_msg = (err or "").strip() or (out or "").strip()
        return False, f"Process exited with code {proc.returncode}:\n{error_msg}", elapsed
    return True, (out or "").strip(), elapsed


# Registry
BACKENDS = {
    "bare": run_bare,
    "systemd": run_systemd,
    "docker": run_docker,
    "bubblewrap": run_bubblewrap,
}

BUILDERS = {
    "bare": build_bare_cmd,
    "systemd": build_systemd_cmd,
    "docker": build_docker_cmd,
}
