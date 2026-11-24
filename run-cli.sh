#!/usr/bin/env bash
# CLI variant: variant W on the interactive Claude Code CLI, driven through tmux.
#
# Usage: run-cli.sh [LABEL]
#   env: REPRO_DIR (default: this script's directory), TIMEOUT (default 600 s),
#        QUIET (default 60 s: stop after the screen is unchanged this long once round 1 ran)
#
# Output: $REPRO_DIR/runs/<LABEL>/ with
#   pane.log        every change of the tmux pane, each block headed "=== <epoch> ==="
#   final-pane.txt  the full scrollback at the end
#   debug.log       claude's --debug-file output (local only, holds paths; do not share it)
#   events.log      epoch of the first sight of nonce.num and r2.txt
#   agents.inline.json, launch.sh, meta.json, cwd/, nonce.num, r2.txt
# Score it with: python3 -I score.py runs/<LABEL>
# The interactive CLI has no machine-readable stream, so score.py can establish less than for
# the stream-json runs and reports validity as NOT ESTABLISHED: read pane.log yourself.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPRO_DIR=${REPRO_DIR:-$HERE}
LABEL=${1:-CLI-$(date -u +%Y%m%dT%H%M%SZ)}
TIMEOUT=${TIMEOUT:-600}
QUIET=${QUIET:-60}

if ! command -v tmux >/dev/null 2>&1; then
  echo "run-cli.sh: tmux is not installed: the CLI variant was not run" >&2
  exit 3
fi

RUN=$REPRO_DIR/runs/$LABEL
if [ -e "$RUN/pane.log" ]; then
  echo "run-cli.sh: $LABEL already has a pane.log; pick another label" >&2
  exit 2
fi
mkdir -p "$RUN/cwd"

python3 -I "$REPRO_DIR/driver.py" --variant W --run-dir "$RUN" --render-only >"$RUN/agents.inline.json"
ALLOWED=$(python3 -I "$REPRO_DIR/driver.py" --print-allowed)
VERSION=$(claude --version 2>/dev/null | head -n 1 || true)

cat >"$RUN/launch.sh" <<EOF
#!/usr/bin/env bash
cd $(printf %q "$RUN/cwd")
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SSE_PORT
exec claude --agent repro-root \\
  --agents "\$(cat $(printf %q "$RUN/agents.inline.json"))" \\
  --setting-sources project,local --strict-mcp-config --disable-slash-commands \\
  --allowedTools "$ALLOWED" \\
  --debug-file $(printf %q "$RUN/debug.log") \\
  START
EOF

SESSION="repro-$$"
START=$(date +%s.%N)
tmux new-session -d -s "$SESSION" -x 220 -y 50 "bash $(printf %q "$RUN/launch.sh")" \; \
  set-option -t "$SESSION" remain-on-exit on

LAST=""
LAST_CHANGE=$(date +%s)
SEEN_NONCE=0
SEEN_R2=0
TIMED_OUT=false
: >"$RUN/pane.log"
: >"$RUN/events.log"

while :; do
  NOW=$(date +%s)
  if [ $((NOW - ${START%.*})) -ge "$TIMEOUT" ]; then
    TIMED_OUT=true
    break
  fi
  PANE=$(tmux capture-pane -p -t "$SESSION" -S - 2>/dev/null || true)
  if [ "$PANE" != "$LAST" ]; then
    printf '=== %s ===\n%s\n' "$(date +%s.%N)" "$PANE" >>"$RUN/pane.log"
    LAST=$PANE
    LAST_CHANGE=$NOW
    # First-run folder trust dialog for the fresh working directory: accept the default.
    if printf '%s' "$PANE" | grep -qiE 'trust the files|trust this folder|Do you trust'; then
      tmux send-keys -t "$SESSION" Enter
    fi
  fi
  if [ "$SEEN_NONCE" = 0 ] && [ -s "$RUN/nonce.num" ]; then
    SEEN_NONCE=1
    echo "$(date +%s.%N) nonce.num seen" >>"$RUN/events.log"
  fi
  if [ "$SEEN_R2" = 0 ] && [ -s "$RUN/r2.txt" ]; then
    SEEN_R2=1
    echo "$(date +%s.%N) r2.txt seen" >>"$RUN/events.log"
  fi
  if [ "$SEEN_NONCE" = 1 ] && [ $((NOW - LAST_CHANGE)) -ge "$QUIET" ]; then
    break
  fi
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    break
  fi
  sleep 1
done

tmux capture-pane -p -t "$SESSION" -S - >"$RUN/final-pane.txt" 2>/dev/null || true
tmux kill-session -t "$SESSION" 2>/dev/null || true

END=$(date +%s.%N)
cat >"$RUN/meta.json" <<EOF
{
  "variant": "CLI",
  "label": "$LABEL",
  "start_epoch": $START,
  "end_epoch": $END,
  "claude_version_flag": "$VERSION",
  "timed_out": $TIMED_OUT,
  "timeout_seconds": $TIMEOUT,
  "quiet_seconds": $QUIET,
  "env_set": {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
}
EOF
echo "run $LABEL: timed_out=$TIMED_OUT; score with: python3 -I score.py runs/$LABEL"
