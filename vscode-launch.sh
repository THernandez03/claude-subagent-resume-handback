#!/usr/bin/env bash
# Wrapper for the VS Code extension setting "claudeCode.claudeProcessWrapper".
#
# Usage (set by the extension, never by hand):
#   vscode-launch.sh <claude binary> <extension arguments...>
#
# Default is pass-through: the binary is started with the same arguments and
# environment. The repro is injected only when both hold:
#   (a) the call is the chat-session launch of the extension, and
#   (b) the file .resume-handback-repro exists in the launch's working
#       directory (the folder opened in VS Code).
#
# How the chat-session launch is told apart (extension 2.1.331): it always
# passes "--input-format stream-json" and "--output-format stream-json" as
# separate arguments, and "--permission-prompt-tool=stdio" because the
# extension handles tool permissions itself. The other launches through the
# wrapper (workspace-trust, auth status, plugin and mcp commands) are
# subcommands run with --json and carry none of these. The config probe and
# the cloud-session read also look like a chat session, but they set the
# environment variable CLAUDE_CODE_CONFIG_PROBE (and pass --safe-mode in the
# second case), so those are excluded.
#
# Assumption: each value flag the launcher strips (such as --allowedTools) takes one value, as "--flag value" or "--flag=value".
#
# The extension reads the binary's stdout, so this script writes nothing to
# stdout. Diagnostics go to stderr.

set -eu

REPRO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -lt 1 ]; then
  echo "vscode-launch.sh: missing the claude binary argument" >&2
  exit 2
fi

binary="$1"
shift

# Flags the repro sets itself. The extension may already pass some of them, in
# the form "--flag=value" or "--flag value". Every occurrence is removed first
# so the injected value is the only one.
value_flags=(--agent --agents --setting-sources --allowedTools --allowed-tools --debug-file)
bool_flags=(--strict-mcp-config --disable-slash-commands)

in_array() {
  local needle="$1" item
  shift
  for item in "$@"; do
    [ "$item" = "$needle" ] && return 0
  done
  return 1
}

# True when the arguments are the chat-session launch.
is_chat_launch() {
  local i n="$#" a next
  local input_fmt=0 output_fmt=0 stdio_prompt=0 safe_mode=0
  local args=("$@")
  for ((i = 0; i < n; i++)); do
    a="${args[i]}"
    next=""
    if [ $((i + 1)) -lt "$n" ]; then next="${args[i + 1]}"; fi
    case "$a" in
      --input-format=stream-json) input_fmt=1 ;;
      --input-format) [ "$next" = "stream-json" ] && input_fmt=1 ;;
      --output-format=stream-json) output_fmt=1 ;;
      --output-format) [ "$next" = "stream-json" ] && output_fmt=1 ;;
      --permission-prompt-tool=stdio) stdio_prompt=1 ;;
      --permission-prompt-tool) [ "$next" = "stdio" ] && stdio_prompt=1 ;;
      --safe-mode) safe_mode=1 ;;
    esac
  done
  [ "$input_fmt" = 1 ] && [ "$output_fmt" = 1 ] && [ "$stdio_prompt" = 1 ] &&
    [ "$safe_mode" = 0 ] && [ -z "${CLAUDE_CODE_CONFIG_PROBE:-}" ]
}

if ! is_chat_launch "$@" || [ ! -f "$PWD/.resume-handback-repro" ]; then
  exec "$binary" "$@"
fi

# Injection.
mkdir -p "$REPRO_DIR/runs"
run_dir="$(mktemp -d "$REPRO_DIR/runs/vscode-XXXXXX")"

python3 -I "$REPRO_DIR/driver.py" --variant W --run-dir "$PWD" --render-only >"$run_dir/agents.json"
allowed="$(python3 -I "$REPRO_DIR/driver.py" --print-allowed)"

kept=()
skip_next=0
for a in "$@"; do
  if [ "$skip_next" = 1 ]; then
    skip_next=0
    continue
  fi
  case "$a" in
    *=*)
      name="${a%%=*}"
      if in_array "$name" "${value_flags[@]}"; then continue; fi
      ;;
    *)
      if in_array "$a" "${value_flags[@]}"; then
        skip_next=1
        continue
      fi
      if in_array "$a" "${bool_flags[@]}"; then continue; fi
      ;;
  esac
  kept+=("$a")
done

export DISABLE_AUTOUPDATER=1
export CLAUDE_CODE_DISABLE_CLAUDE_MDS=1
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1

echo "vscode-launch.sh: injecting the repro, run dir $run_dir" >&2

exec "$binary" ${kept[@]+"${kept[@]}"} \
  --agent repro-root \
  --agents "$run_dir/agents.json" \
  --setting-sources project,local \
  --strict-mcp-config \
  --disable-slash-commands \
  --allowedTools "$allowed" \
  --debug-file "$run_dir/debug.log"
