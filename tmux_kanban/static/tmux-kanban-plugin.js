// tmux-kanban OpenCode Plugin
// Copy to ~/.config/opencode/plugins/tmux-kanban.js
// or .opencode/plugins/tmux-kanban.js in your project.
//
// When OpenCode asks for permission, this plugin notifies
// tmux-kanban so you can see it on the kanban board.
//
// Requires: ~/.tmux-kanban/alert-agent-needs-you.sh
// (auto-deployed by tmux-kanban on first startup).
//
// Remote kanban server? Set in your shell profile:
//   export TMUX_KANBAN_HOST=100.xxx.xxx.xxx
// Or set per-project: see README.

const DEBOUNCE_MS = 3000; // avoid flooding on rapid permission prompts

export const TmuxKanbanPlugin = async ({ $ }) => {
  let _debounceTimer = null;
  const host = process.env.TMUX_KANBAN_HOST || "";
  const port = process.env.TMUX_KANBAN_PORT || "";
  const envPrefix = [
    host ? `TMUX_KANBAN_HOST=${host}` : "",
    port ? `TMUX_KANBAN_PORT=${port}` : "",
  ].filter(Boolean).join(" ");

  function _notify() {
    if (_debounceTimer) clearTimeout(_debounceTimer);
    _debounceTimer = setTimeout(() => {
      $`${envPrefix} bash ~/.tmux-kanban/alert-agent-needs-you.sh "OpenCode needs your approval"`
        .quiet()
        .catch(() => {});
    }, DEBOUNCE_MS);
  }

  return {
    event: async ({ event }) => {
      switch (event.type) {
        case "permission.asked":
          _notify();
          break;
        case "session.idle":
          _notify();
          break;
        case "session.error":
          _notify();
          break;
      }
    },
  };
};
