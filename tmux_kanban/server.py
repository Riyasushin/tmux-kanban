import asyncio
import fcntl
import hashlib
import json
import os
import pty
import re
import secrets
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

app = FastAPI()


def _ensure_terminal_env():
    """Systemd user services may start without TERM, which breaks tmux attach."""
    term = os.environ.get("TERM", "").strip().lower()
    if not term or term == "dumb":
        os.environ["TERM"] = "xterm-256color"
    os.environ.setdefault("COLORTERM", "truecolor")


_ensure_terminal_env()

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_custom_config = os.environ.get("TMUX_KANBAN_CONFIG")
if _custom_config:
    CONFIG_PATH = os.path.realpath(_custom_config)
    KANBAN_DIR = os.path.dirname(CONFIG_PATH)
else:
    KANBAN_DIR = os.path.expanduser("~/.tmux-kanban")
    CONFIG_PATH = os.path.join(KANBAN_DIR, "config.json")
os.makedirs(KANBAN_DIR, exist_ok=True)
os.chmod(KANBAN_DIR, 0o700)
# Create empty config file if it doesn't exist (needed for first-time setup command)
if not os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "w") as f:
        json.dump({}, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)

# ── Auth tokens (persisted in config + cached in memory) ──
_valid_tokens: set[str] = set()


def _load_tokens():
    """Load persisted tokens from config into memory cache."""
    global _valid_tokens
    try:
        config = load_config()
        _valid_tokens = set(config.get("auth_tokens", []))
    except Exception:
        _valid_tokens = set()


def _save_token_nolock(token: str):
    """Add token to memory + config. Caller must hold _config_lock if async."""
    _valid_tokens.add(token)
    config = load_config()
    tokens = list(config.get("auth_tokens", []))
    if token not in tokens:
        tokens.append(token)
    # Keep only the 10 most recent tokens (oldest first)
    config["auth_tokens"] = tokens[-10:]
    # Sync memory cache to match persisted tokens exactly
    _valid_tokens.clear()
    _valid_tokens.update(config["auth_tokens"])
    save_config(config)


# ── Agent token (for hooks from inside tmux sessions) ──
_agent_token: str = ""


def _init_agent_token():
    """Ensure agent-token exists in config and ~/.tmux-kanban/agent-token file."""
    global _agent_token
    config = load_config()
    if "agent_token" not in config:
        config["agent_token"] = secrets.token_urlsafe(32)
        save_config(config)
    _agent_token = config["agent_token"]
    token_path = os.path.join(KANBAN_DIR, "agent-token")
    with open(token_path, "w") as f:
        f.write(_agent_token + "\n")
    os.chmod(token_path, 0o600)


def _write_helper_script():
    """Deploy alert-agent-needs-you.sh helper to ~/.tmux-kanban/."""
    script_path = os.path.join(KANBAN_DIR, "alert-agent-needs-you.sh")
    notify_path = os.path.join(KANBAN_DIR, "notify-kanban.sh")
    script = r"""#!/bin/bash
# Notify tmux-kanban that this session's agent needs your attention.
MESSAGE="${1:-Needs your attention}"
SESSION="${TMUX_KANBAN_SESSION:-}"
if [ -z "$SESSION" ]; then
    SESSION="$(tmux display-message -p '#S' 2>/dev/null || true)"
fi
[ -z "$SESSION" ] && exit 0

MANAGED="${TMUX_KANBAN_MANAGED:-}"
if [ "$MANAGED" != "1" ]; then
    MANAGED_LINE="$(tmux show-environment -t "$SESSION" TMUX_KANBAN_MANAGED 2>/dev/null || true)"
    [ "$MANAGED_LINE" = "TMUX_KANBAN_MANAGED=1" ] && MANAGED="1"
fi
[ "$MANAGED" != "1" ] && exit 0

BASE_URL="${TMUX_KANBAN_URL:-}"
if [ -z "$BASE_URL" ]; then
    URL_LINE="$(tmux show-environment -t "$SESSION" TMUX_KANBAN_URL 2>/dev/null || true)"
    case "$URL_LINE" in
        TMUX_KANBAN_URL=*) BASE_URL="${URL_LINE#TMUX_KANBAN_URL=}" ;;
    esac
fi
HOST="${TMUX_KANBAN_HOST:-127.0.0.1}"
PORT="${TMUX_KANBAN_PORT:-59235}"
BASE_URL="${BASE_URL:-http://${HOST}:${PORT}}"
TOKEN=$(cat "$HOME/.tmux-kanban/agent-token" 2>/dev/null)
[ -z "$TOKEN" ] && exit 1
BODY=$(MESSAGE="$MESSAGE" python3 -c 'import json, os; print(json.dumps({"message": os.environ.get("MESSAGE", "")}))')
curl -s -X PUT "${BASE_URL}/api/sessions/${SESSION}/attention" \
    -H "X-Auth-Token: ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${BODY}" -o /dev/null
exit 0
"""
    notify_script = r"""#!/bin/bash
# Ask tmux-kanban dashboards to refresh.
EVENT_TYPE="${1:-refresh}"
MESSAGE="${2:-}"
SESSION="${TMUX_KANBAN_SESSION:-}"
if [ -z "$SESSION" ]; then
    SESSION="$(tmux display-message -p '#S' 2>/dev/null || true)"
fi
[ -z "$SESSION" ] && exit 0

MANAGED="${TMUX_KANBAN_MANAGED:-}"
if [ "$MANAGED" != "1" ]; then
    MANAGED_LINE="$(tmux show-environment -t "$SESSION" TMUX_KANBAN_MANAGED 2>/dev/null || true)"
    [ "$MANAGED_LINE" = "TMUX_KANBAN_MANAGED=1" ] && MANAGED="1"
fi
[ "$MANAGED" != "1" ] && exit 0

BASE_URL="${TMUX_KANBAN_URL:-}"
if [ -z "$BASE_URL" ]; then
    URL_LINE="$(tmux show-environment -t "$SESSION" TMUX_KANBAN_URL 2>/dev/null || true)"
    case "$URL_LINE" in
        TMUX_KANBAN_URL=*) BASE_URL="${URL_LINE#TMUX_KANBAN_URL=}" ;;
    esac
fi
HOST="${TMUX_KANBAN_HOST:-127.0.0.1}"
PORT="${TMUX_KANBAN_PORT:-59235}"
BASE_URL="${BASE_URL:-http://${HOST}:${PORT}}"
TOKEN=$(cat "$HOME/.tmux-kanban/agent-token" 2>/dev/null)
[ -z "$TOKEN" ] && exit 1
BODY=$(EVENT_TYPE="$EVENT_TYPE" SESSION="$SESSION" MESSAGE="$MESSAGE" python3 -c 'import json, os; print(json.dumps({"type": os.environ.get("EVENT_TYPE", "refresh"), "session": os.environ.get("SESSION"), "message": os.environ.get("MESSAGE", "")}))')
curl -s -X POST "${BASE_URL}/api/events/notify" \
    -H "X-Auth-Token: ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${BODY}" -o /dev/null
exit 0
"""
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)
    with open(notify_path, "w") as f:
        f.write(notify_script)
    os.chmod(notify_path, 0o755)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Always public: static files, index page, auth status, auth login
        if path == "/" or path.startswith("/static") or path in ("/api/auth/status", "/api/auth/login"):
            return await call_next(request)
        # Agent hook endpoints: use agent_token (not web auth)
        if request.method == "PUT" and re.match(r"^/api/sessions/[^/]+/attention$", path):
            token = request.headers.get("X-Auth-Token", "")
            if token and token == _agent_token:
                return await call_next(request)
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        if request.method == "POST" and path == "/api/events/notify":
            token = request.headers.get("X-Auth-Token", "")
            if token and token == _agent_token:
                return await call_next(request)
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        # Check auth token
        token = request.headers.get("X-Auth-Token", "")
        if not token:
            # Fallback: support Authorization: Bearer for non-proxy setups
            auth = request.headers.get("Authorization", "")
            token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
        if not token or token not in _valid_tokens:
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)


app.add_middleware(AuthMiddleware)


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles with no-cache headers — frontend assets evolve frequently."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


_event_clients: set[WebSocket] = set()
_event_seq = 0


async def _send_event_to_clients(message: str):
    dead = []
    for client in list(_event_clients):
        try:
            await client.send_text(message)
        except Exception:
            dead.append(client)
    for client in dead:
        _event_clients.discard(client)


def notify_clients(event_type: str = "refresh", payload: dict | None = None):
    """Best-effort event push to open dashboards."""
    global _event_seq
    _event_seq += 1
    message = json.dumps({"type": event_type, "seq": _event_seq, "payload": payload or {}})
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_send_event_to_clients(message))


def validate_name(name: str, label: str = "name"):
    if not name or not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$', name) or len(name) > 64:
        raise HTTPException(status_code=400, detail=f"Invalid {label}: must be 1-64 alphanumeric/dash/underscore characters")


_HOME = os.path.realpath(os.path.expanduser("~"))  # resolve symlinks (e.g. /home/x -> /gpfs/.../x)


def _path_in_root(path: str, root: str) -> bool:
    path_real = os.path.realpath(path)
    root_real = os.path.realpath(root)
    return path_real == root_real or path_real.startswith(root_real + os.sep)


def _allowed_local_roots() -> list[str]:
    roots = [_HOME, KANBAN_DIR]
    env_roots = os.environ.get("TMUX_KANBAN_ALLOWED_ROOTS", "")
    for raw in env_roots.split(os.pathsep):
        if raw.strip():
            roots.append(os.path.abspath(os.path.expanduser(raw.strip())))
    wt_root = os.environ.get("TMUX_KANBAN_WORKTREE_PATH")
    if wt_root:
        roots.append(os.path.abspath(os.path.expanduser(wt_root)))
    return list(dict.fromkeys(roots))


def safe_path(path: str) -> str:
    """Expand path and keep local filesystem operations inside trusted roots."""
    if not path or path == "~":
        return _HOME
    expanded = os.path.expanduser(path)
    absolute = os.path.abspath(expanded)
    if not any(_path_in_root(absolute, root) for root in _allowed_local_roots()):
        roots = ", ".join(to_home_display(r) for r in _allowed_local_roots())
        raise HTTPException(status_code=400, detail=f"Path is outside allowed roots: {roots}")
    return absolute


def to_home_display(p: str) -> str:
    """Normalize a path for display: return absolute form. Preserves symlink paths as-is."""
    if not p:
        return p
    return os.path.abspath(os.path.expanduser(p))


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


def get_wt_base(config) -> str:
    """Get validated worktree base path from config. Falls back to default if invalid."""
    wt = os.path.expanduser(config.get("settings", {}).get("worktreePath", "~/.tmux-kanban/worktrees"))
    wt = os.path.abspath(wt)
    if not any(_path_in_root(wt, root) for root in _allowed_local_roots()):
        wt = os.path.join(KANBAN_DIR, "worktrees")
    return wt


def validate_ssh_host(ssh_host: str):
    if not ssh_host:
        return
    if ssh_host.startswith("-") or not re.match(r"^[A-Za-z0-9_.@:-]+$", ssh_host):
        raise HTTPException(status_code=400, detail="Invalid SSH host")


def remote_tmux_command(ssh_host: str, cwd: str, session_name: str) -> str:
    validate_ssh_host(ssh_host)
    remote_cmd = f"cd {shlex.quote(cwd or '~')} && tmux new-session -A -s {shlex.quote(session_name)}"
    return f"ssh -t {shlex.quote(ssh_host)} {shlex.quote(remote_cmd)}"


_git_check_cache: dict[str, tuple[float, bool, str | None]] = {}
GIT_CHECK_TTL_SECONDS = 15


# ── Tmux Control Mode (persistent connection) ──

class TmuxControl:
    """Persistent tmux control mode connection. All commands share one process."""

    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def start(self):
        """Attach control mode to an existing session. Returns False if no sessions exist."""
        # Find any existing session to attach to
        result = await asyncio.create_subprocess_exec(
            "tmux", "list-sessions", "-F", "#{session_name}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await result.communicate()
        sessions = [s for s in stdout.decode().strip().splitlines() if s]
        if not sessions:
            return False  # no sessions yet, caller should retry later
        self._proc = await asyncio.create_subprocess_exec(
            "tmux", "-C", "attach-session", "-t", sessions[0],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._drain_initial()
        return True

    async def _drain_initial(self):
        """Read and discard startup messages until quiet."""
        try:
            while True:
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=1.0)
                if not line:
                    break
                text = line.decode().strip()
                if text.startswith("%exit"):
                    break
        except asyncio.TimeoutError:
            pass  # no more output, ready

    async def _ensure_alive(self) -> bool:
        """Restart if process died. Returns False if no sessions to attach to."""
        if self._proc is None or self._proc.returncode is not None:
            return await self.start()
        return True

    async def command(self, cmd: str) -> str:
        """Send one command, return output string. Serialized via lock."""
        async with self._lock:
            if not await self._ensure_alive():
                return ""  # no sessions, fall back silently
            try:
                self._proc.stdin.write((cmd + "\n").encode())
                await self._proc.stdin.drain()
                return await self._read_response()
            except Exception:
                # Connection broken, will reconnect next call
                self._proc = None
                return ""

    async def _read_response(self) -> str:
        """Read until %end or %error, skip async notifications."""
        lines = []
        in_block = False
        while True:
            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=60.0)
            except asyncio.TimeoutError:
                self._proc = None  # assume connection broken
                return "\n".join(lines)
            if not raw:
                return "\n".join(lines)  # EOF (process died)
            text = raw.decode().rstrip("\n")
            if text.startswith("%begin"):
                in_block = True
                continue
            if text.startswith("%end"):
                return "\n".join(lines)
            if text.startswith("%error"):
                return ""
            if text.startswith("%exit"):
                return ""
            if in_block:
                lines.append(text)
            # else: async notification like %session-changed, skip

    async def batch(self, cmds: list[str]) -> list[str]:
        """Send multiple commands, return list of outputs in order."""
        async with self._lock:
            if not await self._ensure_alive():
                return [""] * len(cmds)
            try:
                for cmd in cmds:
                    self._proc.stdin.write((cmd + "\n").encode())
                await self._proc.stdin.drain()
                results = []
                for _ in cmds:
                    results.append(await self._read_response())
                return results
            except Exception:
                self._proc = None
                return [""] * len(cmds)

    async def stop(self):
        """Shutdown the control mode client."""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except Exception:
                pass


_tmux_ctrl = TmuxControl()


class TmuxError(RuntimeError):
    """Raised when tmux returns a real error rather than "no server running"."""


@app.exception_handler(TmuxError)
async def tmux_error_handler(request: Request, exc: TmuxError):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def _format_tmux_error(action: str, result: subprocess.CompletedProcess) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    if not detail:
        detail = f"exit code {result.returncode}"
    hint = ""
    lower = detail.lower()
    if "server exited unexpectedly" in lower:
        hint = " Check the tmux server/socket state; `tmux -vv list-sessions` usually shows the underlying cause."
    elif "operation not permitted" in lower or "permission denied" in lower:
        hint = " Check the tmux socket permissions and the user running tmux-kanban."
    return f"tmux {action} failed: {detail}{hint}"


def _run_tmux(args: list[str], *, action: str, timeout: int = 5, allow_no_server: bool = False) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise TmuxError(f"tmux {action} timed out after {timeout}s") from e
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().lower()
        if allow_no_server and "no server running" in detail:
            return result
        raise TmuxError(_format_tmux_error(action, result))
    return result


def run_tmux(*args):
    """Sync tmux command (for cold path / non-async contexts)."""
    result = _run_tmux(list(args), action="command")
    return result.stdout.strip()


async def async_run_tmux(*args):
    """Async tmux command via control mode (hot path)."""
    cmd = " ".join(f'"{a}"' if " " in a or "#" in a else a for a in args)
    return await _tmux_ctrl.command(cmd)


# ── Activity detection (terminal output based) ──
import hashlib
import time

_pane_active: dict[str, bool] = {}
_pane_last_output: dict[str, float] = {}   # last time screen changed
_pane_work_start: dict[str, float] = {}    # when working started (for duration)
_pane_last_idle: dict[str, float] = {}
_pane_screen_hash: dict[str, str] = {}

IDLE_GRACE_SECONDS = 5


def detect_activity_from_captures(session_name: str, pane_captures: dict[str, str]) -> bool:
    """Check if session is active from pre-fetched pane captures.
    pane_captures: {pane_id: captured_text}
    """
    if not pane_captures:
        return False

    now = time.time()
    was_active = _pane_active.get(session_name)
    is_active = False
    for pane_id, content in pane_captures.items():
        h = hashlib.md5(content.encode()).hexdigest()
        prev = _pane_screen_hash.get(pane_id)
        _pane_screen_hash[pane_id] = h
        if prev is not None and h != prev:
            is_active = True

    if is_active:
        _pane_last_output[session_name] = now
        if not _pane_active.get(session_name):
            # Transition idle → working: record start time
            _pane_work_start[session_name] = now
        _pane_active[session_name] = True
        _pane_last_idle.pop(session_name, None)
        if was_active is False:
            notify_clients("activity_changed", {"session": session_name, "active": True})
        return True
    else:
        last_output = _pane_last_output.get(session_name, 0)
        if now - last_output < IDLE_GRACE_SECONDS:
            _pane_active[session_name] = True
            return True
        else:
            if session_name not in _pane_last_idle:
                _pane_last_idle[session_name] = now
            _pane_active[session_name] = False
            _pane_work_start.pop(session_name, None)
            if was_active is True:
                notify_clients("activity_changed", {"session": session_name, "active": False})
            return False


def is_session_active(session_name: str) -> bool:
    return _pane_active.get(session_name, False)


# ── Background activity poller ──
_bg_pane_cache: dict[str, list] = {}  # session_name -> list of pane dicts


async def _activity_poll_loop():
    """Background task: poll activity using control mode batch commands."""
    while True:
        try:
            # Step 1: list all panes (one command)
            raw = await _tmux_ctrl.command(
                'list-panes -a -F "#{session_name}|#{pane_id}|#{pane_current_command}"'
            )
            session_panes: dict[str, list] = {}
            if raw:
                for line in raw.splitlines():
                    parts = line.split("|", 2)
                    if len(parts) >= 2:
                        pane = {"id": parts[1], "command": parts[2] if len(parts) > 2 else ""}
                        session_panes.setdefault(parts[0], []).append(pane)
            _bg_pane_cache.clear()
            registered = set(load_config().get("sessionInfo", {}).keys()) - {"_kanban_ctrl"}
            session_panes = {name: panes for name, panes in session_panes.items() if name in registered}
            all_pane_ids = [pane["id"] for panes in session_panes.values() for pane in panes]
            for stale in set(_pane_active.keys()) - registered:
                _pane_active.pop(stale, None)
                _pane_last_output.pop(stale, None)
                _pane_work_start.pop(stale, None)
                _pane_last_idle.pop(stale, None)
            _bg_pane_cache.update(session_panes)

            # Step 2: batch capture all panes (N commands, one batch call)
            if all_pane_ids:
                capture_cmds = [f"capture-pane -t {pid} -p -S -20" for pid in all_pane_ids]
                captures = await _tmux_ctrl.batch(capture_cmds)
                pane_content = dict(zip(all_pane_ids, captures))
            else:
                pane_content = {}

            # Step 3: detect activity per session from captured content
            for sess_name, panes in session_panes.items():
                sess_captures = {p["id"]: pane_content.get(p["id"], "") for p in panes}
                detect_activity_from_captures(sess_name, sess_captures)
        except Exception:
            pass
        await asyncio.sleep(5)


@app.on_event("startup")
async def start_background_tasks():
    global _setup_secret
    # Control mode connects lazily on first command (needs existing sessions)
    _init_agent_token()
    _write_helper_script()
    _mark_registered_tmux_sessions()
    asyncio.create_task(_activity_poll_loop())
    signal.signal(signal.SIGCHLD, lambda *_: _reap_children())
    # Password setup is now done client-side (no server-generated secrets)


@app.on_event("shutdown")
async def shutdown_tasks():
    await _tmux_ctrl.stop()


_pty_pids: set[int] = set()


def _reap_children():
    """Non-blocking reap of tracked PTY children only.

    Using os.waitpid(-1, ...) would steal exit codes from subprocess.run()
    (e.g. `git rev-parse`), making them look like rc=0 even when they failed.
    So we only reap PIDs we explicitly registered in _pty_pids.
    """
    for pid in list(_pty_pids):
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                _pty_pids.discard(pid)
        except ChildProcessError:
            _pty_pids.discard(pid)


def get_activity_info(session_name: str) -> dict:
    """Return cached activity state for display."""
    active = _pane_active.get(session_name, False)
    if active:
        return {"active": True, "label": "working"}
    return {"active": False, "label": "idle"}


def get_live_sessions():
    """Get set of currently running tmux session names."""
    result = _run_tmux(
        ["list-sessions", "-F", "#{session_name}"],
        action="list sessions",
        allow_no_server=True,
    )
    if result.returncode != 0:
        return set()
    raw = result.stdout.strip()
    if not raw:
        return set()
    return set(raw.splitlines())


def _set_kanban_session_env(name: str):
    """Mark a tmux session as managed by this tmux-kanban instance."""
    base_url = os.environ.get("TMUX_KANBAN_URL", "http://127.0.0.1:59235")
    env = {
        "TMUX_KANBAN_MANAGED": "1",
        "TMUX_KANBAN_SESSION": name,
        "TMUX_KANBAN_URL": base_url,
        "TMUX_KANBAN_HOST": os.environ.get("TMUX_KANBAN_HOST", "127.0.0.1"),
        "TMUX_KANBAN_PORT": os.environ.get("TMUX_KANBAN_PORT", "59235"),
    }
    for key, value in env.items():
        run_tmux("set-environment", "-t", name, key, value)


def _mark_registered_tmux_sessions():
    """Refresh tmux session environment for already-registered live sessions."""
    config = load_config()
    registered = set(config.get("sessionInfo", {}).keys()) - {"_kanban_ctrl"}
    live = get_live_sessions()
    for name in registered & live:
        _set_kanban_session_env(name)


def ensure_tmux_session(name, cwd="~"):
    """Create tmux session if it doesn't exist. Returns True if created, raises on failure."""
    live = get_live_sessions()
    if name in live:
        _set_kanban_session_env(name)
        return False
    expanded = os.path.expanduser(cwd)
    if not os.path.isdir(expanded):
        expanded = os.path.expanduser("~")
    _run_tmux(
        ["new-session", "-d", "-s", name, "-c", expanded],
        action="new-session",
    )
    _set_kanban_session_env(name)
    return True


def check_git(cwd):
    """Check if cwd is a git repo (not just inside one). Returns (is_git, branch)."""
    expanded = os.path.expanduser(cwd)
    try:
        # --show-toplevel returns the repo root; check if cwd IS that root
        r = subprocess.run(
            ["git", "-C", expanded, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        # Treat empty stdout as not-a-repo even if rc==0 (defensive: SIGCHLD
        # handler could race and steal the true exit code).
        if r.returncode != 0 or not r.stdout.strip():
            return False, None
        toplevel = os.path.realpath(r.stdout.strip())
        if os.path.realpath(expanded) != toplevel:
            return False, None  # cwd is inside a repo but not the root
        r2 = subprocess.run(
            ["git", "-C", expanded, "branch", "--show-current"],
            capture_output=True, text=True, timeout=3,
        )
        branch = r2.stdout.strip() or "HEAD"
        return True, branch
    except Exception:
        return False, None


def check_git_cached(cwd):
    """Cached git check for hot dashboard polling."""
    expanded = os.path.abspath(os.path.expanduser(cwd or "~"))
    now = time.time()
    cached = _git_check_cache.get(expanded)
    if cached and now - cached[0] < GIT_CHECK_TTL_SECONDS:
        return cached[1], cached[2]
    is_git, branch = check_git(expanded)
    _git_check_cache[expanded] = (now, is_git, branch)
    if len(_git_check_cache) > 256:
        oldest = sorted(_git_check_cache.items(), key=lambda item: item[1][0])[:64]
        for key, _ in oldest:
            _git_check_cache.pop(key, None)
    return is_git, branch


def get_default_branch(cwd):
    """Get main or master branch name."""
    expanded = os.path.expanduser(cwd)
    for name in ["main", "master"]:
        r = subprocess.run(
            ["git", "-C", expanded, "rev-parse", "--verify", name],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            return name
    return "main"


# ── Config (projects, session status & info) ──

_config_cache: dict | None = None  # in-memory cache


def _read_config_from_disk() -> dict:
    """Read config from disk. Only called on startup and cache miss."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            import logging
            logging.warning(f"Config file corrupted, using defaults: {e}")
            data = {}
    else:
        data = {}
    data.setdefault("projects", [])
    data.setdefault("sessionStatus", {})
    data.setdefault("sessionInfo", {})
    _default_wt = os.environ.get("TMUX_KANBAN_WORKTREE_PATH") or os.path.expanduser("~/.tmux-kanban/worktrees")
    data.setdefault("settings", {
        "configPath": CONFIG_PATH,
        "worktreePath": _default_wt,
    })
    # CLI --worktree-path overrides config
    if os.environ.get("TMUX_KANBAN_WORKTREE_PATH"):
        data["settings"]["worktreePath"] = os.environ["TMUX_KANBAN_WORKTREE_PATH"]
    return data


def load_config() -> dict:
    """Return cached config (no disk read after first load)."""
    global _config_cache
    if _config_cache is None:
        _config_cache = _read_config_from_disk()
    return _config_cache


_config_lock = asyncio.Lock()

# Load persisted tokens on startup
_load_tokens()


def save_config(config):
    """Write config to disk and update memory cache."""
    global _config_cache
    fd, tmp = tempfile.mkstemp(dir=KANBAN_DIR, suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(config, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
        _config_cache = config  # update cache
        notify_clients("config_changed")
    except Exception:
        os.unlink(tmp)
        raise


def _ensure_session(name: str, cwd: str = "~"):
    """Create local tmux session if it doesn't exist."""
    ensure_tmux_session(name, cwd)


def _kill_remote_tmux(name: str, info: dict):
    """Kill remote tmux session with same name via SSH, if sshHost is configured."""
    ssh_host = info.get("sshHost", "")
    if ssh_host:
        validate_ssh_host(ssh_host)
        subprocess.run(
            ["ssh", ssh_host, "tmux", "kill-session", "-t", name],
            capture_output=True, text=True, timeout=5,
        )


# ── Auth endpoints ──

@app.get("/api/auth/status")
async def auth_status():
    """Check if password is set. If not, return setup command and generated password."""
    global _config_cache
    config = load_config()
    pw_plain = config.get("password_plain", "")
    has_password = bool(config.get("password_hash")) and len(pw_plain) >= 10
    if not has_password:
        # Reload from disk — password may have been set via terminal command (bypasses cache)
        _config_cache = _read_config_from_disk()
        config = _config_cache
        pw_plain = config.get("password_plain", "")
        has_password = bool(config.get("password_hash")) and len(pw_plain) >= 10
        if has_password:
            _load_tokens()  # also reload tokens into memory
    return {"hasPassword": has_password}


class AuthSetup(BaseModel):
    password: str

@app.post("/api/auth/setup")
async def auth_setup(body: AuthSetup):
    """Change password. Requires auth (first-time setup done via terminal command)."""
    global _valid_tokens
    if not body.password or len(body.password) < 10:
        raise HTTPException(status_code=400, detail="Password must be at least 10 characters")
    async with _config_lock:
        config = load_config()
        pw_hash = hashlib.sha256(body.password.encode()).hexdigest()
        config["password_hash"] = pw_hash
        config["password_plain"] = body.password  # stored so user can recover from config.json
        # Clear ALL old tokens — invalidate every previous session
        _valid_tokens.clear()
        token = secrets.token_urlsafe(32)
        _valid_tokens.add(token)
        config["auth_tokens"] = [token]
        save_config(config)
    if os.path.exists(CONFIG_PATH):
        os.chmod(CONFIG_PATH, 0o600)
    return {"ok": True, "token": token}


class AuthLogin(BaseModel):
    password: str

@app.post("/api/auth/login")
async def auth_login(body: AuthLogin):
    """Validate password and return session token."""
    config = load_config()
    stored_hash = config.get("password_hash", "")
    if not stored_hash:
        raise HTTPException(status_code=400, detail="No password set. Use /api/auth/setup first.")
    pw_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if pw_hash != stored_hash:
        raise HTTPException(status_code=401, detail="Wrong password")
    token = secrets.token_urlsafe(32)
    async with _config_lock:
        _save_token_nolock(token)
    return {"ok": True, "token": token}


_SENSITIVE_KEYS = {"password_hash", "password_plain", "auth_tokens", "agent_token"}

@app.get("/api/config")
async def get_config():
    config = load_config()
    out = {k: v for k, v in config.items() if k not in _SENSITIVE_KEYS}
    out["home"] = _HOME
    return out


@app.put("/api/settings")
async def put_settings(body: dict):
    """Update only the settings field of config (theme, etc)."""
    allowed = {"theme", "title"}
    updates = {k: v for k, v in body.items() if k in allowed}
    async with _config_lock:
        config = load_config()
        config["settings"] = {**config.get("settings", {}), **updates}
        save_config(config)
    return {"ok": True}


class StatusUpdate(BaseModel):
    status: str

@app.put("/api/session/{name}/status")
async def set_session_status(name: str, body: StatusUpdate):
    validate_name(name, "session name")
    if body.status not in {"todo", "running", "review", "finish"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    async with _config_lock:
        config = load_config()
        if name not in config["sessionInfo"]:
            raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
        config["sessionStatus"][name] = body.status
        save_config(config)
    return {"ok": True}


class SessionInfoUpdate(BaseModel):
    description: str | None = None
    sortIndex: int | None = None
    sshHost: str | None = None
    sshCwd: str | None = None

@app.put("/api/session/{name}/info")
async def update_session_info(name: str, body: SessionInfoUpdate):
    """Update session metadata (description, sortIndex, sshHost)."""
    validate_name(name, "session name")
    if body.sshHost is not None and body.sshHost:
        validate_ssh_host(body.sshHost)
    async with _config_lock:
        config = load_config()
        if name not in config["sessionInfo"]:
            raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
        info = config["sessionInfo"][name]
        if body.description is not None:
            info["description"] = body.description
        if body.sortIndex is not None:
            info["sortIndex"] = body.sortIndex
        if body.sshHost is not None:
            info["sshHost"] = body.sshHost
        if body.sshCwd is not None:
            info["sshCwd"] = body.sshCwd
        save_config(config)
    return {"ok": True}


@app.put("/api/sessions/sort")
async def update_sort_order(body: dict):
    """Batch update sortIndex for multiple sessions. Body: {"order": {"sessName": index, ...}}"""
    order = body.get("order", {})
    if not order:
        return {"ok": True}
    async with _config_lock:
        config = load_config()
        for name, idx in order.items():
            validate_name(name, "session name")
            if not isinstance(idx, int):
                raise HTTPException(status_code=400, detail="sortIndex must be an integer")
            if name in config["sessionInfo"]:
                config["sessionInfo"][name]["sortIndex"] = idx
        save_config(config)
    return {"ok": True}


class AttentionRequest(BaseModel):
    message: str = ""


class NotifyRequest(BaseModel):
    type: str = "refresh"
    session: str | None = None
    message: str = ""


@app.post("/api/events/notify")
async def event_notify(body: NotifyRequest):
    """Agent hook endpoint: ask dashboards to refresh without changing config."""
    notify_clients(body.type or "refresh", {
        "session": body.session,
        "message": body.message,
    })
    return {"ok": True}


@app.put("/api/sessions/{name}/attention")
async def request_attention(name: str, body: AttentionRequest):
    """Agent hook calls this to flag 'needs human attention' on a session.
    Uses agent_token (validated in middleware)."""
    from datetime import datetime, timezone
    async with _config_lock:
        config = load_config()
        if name not in config.get("sessionInfo", {}):
            raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
        config["sessionInfo"][name]["needsAttention"] = True
        config["sessionInfo"][name]["attentionMessage"] = body.message
        config["sessionInfo"][name]["attentionAt"] = datetime.now(timezone.utc).isoformat()
        save_config(config)
    return {"ok": True}


@app.delete("/api/sessions/{name}/attention")
async def clear_attention(name: str):
    """User clears the attention flag from Web UI. Requires auth."""
    async with _config_lock:
        config = load_config()
        if name not in config.get("sessionInfo", {}):
            raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
        info = config["sessionInfo"][name]
        info.pop("needsAttention", None)
        info.pop("attentionMessage", None)
        info.pop("attentionAt", None)
        save_config(config)
    return {"ok": True}


@app.post("/api/sessions/{name}/open-terminal")
async def open_native_terminal(name: str):
    """Open a native terminal window attached to this tmux session."""
    validate_name(name, "session name")
    config = load_config()
    info = config.get("sessionInfo", {}).get(name, {})
    if not info:
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    live = await asyncio.to_thread(get_live_sessions)
    if name not in live:
        raise HTTPException(status_code=400, detail="Session is not running")
    ssh_host = info.get("sshHost", "")
    ssh_cwd = info.get("sshCwd", "")
    if ssh_host:
        validate_ssh_host(ssh_host)
        if ssh_cwd:
            cmd = f"cd {shlex.quote(ssh_cwd)} && tmux attach -t {shlex.quote(name)}"
        else:
            cmd = f"tmux attach -t {shlex.quote(name)}"
        script = f"#!/bin/bash\nexec ssh -t {shlex.quote(ssh_host)} {shlex.quote(cmd)}\n"
    else:
        script = f"#!/bin/bash\nexec tmux attach -t {shlex.quote(name)}\n"
    platsys = sys.platform
    if platsys == "darwin":
        script_path = f"/tmp/tmux-kanban-attach-{name}.command"
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        subprocess.run(["open", script_path], timeout=3)
    elif platsys.startswith("linux"):
        for term in ["gnome-terminal", "xterm", "konsole", "alacritty", "kitty"]:
            r = subprocess.run(["which", term], capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                if term == "gnome-terminal":
                    subprocess.Popen([term, "--", "tmux", "attach", "-t", name])
                elif term == "kitty":
                    subprocess.Popen([term, "tmux", "attach", "-t", name])
                else:
                    subprocess.Popen([term, "-e", f"tmux attach -t {name}"])
                break
        else:
            raise HTTPException(status_code=500, detail="No terminal emulator found")
    else:
        raise HTTPException(status_code=500, detail=f"Unsupported platform: {platsys}")
    return {"ok": True}


class ProjectCreate(BaseModel):
    name: str
    sessions: list[str] = []
    color: int = 0

@app.post("/api/projects")
async def create_project(body: ProjectCreate):
    validate_name(body.name, "project name")
    async with _config_lock:
        config = load_config()
        for p in config["projects"]:
            if p["name"] == body.name:
                raise HTTPException(status_code=400, detail="project already exists")
        config["projects"].append({"name": body.name, "sessions": body.sessions, "color": body.color})
        save_config(config)
    return {"ok": True}


class ProjectUpdate(BaseModel):
    name: str | None = None
    sessions: list[str] | None = None
    color: int | None = None

@app.put("/api/projects/{name}")
async def update_project(name: str, body: ProjectUpdate):
    if body.name is not None:
        validate_name(body.name, "project name")
    async with _config_lock:
        config = load_config()
        if body.name is not None and body.name != name and any(p["name"] == body.name for p in config["projects"]):
            raise HTTPException(status_code=400, detail=f"Project '{body.name}' already exists")
        for p in config["projects"]:
            if p["name"] == name:
                if body.name is not None:
                    p["name"] = body.name
                if body.sessions is not None:
                    p["sessions"] = body.sessions
                if body.color is not None:
                    p["color"] = body.color
                save_config(config)
                return {"ok": True}
    raise HTTPException(status_code=404, detail="project not found")


@app.delete("/api/projects/{name}")
async def delete_project(name: str):
    async with _config_lock:
        config = load_config()
        config["projects"] = [p for p in config["projects"] if p["name"] != name]
        save_config(config)
    return {"ok": True}


@app.put("/api/projects/reorder")
async def reorder_projects(body: dict):
    """Reorder projects by name list."""
    order = body.get("order", [])
    if not order:
        return {"ok": True}
    async with _config_lock:
        config = load_config()
        by_name = {p["name"]: p for p in config["projects"]}
        new_list = [by_name[n] for n in order if n in by_name]
        # Append any projects not in the order list (shouldn't happen normally)
        for p in config["projects"]:
            if p["name"] not in order:
                new_list.append(p)
        config["projects"] = new_list
        save_config(config)
    return {"ok": True}


@app.post("/api/projects/{name}/assign-session")
async def assign_session_to_project(name: str, body: dict):
    """Add a session to a project. Removes from other projects first (one session = one project)."""
    session = body.get("session", "")
    validate_name(session, "session name")
    async with _config_lock:
        config = load_config()
        if session not in config["sessionInfo"]:
            raise HTTPException(status_code=404, detail=f"Session '{session}' not found")
        # Remove session from all other projects first
        for p in config["projects"]:
            if p["name"] != name:
                p["sessions"] = [s for s in p.get("sessions", []) if s != session]
        # Add to target project
        for p in config["projects"]:
            if p["name"] == name:
                if session not in p["sessions"]:
                    p["sessions"].append(session)
                save_config(config)
                return {"ok": True}
    raise HTTPException(status_code=404, detail="project not found")


@app.delete("/api/projects/{name}/remove-session/{session}")
async def remove_session_from_project(name: str, session: str):
    """Remove a session from a project."""
    validate_name(session, "session name")
    async with _config_lock:
        config = load_config()
        for p in config["projects"]:
            if p["name"] == name:
                p["sessions"] = [s for s in p["sessions"] if s != session]
                save_config(config)
                return {"ok": True}
    raise HTTPException(status_code=404, detail="project not found")


@app.get("/api/path-complete")
async def path_complete(q: str = ""):
    expanded = os.path.expanduser(q)
    if os.path.isdir(expanded) and expanded.endswith("/"):
        parent = expanded
        prefix = ""
    else:
        parent = os.path.dirname(expanded) or "."
        prefix = os.path.basename(expanded).lower()
    try:
        safe_path(parent)
    except HTTPException:
        return {"completions": []}
    if not os.path.isdir(parent):
        return {"completions": []}
    try:
        entries = sorted(os.listdir(parent))
    except PermissionError:
        return {"completions": []}
    results = []
    for name in entries:
        if name.startswith(".") and not prefix.startswith("."):
            continue
        if prefix and not name.lower().startswith(prefix):
            continue
        full = os.path.join(parent, name)
        if os.path.isdir(full):
            display = to_home_display(full)
            results.append(display + "/")
        if len(results) >= 12:
            break
    return {"completions": results}


@app.get("/api/browse")
async def browse_dir(path: str = ""):
    expanded = safe_path(path)
    if not os.path.isdir(expanded):
        return {"path": path, "parent": None, "dirs": []}
    display_path = to_home_display(expanded)
    parent = os.path.dirname(expanded)
    if parent == expanded:
        display_parent = None  # at filesystem root
    else:
        display_parent = to_home_display(parent)
    try:
        entries = sorted(os.listdir(expanded))
    except PermissionError:
        return {"path": display_path, "parent": display_parent, "dirs": []}
    dirs = []
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(expanded, name)
        if os.path.isdir(full):
            dirs.append(name)
    return {"path": display_path, "parent": display_parent, "dirs": dirs}


_remote_home_cache: dict[str, str] = {}

def _remote_home(ssh_host: str) -> str:
    """Get remote $HOME via SSH, cached per host."""
    if ssh_host in _remote_home_cache:
        return _remote_home_cache[ssh_host]
    r = subprocess.run(
        ["ssh", ssh_host, "echo", "$HOME"],
        capture_output=True, text=True, timeout=5,
    )
    home = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "/home"
    _remote_home_cache[ssh_host] = home
    return home


def _run_subprocess(args, **kwargs):
    return subprocess.run(args, **kwargs)


@app.get("/api/remote-browse")
async def remote_browse_dir(ssh_host: str = "", path: str = ""):
    """List remote directories via SSH."""
    if not ssh_host:
        raise HTTPException(status_code=400, detail="ssh_host is required")
    validate_ssh_host(ssh_host)
    remote_path = path or "~"
    if remote_path.startswith("~"):
        home = await asyncio.to_thread(_remote_home, ssh_host)
        remote_path = home + remote_path[1:]
    r = await asyncio.to_thread(
        _run_subprocess,
        ["ssh", ssh_host, "ls", "-1pF", "--", remote_path],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return {"path": remote_path, "parent": None, "dirs": []}
    dirs = []
    for line in r.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("/") or stripped.endswith("@"):
            name = stripped.rstrip("/").rstrip("@")
            if name:
                dirs.append(name)
    parent = "/" if remote_path == "/" else (remote_path.rsplit("/", 1)[0] or "/")
    return {"path": remote_path, "parent": parent, "dirs": dirs}


@app.get("/api/remote-path-complete")
async def remote_path_complete(ssh_host: str = "", q: str = ""):
    """Autocomplete remote path via SSH."""
    if not ssh_host:
        return {"completions": []}
    validate_ssh_host(ssh_host)
    remote_q = q or "~"
    if remote_q.startswith("~"):
        home = await asyncio.to_thread(_remote_home, ssh_host)
        remote_q = home + remote_q[1:]
    remote_glob = f"{shlex.quote(remote_q)}*"
    remote_cmd = f'for p in {remote_glob}; do [ -d "$p" ] && printf "%s/\\n" "$p"; done | head -12'
    r = await asyncio.to_thread(
        _run_subprocess,
        ["ssh", ssh_host, "bash", "-c", remote_cmd],
        capture_output=True, text=True, timeout=10,
    )
    return {"completions": [line.strip() for line in r.stdout.splitlines() if line.strip()]}


@app.get("/api/ssh-hosts")
async def list_ssh_hosts():
    """Parse ~/.ssh/config and return available hosts."""
    ssh_config = os.path.expanduser("~/.ssh/config")
    hosts = []
    if os.path.isfile(ssh_config):
        try:
            with open(ssh_config) as f:
                current = {"host": "", "hostname": "", "user": ""}
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    key = parts[0].lower()
                    if key == "host":
                        if current["host"]:
                            hosts.append({
                                "host": current["host"],
                                "hostname": current["hostname"] or current["host"],
                                "user": current["user"] or os.environ.get("USER", ""),
                            })
                        current = {"host": parts[1], "hostname": "", "user": ""}
                        for extra in parts[2:]:
                            if extra and extra != parts[1]:
                                pass  # ignore multi-host patterns for simplicity
                    elif key == "hostname" and current["host"]:
                        current["hostname"] = parts[1]
                    elif key == "user" and current["host"]:
                        current["user"] = parts[1]
                if current["host"]:
                    hosts.append({
                        "host": current["host"],
                        "hostname": current["hostname"] or current["host"],
                        "user": current["user"] or os.environ.get("USER", ""),
                    })
        except Exception:
            pass
    return {"hosts": hosts}


@app.get("/api/check-git")
async def check_git_path(path: str = "~"):
    """Check if a path is a git repo, return repo name and default branch."""
    expanded = safe_path(path.rstrip("/"))
    is_git, branch = check_git(expanded)
    if not is_git:
        return {"isGit": False}
    repo_name = os.path.basename(
        subprocess.run(
            ["git", "-C", expanded, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    )
    default_branch = get_default_branch(expanded)
    return {"isGit": True, "repoName": repo_name, "branch": branch, "defaultBranch": default_branch}


class SessionCreate(BaseModel):
    name: str
    cwd: str = "~"
    project: str | None = None
    command: str | None = None
    description: str | None = None
    sshHost: str | None = None
    sshCwd: str | None = None

@app.post("/api/sessions")
async def create_session(body: SessionCreate):
    validate_name(body.name, "session name")
    config = load_config()
    if body.name in config["sessionInfo"]:
        raise HTTPException(status_code=400, detail=f"Session '{body.name}' already exists")
    if body.sshHost:
        validate_ssh_host(body.sshHost)
        cwd = _HOME
    else:
        cwd = safe_path(body.cwd)
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
    try:
        _ensure_session(body.name, cwd)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    # If SSH host is set, send SSH command into the local tmux session
    if body.sshHost:
        run_tmux("send-keys", "-t", body.name, remote_tmux_command(body.sshHost, body.sshCwd or "~", body.name), "Enter")
    if body.command:
        run_tmux("send-keys", "-t", body.name, body.command, "Enter")
    async with _config_lock:
        config = load_config()
        info = {"cwd": to_home_display(cwd)}
        if body.description:
            info["description"] = body.description
        if body.sshHost:
            info["sshHost"] = body.sshHost
        if body.sshCwd:
            info["sshCwd"] = body.sshCwd
        config["sessionInfo"][body.name] = info
        if body.project:
            for p in config["projects"]:
                if p["name"] == body.project:
                    if body.name not in p["sessions"]:
                        p["sessions"].append(body.name)
                    break
        save_config(config)
    return {"ok": True}


@app.get("/api/check-worktree")
async def check_worktree(repo: str = "", branch: str = ""):
    """Check if a worktree name already exists."""
    if not repo or not branch:
        return {"exists": False}
    config = load_config()
    wt_base = get_wt_base(config)
    wt_path = os.path.join(wt_base, repo, branch)
    return {"exists": os.path.exists(wt_path), "path": wt_path}


class SessionWithWorktree(BaseModel):
    name: str
    cwd: str = "~"
    branch: str = ""
    base_branch: str = "main"
    project: str | None = None
    command: str | None = None
    description: str | None = None
    sshHost: str | None = None
    sshCwd: str | None = None

@app.post("/api/create-session-with-worktree")
async def create_session_with_worktree(body: SessionWithWorktree):
    """Create a worktree + tmux session in one step."""
    if body.sshHost:
        raise HTTPException(status_code=400, detail="Remote worktree creation is not supported yet")
    validate_name(body.name, "session name")
    validate_name(body.branch, "branch name")
    config = load_config()
    if body.name in config["sessionInfo"]:
        raise HTTPException(status_code=400, detail=f"Session '{body.name}' already exists")
    expanded_cwd = safe_path(body.cwd)
    if not os.path.isdir(expanded_cwd):
        raise HTTPException(status_code=400, detail=f"Directory not found: {body.cwd}")

    # Get repo name
    r = subprocess.run(
        ["git", "-C", expanded_cwd, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=3,
    )
    if r.returncode != 0:
        raise HTTPException(status_code=400, detail="Not a git repository")
    repo_name = os.path.basename(r.stdout.strip())

    wt_base = get_wt_base(config)
    # Two-level: worktreeBase/repoName/branchName
    repo_dir = os.path.join(wt_base, repo_name)
    os.makedirs(repo_dir, exist_ok=True)
    wt_path = os.path.join(repo_dir, body.branch)

    # Check if worktree path already exists
    if os.path.exists(wt_path):
        raise HTTPException(status_code=400, detail=f"Worktree '{body.branch}' already exists at {wt_path}")

    base = body.base_branch or get_default_branch(expanded_cwd)
    r2 = subprocess.run(
        ["git", "-C", expanded_cwd, "worktree", "add", "-b", body.branch, wt_path, base],
        capture_output=True, text=True, timeout=10,
    )
    if r2.returncode != 0:
        raise HTTPException(status_code=500, detail=r2.stderr.strip())

    # Create tmux session in the worktree
    display_path = to_home_display(wt_path)
    try:
        _ensure_session(body.name, display_path)
    except Exception as e:
        # Rollback: remove the worktree we just created
        rb = subprocess.run(["git", "-C", expanded_cwd, "worktree", "remove", wt_path, "--force"],
                            capture_output=True, text=True, timeout=10)
        msg = str(e)
        if rb.returncode != 0:
            msg += f" (rollback also failed: {rb.stderr.strip()})"
        raise HTTPException(status_code=500, detail=msg)
    if body.command:
        run_tmux("send-keys", "-t", body.name, body.command, "Enter")
    async with _config_lock:
        config = load_config()
        info = {"cwd": display_path}
        if body.description:
            info["description"] = body.description
        if body.sshHost:
            info["sshHost"] = body.sshHost
        if body.sshCwd:
            info["sshCwd"] = body.sshCwd
        config["sessionInfo"][body.name] = info
        if body.project:
            for p in config["projects"]:
                if p["name"] == body.project:
                    if body.name not in p["sessions"]:
                        p["sessions"].append(body.name)
                    break
        save_config(config)
    return {"ok": True, "path": display_path}


class SessionRename(BaseModel):
    new_name: str

@app.put("/api/sessions/{name}/rename")
async def rename_session(name: str, body: SessionRename):
    validate_name(name, "session name")
    validate_name(body.new_name, "session name")
    async with _config_lock:
        config = load_config()
        if name not in config.get("sessionInfo", {}):
            raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
        if body.new_name != name and (body.new_name in config.get("sessionInfo", {}) or body.new_name in config.get("sessionStatus", {})):
            raise HTTPException(status_code=400, detail=f"Session '{body.new_name}' already exists")
        live = get_live_sessions()
        if name in live:
            run_tmux("rename-session", "-t", name, body.new_name)
        if name in config["sessionStatus"]:
            config["sessionStatus"][body.new_name] = config["sessionStatus"].pop(name)
        if name in config["sessionInfo"]:
            config["sessionInfo"][body.new_name] = config["sessionInfo"].pop(name)
        for p in config["projects"]:
            if name in p.get("sessions", []):
                p["sessions"] = [body.new_name if s == name else s for s in p["sessions"]]
        save_config(config)
    if body.new_name in get_live_sessions():
        _set_kanban_session_env(body.new_name)
    return {"ok": True}


class CommandSend(BaseModel):
    command: str

@app.post("/api/sessions/{name}/command")
async def send_command(name: str, body: CommandSend):
    """Send a command to a running tmux session."""
    validate_name(name, "session name")
    config = load_config()
    if name not in config.get("sessionInfo", {}):
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    if name not in get_live_sessions():
        raise HTTPException(status_code=400, detail="Session is not running")
    run_tmux("send-keys", "-t", name, body.command, "Enter")
    return {"ok": True}


@app.delete("/api/sessions/{name}")
async def delete_session(name: str, force: bool = False):
    """Kill tmux session, clean up worktree if applicable, and remove from config."""
    validate_name(name, "session name")
    async with _config_lock:
        config = load_config()
        if name not in config.get("sessionInfo", {}):
            raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
        info = config.get("sessionInfo", {}).get(name, {})
        cwd = info.get("cwd", "")
        wt_base = get_wt_base(config)
        expanded_cwd = os.path.expanduser(cwd)
        # Check worktree BEFORE killing session — fail early if dirty/can't remove
        if expanded_cwd.startswith(wt_base + os.sep) and os.path.isdir(expanded_cwd):
            dirty_check = subprocess.run(
                ["git", "-C", expanded_cwd, "status", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            )
            if dirty_check.stdout.strip() and not force:
                raise HTTPException(
                    status_code=400,
                    detail=f"Worktree has uncommitted changes. Use force=true to delete anyway.\n{dirty_check.stdout.strip()}"
                )
            # Run from inside the worktree so git can locate the main repo
            # via the worktree's .git file pointer.
            cmd = ["git", "-C", expanded_cwd, "worktree", "remove", expanded_cwd]
            if force:
                cmd.append("--force")
            wt_result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if wt_result.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to remove worktree: {wt_result.stderr.strip()}. Session kept in config."
                )
            parent = os.path.dirname(expanded_cwd)
            if parent != wt_base and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        # Kill tmux session only after all checks pass. If it is already dead,
        # keep deleting the stale dashboard registration.
        if name in get_live_sessions():
            run_tmux("kill-session", "-t", name)
        # Also kill remote tmux session if SSH host is set
        _kill_remote_tmux(name, info)
        config["sessionInfo"].pop(name, None)
        config["sessionStatus"].pop(name, None)
        for p in config["projects"]:
            p["sessions"] = [s for s in p.get("sessions", []) if s != name]
        save_config(config)
    return {"ok": True}


@app.post("/api/sessions/{name}/scroll-bottom")
async def scroll_bottom(name: str):
    """Exit copy-mode if active (jumps to bottom), then refresh."""
    validate_name(name, "session name")
    if name not in load_config().get("sessionInfo", {}):
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    if name not in get_live_sessions():
        return {"ok": True}
    in_mode = run_tmux("display-message", "-t", name, "-p", "#{pane_in_mode}")
    if in_mode.strip() == "1":
        run_tmux("send-keys", "-t", name, "q")
    return {"ok": True}


def _detect_install_mode() -> str:
    """Return 'editable' if running from a git checkout (pip install -e), else 'packaged'."""
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return "editable" if os.path.isdir(os.path.join(pkg_dir, ".git")) else "packaged"


@app.get("/api/update/info")
async def update_info():
    """Tell the frontend which sources are available so it can show the right buttons."""
    return {
        "mode": _detect_install_mode(),
        "sources": ["pypi", "github"],  # always offered; editable mode also gets 'editable'
    }


class UpdateRequest(BaseModel):
    source: str = "pypi"  # "pypi" | "github" | "editable"


@app.post("/api/update")
async def update_app(body: UpdateRequest | None = None):
    """Update tmux-kanban from the requested source.

    - source='pypi'     -> pip install --upgrade tmux-kanban   (default; stable)
    - source='github'   -> pip install --upgrade git+https://github.com/linwk20/tmux-kanban.git  (latest main)
    - source='editable' -> git pull && pip install -e .        (only valid on git-checkout installs)
    """
    import sys
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = (body.source if body else "pypi").lower()
    mode = _detect_install_mode()

    # Backward compat: if the client didn't specify a source and we're on a
    # git checkout, keep the old "editable" behavior.
    if body is None and mode == "editable":
        source = "editable"

    if source == "editable":
        if mode != "editable":
            raise HTTPException(status_code=400, detail="Not a git checkout; cannot update in editable mode.")
        pull = await asyncio.to_thread(subprocess.run,
            ["git", "-C", pkg_dir, "pull", "--ff-only"],
            capture_output=True, text=True, timeout=30,
        )
        if pull.returncode != 0:
            raise HTTPException(status_code=500, detail=f"git pull failed: {pull.stderr.strip()}")
        install = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "pip", "install", "--break-system-packages", "-e", pkg_dir],
            capture_output=True, text=True, timeout=60,
        )
        if install.returncode != 0:
            raise HTTPException(status_code=500, detail=f"pip install failed: {install.stderr.strip()}")
        return {"ok": True, "source": "editable", "detail": pull.stdout.strip(),
                "message": "Updated from local checkout. Restart the server to apply changes."}

    if source == "github":
        install = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "pip", "install", "--break-system-packages", "--upgrade",
             "git+https://github.com/linwk20/tmux-kanban.git"],
            capture_output=True, text=True, timeout=120,
        )
        if install.returncode != 0:
            raise HTTPException(status_code=500, detail=f"pip install failed: {install.stderr.strip()}")
        return {"ok": True, "source": "github", "detail": "Upgraded from GitHub main",
                "message": "Updated from GitHub. Restart the server to apply changes."}

    if source == "pypi":
        install = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "pip", "install", "--break-system-packages", "--upgrade",
             "tmux-kanban"],
            capture_output=True, text=True, timeout=120,
        )
        if install.returncode != 0:
            raise HTTPException(status_code=500, detail=f"pip install failed: {install.stderr.strip()}")
        return {"ok": True, "source": "pypi", "detail": "Upgraded from PyPI",
                "message": "Updated from PyPI. Restart the server to apply changes."}

    raise HTTPException(status_code=400, detail=f"Unknown source: {source!r}")


@app.post("/api/sessions/{name}/stop")
async def stop_session(name: str):
    validate_name(name, "session name")
    config = load_config()
    if name not in config.get("sessionInfo", {}):
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    info = config.get("sessionInfo", {}).get(name, {})
    if name in get_live_sessions():
        _run_tmux(["kill-session", "-t", name], action="kill-session")
    _kill_remote_tmux(name, info)
    notify_clients("session_changed", {"session": name, "alive": False})
    return {"ok": True}


@app.post("/api/sessions/{name}/start")
async def start_session(name: str):
    validate_name(name, "session name")
    config = load_config()
    if name not in config.get("sessionInfo", {}):
        raise HTTPException(status_code=404, detail=f"Session '{name}' not found")
    info = config.get("sessionInfo", {}).get(name, {})
    cwd = info.get("cwd", "~")
    ssh_host = info.get("sshHost", "")
    ssh_cwd = info.get("sshCwd", "")
    if ssh_host:
        cwd = _HOME
    expanded = os.path.expanduser(cwd)
    if not ssh_host and not os.path.isdir(expanded):
        cwd = "~"
        async with _config_lock:
            config = load_config()
            if name in config["sessionInfo"]:
                config["sessionInfo"][name]["cwd"] = "~"
                save_config(config)
    try:
        _ensure_session(name, cwd)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    if ssh_host:
        run_tmux("send-keys", "-t", name, remote_tmux_command(ssh_host, ssh_cwd or "~", name), "Enter")
    notify_clients("session_changed", {"session": name, "alive": True})
    return {"ok": True}


@app.post("/api/scan")
async def scan_sessions():
    """Scan all live tmux sessions and register unknown ones into config."""
    config = load_config()
    live = get_live_sessions()
    raw = ""
    if live:
        raw = run_tmux(
            "list-panes", "-a",
            "-F", "#{session_name}|#{pane_current_path}"
        )
    live_cwds = {}
    if raw:
        for line in raw.splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2 and parts[0] not in live_cwds:
                live_cwds[parts[0]] = to_home_display(parts[1])

    added = []
    async with _config_lock:
        config = load_config()
        for name in live:
            if name not in config["sessionInfo"]:
                cwd = live_cwds.get(name, "~")
                # Validate cwd is within home — fallback to ~ if not
                try:
                    safe_path(cwd)
                except HTTPException:
                    cwd = "~"
                config["sessionInfo"][name] = {"cwd": cwd}
                added.append(name)
        if added:
            save_config(config)
    if added:
        _mark_registered_tmux_sessions()
    return {"ok": True, "added": added, "total": len(live)}


@app.get("/api/sessions")
async def get_sessions():
    """Return merged view: config sessions + live tmux data."""
    config = load_config()
    live = await asyncio.to_thread(get_live_sessions)

    # Get live pane details via control mode (no timeout — waits for tmux to respond)
    live_details = {}
    raw = await async_run_tmux(
        "list-panes", "-a",
        "-F", "#{session_name}|#{window_index}|#{window_name}|#{pane_id}|#{pane_width}x#{pane_height}|#{pane_current_command}|#{pane_current_path}|#{@name}"
    )
    if raw:
        for line in raw.splitlines():
            parts = line.split("|", 7)
            if len(parts) < 8:
                continue
            sess, widx, wname, pid, size, cmd, cwd, label = parts
            if sess not in live_details:
                live_details[sess] = {"name": sess, "alive": True, "windows": {}}
            if widx not in live_details[sess]["windows"]:
                live_details[sess]["windows"][widx] = {
                    "index": widx, "name": wname, "panes": []
                }
            live_details[sess]["windows"][widx]["panes"].append({
                "id": pid, "size": size, "command": cmd, "cwd": cwd,
                "label": label or None,
            })

    # Only show sessions registered in config (use Scan to discover new ones)
    all_session_names = set(config["sessionInfo"].keys()) - {"_kanban_ctrl"}
    result = []
    for name in sorted(all_session_names):
        if name in live_details:
            entry = live_details[name]
            entry["windows"] = list(entry["windows"].values())
        else:
            info = config["sessionInfo"].get(name, {})
            entry = {
                "name": name,
                "alive": False,
                "windows": [{
                    "index": "0", "name": "-", "panes": [{
                        "id": "-", "size": "-", "command": "-",
                        "cwd": info.get("cwd", "~"), "label": None,
                    }]
                }],
            }
        # Read cached activity state (updated by background poller)
        if entry.get("alive"):
            info = get_activity_info(name)
            entry["active"] = info["active"]
            entry["activityLabel"] = info["label"]
        else:
            entry["active"] = False
            entry["activityLabel"] = "stopped"

        # Add git info
        sess_info = config.get("sessionInfo", {}).get(name, {})
        sess_cwd = sess_info.get("cwd", "~")
        if not sess_cwd or sess_cwd == "~":
            first_pane = entry["windows"][0]["panes"][0] if entry["windows"] else None
            if first_pane and first_pane["cwd"] not in ("-", ""):
                sess_cwd = first_pane["cwd"]
        if sess_info.get("sshHost"):
            is_git, git_branch = False, None
        else:
            is_git, git_branch = await asyncio.to_thread(check_git_cached, sess_cwd)
        entry["isGit"] = is_git
        entry["gitBranch"] = git_branch
        entry["needsAttention"] = sess_info.get("needsAttention", False)
        entry["attentionMessage"] = sess_info.get("attentionMessage", "")
        entry["attentionAt"] = sess_info.get("attentionAt", "")
        result.append(entry)

    return {"sessions": result}


# ── Worktree management ──

@app.get("/api/git/{session_name}/worktrees")
async def list_worktrees(session_name: str):
    validate_name(session_name, "session name")
    config = load_config()
    info = config.get("sessionInfo", {}).get(session_name, {})
    if not info:
        raise HTTPException(status_code=404, detail=f"Session '{session_name}' not found")
    if info.get("sshHost"):
        return {"worktrees": [], "defaultBranch": "main"}
    cwd = os.path.expanduser(info.get("cwd", "~"))
    r = subprocess.run(
        ["git", "-C", cwd, "worktree", "list", "--porcelain"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        return {"worktrees": []}
    worktrees = []
    current = {}
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            path = line[9:]
            current = {"path": to_home_display(path), "branch": None}
        elif line.startswith("branch "):
            current["branch"] = line[7:].split("/")[-1]
        elif line == "bare":
            current["branch"] = "(bare)"
        elif line == "detached":
            current["branch"] = "(detached)"
    if current:
        worktrees.append(current)
    return {"worktrees": worktrees, "defaultBranch": get_default_branch(cwd)}


class WorktreeCreate(BaseModel):
    branch: str
    base_branch: str = ""

@app.post("/api/git/{session_name}/worktree")
async def create_worktree(session_name: str, body: WorktreeCreate):
    validate_name(session_name, "session name")
    validate_name(body.branch, "branch name")
    config = load_config()
    info = config.get("sessionInfo", {}).get(session_name, {})
    if not info:
        raise HTTPException(status_code=404, detail=f"Session '{session_name}' not found")
    if info.get("sshHost"):
        raise HTTPException(status_code=400, detail="Remote worktree creation is not supported yet")
    cwd = os.path.expanduser(info.get("cwd", "~"))
    new_sess_name = f"{session_name}-{body.branch}"
    if new_sess_name in config.get("sessionInfo", {}):
        raise HTTPException(status_code=400, detail=f"Session '{new_sess_name}' already exists")
    wt_base = get_wt_base(config)
    os.makedirs(wt_base, exist_ok=True)

    # Use <repo>/<branch> layout (consistent with create_session_with_worktree)
    repo_top = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=3,
    )
    repo_name = os.path.basename(repo_top.stdout.strip()) if repo_top.returncode == 0 else session_name
    repo_dir = os.path.join(wt_base, repo_name)
    os.makedirs(repo_dir, exist_ok=True)
    wt_path = os.path.join(repo_dir, body.branch)
    base = body.base_branch or get_default_branch(cwd)

    r = subprocess.run(
        ["git", "-C", cwd, "worktree", "add", "-b", body.branch, wt_path, base],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr.strip())

    # Auto-create tmux session
    display_path = to_home_display(wt_path)
    try:
        ensure_tmux_session(new_sess_name, display_path)
    except RuntimeError as e:
        # Rollback: remove the worktree we just created
        rb = subprocess.run(["git", "-C", cwd, "worktree", "remove", wt_path, "--force"],
                            capture_output=True, text=True, timeout=10)
        msg = str(e)
        if rb.returncode != 0:
            msg += f" (rollback also failed: {rb.stderr.strip()})"
        raise HTTPException(status_code=500, detail=msg)
    async with _config_lock:
        config = load_config()
        config["sessionInfo"][new_sess_name] = {"cwd": display_path}
        for p in config["projects"]:
            if session_name in p.get("sessions", []):
                if new_sess_name not in p["sessions"]:
                    p["sessions"].append(new_sess_name)
                break
        save_config(config)
    return {"ok": True, "session": new_sess_name, "path": display_path}


@app.delete("/api/git/{session_name}/worktree/{branch}")
async def delete_worktree(session_name: str, branch: str, force: bool = False):
    validate_name(session_name, "session name")
    validate_name(branch, "branch name")
    config = load_config()
    info = config.get("sessionInfo", {}).get(session_name, {})
    if not info:
        raise HTTPException(status_code=404, detail=f"Session '{session_name}' not found")
    if info.get("sshHost"):
        raise HTTPException(status_code=400, detail="Remote worktree deletion is not supported yet")
    cwd = os.path.expanduser(info.get("cwd", "~"))
    wt_base = get_wt_base(config)
    # Try new layout (repo/branch) first, fall back to legacy (session-branch)
    repo_top = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=3,
    )
    repo_name = os.path.basename(repo_top.stdout.strip()) if repo_top.returncode == 0 else session_name
    wt_path = os.path.join(wt_base, repo_name, branch)
    if not os.path.isdir(wt_path):
        wt_path = os.path.join(wt_base, f"{session_name}-{branch}")  # legacy fallback

    if os.path.isdir(wt_path):
        dirty_check = subprocess.run(
            ["git", "-C", wt_path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if dirty_check.stdout.strip() and not force:
            raise HTTPException(
                status_code=400,
                detail=f"Worktree has uncommitted changes. Use force=true to delete anyway.\n{dirty_check.stdout.strip()}"
            )

    cmd = ["git", "-C", cwd, "worktree", "remove", wt_path]
    if force:
        cmd.append("--force")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr.strip())
    return {"ok": True}


@app.get("/api/tmux-buffer")
async def get_tmux_buffer(session: str = ""):
    """Get tmux paste buffer. If empty, fall back to visible pane content."""
    try:
        content = run_tmux("show-buffer")
    except TmuxError as e:
        if "no buffer" not in str(e).lower():
            raise
        content = ""
    if not content and session:
        content = run_tmux("capture-pane", "-t", session, "-p")
    return {"content": content}


@app.get("/api/panes/{pane_id:path}/capture")
async def capture_pane(pane_id: str):
    content = run_tmux("capture-pane", "-t", pane_id, "-p", "-e")
    return {"pane_id": pane_id, "content": content}


@app.websocket("/ws/events")
async def events_ws(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    config = load_config()
    if not config.get("password_hash") or token not in _valid_tokens:
        await websocket.close(code=4001, reason="Unauthorized")
        return
    await websocket.accept()
    _event_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "connected", "seq": _event_seq, "payload": {}}))
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        _event_clients.discard(websocket)


@app.websocket("/ws/terminal/{session_name}")
async def terminal_ws(websocket: WebSocket, session_name: str):
    # Check auth token from query param
    token = websocket.query_params.get("token", "")
    config = load_config()
    if not config.get("password_hash") or token not in _valid_tokens:
        await websocket.close(code=4001, reason="Unauthorized")
        return
    await websocket.accept()

    # Attach only. Opening a terminal must not create or restart sessions.
    live = await asyncio.to_thread(get_live_sessions)
    if session_name not in live:
        await websocket.close(code=4002, reason="Session is not running")
        return

    # Each WebSocket creates its own PTY → its own tmux client.
    # Mouse tracking: tmux mouse mode + TUI apps prevent xterm.js native selection.
    master_fd, slave_fd = pty.openpty()

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])
        os._exit(1)

    _pty_pids.add(pid)
    os.close(slave_fd)

    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()
    pty_queue = asyncio.Queue()

    def on_pty_readable():
        try:
            data = os.read(master_fd, 65536)
            if data:
                pty_queue.put_nowait(data)
        except OSError:
            pass

    loop.add_reader(master_fd, on_pty_readable)

    async def read_pty():
        try:
            while True:
                data = await pty_queue.get()
                await websocket.send_bytes(data)
        except (WebSocketDisconnect, Exception):
            pass

    reader_task = asyncio.create_task(read_pty())

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" in msg:
                text = msg["text"]
                if text == "":
                    continue  # heartbeat, ignore
                elif text.startswith("resize:"):
                    try:
                        _, dims = text.split(":", 1)
                        cols, rows = dims.split(",")
                        winsize = struct.pack("HHHH", int(rows), int(cols), 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                    except (ValueError, OSError):
                        pass
                else:
                    os.write(master_fd, text.encode())
            elif "bytes" in msg:
                os.write(master_fd, msg["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        reader_task.cancel()
        loop.remove_reader(master_fd)
        os.close(master_fd)
        try:
            os.kill(pid, signal.SIGTERM)
            # Try non-blocking waitpid first, then blocking if child hasn't exited
            for _ in range(10):
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                if wpid != 0:
                    break
                await asyncio.sleep(0.1)
            else:
                os.waitpid(pid, 0)  # final blocking wait
        except (OSError, ChildProcessError):
            pass
        finally:
            _pty_pids.discard(pid)
