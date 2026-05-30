# Advanced Use Cases

Everything below is optional — `tmux-kanban` alone is enough for a single user on `localhost`. These recipes cover production deployment, remote access, and custom configuration.

- [CLI Options](#cli-options)
- [Run as a systemd user service](#run-as-a-systemd-user-service)
- [Remote Access via SSH Tunnel](#remote-access-via-ssh-tunnel)
- [Public IP / Reverse Proxy](#public-ip--reverse-proxy)
- [Agent Attention on Remote Servers](#agent-attention-on-remote-servers)

---

## CLI Options

```bash
tmux-kanban --port 9000       # Custom port
tmux-kanban --host 0.0.0.0    # Bind to all interfaces
tmux-kanban --config /path/to/config.json         # Custom config location
tmux-kanban --worktree-path /path/to/worktrees    # Custom worktree folder
```

---

## Run as a systemd user service

For remote servers, this is the most reliable way to keep tmux-kanban alive and to make browser terminals work cleanly under `systemd --user`:

```bash
tmux-kanban-install-systemd
```

That writes `~/.config/systemd/user/tmux-kanban.service`, enables it, and starts it with:

- `Restart=always`
- `TERM=xterm-256color`
- `COLORTERM=truecolor`

Common variants:

```bash
tmux-kanban-install-systemd --public
tmux-kanban-install-systemd --port 9000
tmux-kanban-install-systemd --service-name tmux-kanban-web
tmux-kanban-install-systemd --config ~/.tmux-kanban/config.json --worktree-path ~/.tmux-kanban/worktrees
```

Useful commands:

```bash
systemctl --user status tmux-kanban.service
systemctl --user restart tmux-kanban.service
journalctl --user -u tmux-kanban.service -f
```

The installer also attempts to enable linger for the current user and prints whether it was already on or just enabled. If your system blocks that, run:

```bash
loginctl enable-linger "$USER"
```

---

## Remote Access via SSH Tunnel

Running tmux-kanban on a remote server? You need to forward the port to your local machine.

**If you use VS Code Remote** — it auto-forwards ports, no extra setup needed. Just open `http://localhost:59235`.

**If you use a terminal** — run this on your **local machine** (not the server):

```bash
ssh -L 59235:localhost:59235 <your-ssh-host>
#         ^                    ^
#    local port           Host in ~/.ssh/config
```

Then open `http://localhost:59235` in your local browser. If local port 59235 is already taken, change the first number (e.g. `-L 9090:localhost:59235`, then open `http://localhost:9090`).

---

## Public IP / Reverse Proxy

If your server has a public IP address, or you want to use a reverse proxy (Cloudflare Tunnel, ngrok, frpc, etc.), bind to all interfaces with `--host 0.0.0.0` and access it directly.

> A detailed tutorial for this setup is coming soon. In the meantime, because tmux-kanban ships with password + bearer-token auth by default, exposing it publicly is safe as long as you set a strong password on first visit.

---

## Agent Attention on Remote Servers

When a coding agent runs on a *different* machine than tmux-kanban, you need two files on the remote server so the agent can ping the kanban API.

### What needs to be on the remote server

Only **two files** — no tmux-kanban install needed:

| File | Purpose |
|------|---------|
| `~/.tmux-kanban/alert-agent-needs-you.sh` | Shell script that calls the kanban API |
| `~/.tmux-kanban/agent-token` | Auth token (auto-generated on first kanban startup) |

### OpenCode: step by step

On your **kanban server** (the machine running `tmux-kanban`):

```bash
# Copy helper files to the remote
REMOTE="user@remote-host"
ssh "$REMOTE" "mkdir -p ~/.tmux-kanban ~/.config/opencode/plugins"

scp ~/.tmux-kanban/alert-agent-needs-you.sh "$REMOTE:~/.tmux-kanban/"
scp ~/.tmux-kanban/agent-token          "$REMOTE:~/.tmux-kanban/"

# Deploy the OpenCode plugin
scp ~/.config/opencode/plugins/tmux-kanban.js "$REMOTE:~/.config/opencode/plugins/"
```

On the **remote server**, set the kanban server's address:

```bash
ssh "$REMOTE"
echo 'export TMUX_KANBAN_HOST=100.82.16.51' >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc
```

**Done.** Next time you run `opencode` on the remote, the plugin is active. It reads `TMUX_KANBAN_HOST` from the environment, passes it to `alert-agent-needs-you.sh`, which uses `agent-token` to call the kanban API.

### How it works

```
Remote opencode needs approval
  → plugin reads TMUX_KANBAN_HOST from process.env
  → calls alert-agent-needs-you.sh with HOST injected
  → curl PUT http://100.82.16.51:59235/api/sessions/{name}/attention
  → kanban server validates agent-token
  → session card glows red on the board
```

The `agent-token` is a shared secret — it's the same file on both machines. The kanban server middleware recognizes it and bypasses normal password auth for the attention endpoint.

### Other agent CLIs (Claude Code, Codex, Kimi)

Same principle — just the two helper files + environment variable. Follow the hook configs in [README](../README.md#-agent-attention-setup), but ensure `TMUX_KANBAN_HOST` is set on the remote machine.

### Troubleshooting

- **No notification appears**: check that `TMUX_KANBAN_HOST` is reachable from the remote (`curl http://100.82.16.51:59235/api/auth/status` should respond)
- **401 Unauthorized**: the `agent-token` on the remote doesn't match the kanban server. Re-copy `~/.tmux-kanban/agent-token` from the kanban server.
- **Wrong session highlighted**: the script uses `tmux display-message -p '#S'` to detect the session name. If opencode runs outside tmux, set `export TMUX_KANBAN_SESSION=your-session-name` on the remote.
