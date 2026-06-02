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
//   export TMUX_KANBAN_URL=http://100.xxx.xxx.xxx:59235
// or:
//   export TMUX_KANBAN_HOST=100.xxx.xxx.xxx
// Or set per-project: see README.

const DEBOUNCE_MS = 3000; // avoid flooding on rapid permission prompts

export const TmuxKanbanPlugin = async ({ $ }) => {
  let _lastNotifyAt = 0;

  async function _notify(message) {
    const now = Date.now();
    if (now - _lastNotifyAt < DEBOUNCE_MS) return;
    _lastNotifyAt = now;
    try {
      await $`bash ~/.tmux-kanban/alert-agent-needs-you.sh ${message}`.quiet();
    } catch {
      // Keep OpenCode usable even if tmux-kanban is not running.
    }
  }

  return {
    event: async ({ event }) => {
      switch (event.type) {
        case "permission.asked":
          await _notify("Needs your choice");
          break;
        case "session.idle":
          await _notify("Task output complete");
          break;
        case "session.error":
          await _notify("Task hit an error");
          break;
      }
    },
  };
};
