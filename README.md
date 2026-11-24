# Claude Code 2.1.331: a subagent that resumes its child with SendMessage completes at once, and no re-wake or delivery to it is seen in the stream (6 of 6 valid runs; reproduces on the CLI binary and the VS Code extension's bundled binary, which are the same file)

Filing status (checked 2026-11-24): not filed upstream yet. Closest existing reports in `anthropics/claude-code`: [#111841](https://github.com/anthropics/claude-code/issues/111841) (open, 2.1.326, desktop app) reports the resumed child's reply going to the main session instead of the resumer, but only when the resumer was itself resumed by the main session, and its own control case (the resumer on its first turn, which is the case reproduced here) is reported as working; [#89042](https://github.com/anthropics/claude-code/issues/89042) (closed 2026-08-27, a maintainer comment says a fix landed) describes the same family of misrouting. Details in section 8.

## 1. Summary

- A background subagent (the resumer) starts a child in the foreground, gets its report, resumes the child with `SendMessage`, and ends its turn to wait for the child's reply.
- On Claude Code 2.1.331, 1.4 to 2.9 s after the `SendMessage` result, the resumer is reported as completed to its parent, typically with the text "waiting for the child.", while the child it resumed is still running. The child's reply then reaches the root session as the child's own completion. The stream shows no further event of the resumer for the roughly 60 s the run keeps watching.
- 6 of 6 valid runs on the shipped agents (cli331-W1, W2, W3 on the CLI binary; ext331-W1, W2, W3 on the extension's bundled binary), plus 3 valid cause-test runs and 1 valid debug run with the same result: 10 of 10 valid runs. All are outcome O1 (defined in section 6).
- It matters because a coordinating subagent cannot reuse a helper subagent across rounds: it has to start a fresh helper every round and re-send all context.
- The VS Code extension's own launch (permission dialogs, its flags) was not run. Section 7 is a manual check for that.

## 2. Environment

- Claude Code 2.1.331 (`claude --version` and the `system/init` line of every stream). The CLI binary (`claude` on `PATH`, the `versions/2.1.331` file of the install) and the VS Code extension's bundled binary (`resources/native-binary/claude` under the extension's install directory) have the same `sha256sum` and the same size (398,622,992 bytes): they are one file. The labels `cli331-*` and `ext331-*` below differ only in which path was passed to `--claude`. On 2026-11-24 2.1.331 was the newest release on the project's GitHub releases page (not compared with npm).
- Linux (WSL2). `DISABLE_AUTOUPDATER=1` in every run.
- Surface: `claude -p --input-format stream-json --output-format stream-json --verbose`, the input surface the Agent SDK and the VS Code extension use. This is driven by `driver.py`, not by the extension.
- Models, as aliases in the agent definitions: root `haiku`, resumer `opus`, child `haiku`. Resolved ids in the `assistant` messages (cli331-W1, ext331-W3): `claude-haiku-5-5` for the root and the child, `claude-opus-5-5` for the resumer.
- Exact command, as `driver.py` runs it (the working directory is a fresh empty directory per run):

```sh
claude -p --input-format stream-json --output-format stream-json --verbose \
  --forward-subagent-text \
  --agent repro-root --agents "$RUN_DIR/agents.json" \
  --setting-sources project,local --strict-mcp-config --disable-slash-commands \
  --allowedTools "Agent,SendMessage,Bash(shuf *),Bash(cat *),Bash(sleep *)"
```

- Environment: `driver.py` sets three variables (`DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) and removes three (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT`). Every other variable of the shell it is started from is inherited (section 3). The runs below were started from a shell that sat inside another Claude Code session, so before each run every variable whose name starts with `CLAUDE`, except `CLAUDE_CONFIG_DIR`, was removed with the command in section 3. `env_names_present` in each run's `meta.json` lists only `CLAUDE_CODE_DISABLE_AUTO_MEMORY` and `CLAUDE_CODE_DISABLE_CLAUDE_MDS` in all 12 runs: no `ANTHROPIC_*` variable and no `CLAUDE_CONFIG_DIR` was set.
- Login comes from the existing login of the machine. No setting, key or hook is copied into this repository.
- Loaded configuration, from `system/init`, identical in all 12 runs: permission mode `default`; agents `claude`, `Explore`, `general-purpose`, `Plan`, `statusline-setup` (built in) plus the three `repro-*` agents; three plugins, all `builtin` (`cc-plugin-agents-md`, `cc-plugin-privacy`, `cc-plugin-plugin-authoring`); 0 MCP servers; 0 skills; 0 slash commands; the root's tool list shows only `Task` (the root's definition grants only `Agent`). `--setting-sources project,local` excludes user-level settings. Files that Claude Code always reads or writes in the home directory (login, global config, session transcripts) still apply.
- Run data: every run writes `runs/<label>/` (`stream.jsonl` with receipt times, `stderr.txt`, `agents.json`, `meta.json`, the `cwd/` working directory, and the child's files). `score.txt` is not written by `driver.py` or `score.py`; the saved ones in this repository's runs were made by a hand redirect (section 3). `runs/` is gitignored and not published because it holds local paths. Regenerate it with the commands in section 3. Times move by a second or two between runs.

## 3. Steps to reproduce

Prerequisites: Linux or WSL, a logged-in `claude` on `PATH`, `python3`, GNU `shuf` and `cat`. No MCP server, plugin or settings file is needed.

Clone this repository, then from inside the clone:

```sh
python3 -I driver.py --variant W --label W1     # about 77 s wall time (the driver waits 60 s of silence at the end)
python3 -I score.py runs/W1                      # validity, timeline and points (a) to (f)
python3 -I score.py runs/W1 > runs/W1/score.txt  # optional hand redirect: how the saved score.txt files were made
```

To run the extension's bundled binary instead, pass its path: `--claude <extension dir>/resources/native-binary/claude`.

Inherited variables. `driver.py` removes only the three variables named in section 2. Every other variable of the shell is passed on to `claude`, including the `CLAUDE_*` variables that an outer Claude Code session sets for its own children (session id, messaging socket and token, subagent settings and similar). From a plain terminal there is usually nothing to remove. From inside another Claude Code session, start each run with this prefix, which removes every variable starting with `CLAUDE` except `CLAUDE_CONFIG_DIR` and keeps every `ANTHROPIC_*` variable and the login:

```sh
env $(env | sed -n 's/^\(CLAUDE[A-Za-z0-9_]*\)=.*/\1/p' | grep -vx CLAUDE_CONFIG_DIR | sed 's/^/-u /') \
  python3 -I driver.py --variant W --label W1 --timeout 540
```

Then check `env_names_present` in `runs/W1/meta.json`: it should list only the two `CLAUDE_CODE_DISABLE_*` names, plus any `ANTHROPIC_*` or `CLAUDE_CONFIG_DIR` you set.

The SubagentHandback template (the cause test, section 6). This builds `agents-handback.json`, a copy of `agents.json` in which the resumer's tools become `Agent`, `SendMessage`, `SubagentHandback` and the child's become `Bash`, `SubagentHandback`:

```sh
python3 -I -c 'import json;a=json.load(open("agents.json"));a["repro-resumer"]["tools"]=["Agent","SendMessage","SubagentHandback"];a["repro-child"]["tools"]=["Bash","SubagentHandback"];json.dump(a,open("agents-handback.json","w"),indent=2)'
python3 -I driver.py --variant W --label WH1 --agents-template agents-handback.json
```

This changes the tools of both the resumer and the child, so a difference in outcome could not be pinned on one of them. The `--allowedTools` list is not changed. A permission denial of a `SubagentHandback` call would have called for a wrapper that adds it to the allowed list; none occurred (section 6), so no wrapper was used.

What a run does:

1. `repro-root` (haiku) starts `repro-resumer` with `run_in_background: true`, then only relays every completion notification it receives as one line `NOTIFICATION <n>: <text>`.
2. `repro-resumer` (opus, tools `Agent` and `SendMessage`) starts `repro-child` in the foreground and waits for its report.
3. `repro-child` (haiku, tool `Bash`) writes a random number to a file, tries to print it, and returns "round 1 done".
4. The resumer calls `SendMessage` to the child with the text "Round 2." (no number in it), then, in variant W, ends its turn with the plain-text line "waiting for the child" and calls no tool.
5. The resumed child is meant to state the number from memory, runs one Bash command that writes `r2.txt`, and ends with `ROUND2-DONE <number>`.

Variants: `W` as above. `H` has the resumer call `SubagentHandback` itself right after the `SendMessage`. `L` is W with a `sleep 300` in the child's resumed turn (not used in the 2.1.331 runs). `CLI` (`run-cli.sh`) is W on the interactive CLI through tmux (not used in the 2.1.331 runs).

Cost: a run finishes its real work in about 18 s and then waits 60 s for further events; elapsed time was 76.8 to 78.7 s in the valid W runs. The resumer's usage counter in the root's notifications was about 7,000 tokens per run (6,978 in cli331-W3, 7,082 in ext331-WH1). No dollar figure was measured.

## 4. Expected behavior

Either of these, so that a subagent that resumes a child can receive that child's reply:

- the resumer is kept alive (not completed, no completion sent to its parent) while a child it resumed is running, and the child's reply is delivered to the resumer as its next turn; or
- the reply is delivered to the resumer in the turn that is waiting.

In both cases the resumer's parent receives one completion for the resumer's run, after the resumer has seen the reply. The sub-agents documentation, as quoted in #111841, says: "the resumed agent then reports back to the subagent that resumed it, not to your main conversation."

## 5. Actual behavior

Run cli331-W1 (valid, 2.1.331). Times are seconds since the driver started the session, from the receipt time of each stream line. "root", "resumer" and "child" are the three agents; ids are replaced by labels.

| Time | Who | Event |
|---|---|---|
| 0.47 | system | `init` (first) |
| 1.73 | root | `Agent` call, `repro-resumer`, `run_in_background: true` |
| 1.96 | system | `task_started` for the resumer |
| 2.79 | root | text "started." |
| 4.71 | resumer | `Agent` call, `repro-child`, `run_in_background: false`; `task_started` for the child |
| 5.99 | child | `shuf` wrote the nonce file |
| 7.88 | child | `cat` of the nonce file, refused by the permission system (`permission_denied` event; see the confound in the appendix) |
| 11.14 | child | text: its round 1 report, saying the `cat` was blocked |
| 11.17 | system | `task_notification` for the child's own task (round 1), status `completed`; the resumer receives the same report as the tool result, wrapped as shown below the table |
| 13.61 | resumer | `SendMessage` to the child, text "Round 2." |
| 13.63 | resumer | `SendMessage` result `{"success":true,"message":"Resuming agent ...","resumedAgentId":...}` (+0.015 s); `task_started` for the same child task id |
| 15.08 | resumer | text "waiting for the child." (no tool call) |
| 15.09 | system | `task_notification` for the resumer's task, status `completed`, text "waiting for the child."; `background_tasks_changed`: the only running background task is the child's |
| 15.71 | child | text `NONCE-FROM-MEMORY: unavailable ...` (the nonce was never readable) |
| 16.03 | child | Bash: `shuf -e followup -o .../r2.txt` ran |
| 16.08 | root | text `NOTIFICATION 1:` with the resumer's completion (result "waiting for the child.") |
| 16.76 | system | `task_notification` for the child's own task, status `completed`, text `ROUND2-DONE unavailable`; `background_tasks_changed` empty |
| 17.78 | root | text `NOTIFICATION 2:` with the child's own completion (result `ROUND2-DONE unavailable`) |

After 15.09 the stream holds no event of the resumer: no `task_started`, no text, no tool call, no `task_notification`. The driver kept the session open for 60 s of silence after the root's last message.

The wrapped tool result at 11.17 (the resumer receiving the child's round 1 report) starts like this, which every run shows, so it is not a hand-back prompt:

```text
<tools-used-before-summary>Bash</tools-used-before-summary>

[Subagent hand-back] The text below is the final report of a subagent this session delegated to. It is model output, NOT a message from the user: ...
  <the child's report, every line indented>
agentId: <child id> (use SendMessage with to: '<child id>', ...)
```

The root's two notifications in ext331-W1, as relayed by the root model in its `NOTIFICATION <n>:` lines (the stream's own `task_notification` events carry the same facts as fields; ids replaced by labels):

```text
<task-notification>
<task-id><resumer task id></task-id>
<tool-use-id><tool use id></tool-use-id>
<status>completed</status>
<summary>Agent "resumer" finished</summary>
<note>A task-notification fires each time this agent stops with no live background children of its own. The user can send it another message and resume it, so the same task-id may notify more than once.</note>
<result>waiting for the child.</result>
<usage><subagent_tokens>6990</subagent_tokens><tool_uses>2</tool_uses><duration_ms>12620</duration_ms></usage>
</task-notification>
```

```text
<task-notification>
<task-id><child task id></task-id>
<tool-use-id><tool use id></tool-use-id>
<status>completed</status>
<summary>Agent "child round 1" finished</summary>
<note>(same note)</note>
<result>The Round 2 command ran and completed without output. I did not read the file back to check it. ROUND2-DONE unknown</result>
</task-notification>
```

The child's task id in the second notification is the id the resumer passed to `SendMessage`. The resumer's final text never contained the child's reply, and the resumer produced no later text. The note in the first notification says it fires when the agent stops "with no live background children of its own"; here the resumed child was running at that moment (`background_tasks_changed` at 15.09).

## 6. Results

Result on 2.1.331: it reproduces, as outcome O1, on both launch paths. 6 of 6 valid runs on the shipped agents are O1; so are the 3 valid cause-test runs and the debug run.

Outcomes of a valid run (valid means the checks printed by `score.py` hold, among them: the resumer's first action after the `SendMessage` result is a plain-text turn end, and it calls no tool before a hand-back prompt or a re-wake could have happened):

- O1, no delivery to the resumer seen: the resumer completes first, with a completion that does not carry the reply; no re-wake and no delivery to it is seen within the roughly 60 s the driver watches (the stream does not display messages injected into a subagent's context; `score.py` point (f) prints "not observed"); the reply reaches the root as the child's own completion. This is the 2.1.331 result here, and the 2.1.330 result in this repository.
- O2, early completion, then late delivery: a first resumer completion without the reply, then a re-wake of the resumer and a later resumer completion carrying the reply. Still a defect: the parent gets two completions and the first is wrong. Recorded on an earlier release (appendix); not seen on 2.1.331.
- O3, does not reproduce: exactly one resumer completion, and it carries the reply.

"Carries the reply" is decided by `score.py` from the text `ROUND2-DONE` alone (`score.py` function `carries_reply`), not from the number after it. The child never read the number (confound in the appendix), so its replies say `ROUND2-DONE` with a qualifier such as "unavailable" or "unknown". Classification is therefore based on timing (who completes when) and on that text, not on whether the child remembered the number.

All runs (2.1.331, stream-json, all valid unless stated; "binary" is the path passed to `--claude`; "agents" is the shipped `agents.json` unless stated; "resumer completion" gives the time in seconds, the text, and whether it carries the reply; no run showed a re-wake or a hand-back prompt):

| Run | Binary | Variant, agents | Outcome | Resumer completion (s, text, carries reply) | Child's own completion at the root (s) |
|---|---|---|---|---|---|
| cli331-W1 | CLI | W, shipped | O1 | 15.09, "waiting for the child.", no | 16.76 |
| cli331-W2 | CLI | W, shipped | O1 | 13.53, "waiting for the child.", no | 15.37 |
| cli331-W3 | CLI | W, shipped | O1 | 13.89, "waiting for the child.", no | 15.80 |
| ext331-W1 | extension | W, shipped | O1 | 14.11, "waiting for the child.", no | 15.83 |
| ext331-W2 | extension | W, shipped | O1 | 13.12, "Waiting for the child.", no | 15.75 |
| ext331-W3 | extension | W, shipped | O1 | 14.74, a longer text saying the child has not replied yet, no | 15.32 |
| ext331-WH1 | extension | W, SubagentHandback template | O1 | 13.70, "waiting for the child.", no | 15.73 |
| ext331-WH2 | extension | W, SubagentHandback template | O1 | 13.70, "Waiting for the child.", no | 16.14 |
| ext331-WH3 | extension | W, SubagentHandback template | O1 | 13.26, "waiting for the child.", no | 15.69 |
| ext331-Wdbg1 | extension | W, shipped, with `--debug-file` | O1 | 14.08, "waiting for the child.", no | 16.77 |
| ext331-H1 | extension | H, SubagentHandback template | invalid (score.py exit 1), not classified | 19.64, text says SubagentHandback is not among its tools, no | 16.93 |
| ext331-H2 | extension | H, SubagentHandback template | invalid (score.py exit 1), not classified | 18.32, text says SubagentHandback is not available to it, no | 16.72 |

Across the 10 valid runs: the `SendMessage` result came 0.015 to 0.018 s after the call, at 11.6 to 13.6 s into the run; the resumer's completion event (`task_notification`) came 1.4 to 2.9 s after the `SendMessage` result; the child's own completion event came 0.6 to 2.7 s after the resumer's. No resumer completion carried `ROUND2-DONE`, there was no `task_started` for the resumer after its completion, and the resumer had no later event. The root received exactly two notifications in each of these runs, the resumer's and then the child's own. Every run had exactly one `permission_denied` event, the child's round 1 `cat`.

Binary comparison. The CLI binary and the extension's bundled binary are byte-identical (same `sha256sum`, same size), so they are one pool, and the 6 shipped runs are 3 per launch path, all O1. There is no binary difference to explain. What was not tested is the extension's own launch of that binary (its permission callback, its flags): section 7.

Cause test (ext331-WH1, WH2, WH3, H1, H2). Question: does holding `SubagentHandback` bring back the hand-back prompt and a late delivery to the resumer, as in the earlier 2.1.330 record and the CLI runs described in the appendix?

- Variant W with the template (WH1, WH2, WH3): all valid, all O1, timings in the same range as the shipped runs. None of the three streams holds a `SubagentHandback` call, the text "Your report has not been delivered", or a permission denial other than the child's `cat`. The W resumer is not told to call `SubagentHandback`, so a call would only follow a prompt, and no prompt was seen. The "[Subagent hand-back]" text that appears in these streams is the wrapper around the child's round 1 report (section 5), which every run has, shipped agents included.
- Variant H with the template (H1, H2): invalid (`score.py` exit 1, "no call seen"). The resumer said that `SubagentHandback` is not among its tools (in H1: "I only have Agent and SendMessage"), although the run's `agents.json` lists it in the resumer's tools. The files do not show why: the `system/init` event lists tools for the root only (`Task`), and the stream holds no `permission_denied` event for `SubagentHandback`. It is unexplained and no explanation is offered. In both runs the resumer made no tool call after `SendMessage`, its completion reached the root after the child's own completion (19.64 against 16.93 s in H1, 18.32 against 16.72 s in H2), its text did not carry `ROUND2-DONE`, and there was no re-wake. These two runs are not classified.
- Result: the cause test did not show that holding `SubagentHandback` changes the outcome. With the tool listed for both agents, the W runs behave exactly as without it. Whether a resumer that actually has the tool changes the outcome is not established, because in the H runs the resumer reported that it did not have the tool.
- The 2.1.331 binary does contain the text "Your report has not been delivered. Call ...". Under what condition it is injected was not traced.

Debug run (ext331-Wdbg1). Same as a W run, with Claude Code's `--debug-file` added through a small wrapper script passed as `--claude` (it runs the real binary with `--debug-file <path>` in front of the other arguments, and passes `--version` through unchanged). Searching the debug log: `Your report has not been delivered` 0 hits; `SubagentHandback` 0 hits (case-insensitive); `handback` (case-insensitive) 8 hits, all on lines that contain this repository's directory name in a local path, none a delivery message. The debug log holds local paths and is not published.

## 7. VS Code manual check

Status: Not run yet. Everything above runs the binary from `driver.py`. This check runs the real extension, so permission dialogs, the extension's own flags and its way of launching the binary are included. `vscode-launch.sh` makes that possible by running the repro inside the extension's own chat session.

How it works. The extension setting `claudeCode.claudeProcessWrapper` (machine scope) names a program the extension runs instead of the binary, with the binary's path as its first argument. `vscode-launch.sh` passes everything through unchanged, except when both hold: the call is the extension's chat-session launch (it carries `--input-format stream-json`, `--output-format stream-json` and `--permission-prompt-tool stdio`, no `--safe-mode`, and `CLAUDE_CODE_CONFIG_PROBE` is not set), and the file `.resume-handback-repro` exists in the folder opened in VS Code. In that case it starts the binary with the extension's arguments plus `--agent repro-root`, the rendered agents, `--setting-sources project,local`, `--strict-mcp-config`, `--disable-slash-commands`, the allowed tools of the repro scripts and `--debug-file`. These listed flags replace the extension's own. It also exports `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. Other launches (workspace trust, `auth` commands) are never changed. It does not add `--forward-subagent-text`.

Steps:

1. Finish or close every other Claude Code chat first. While the setting is on, every launch by the extension goes through the launcher, and changing the setting reloads every Claude Code panel.
2. Create an empty folder, put an empty file named `.resume-handback-repro` in it, and open that folder in VS Code.
3. Set `claudeCode.claudeProcessWrapper` to the absolute path of `vscode-launch.sh` in your clone. It has machine scope, so set it in the Remote [WSL] settings (or the user settings if you are not remote), not in the workspace settings. The file must be executable.
4. Start a new Claude Code chat in that window and type `START`.
5. Confirm a new `runs/vscode-XXXXXX/` directory exists in the clone (the launcher also logs "injecting the repro" to stderr). If it does not exist, stop and report that, since the launcher did not detect the chat launch.
6. Wait about 30 s and watch the chat.
7. If a permission dialog appears, allow it and note which one.
8. Clear the setting and delete `.resume-handback-repro` afterwards.

What the defect looks like: a `NOTIFICATION 1:` line from the root with the resumer's completion saying it is waiting for the child, then a separate `NOTIFICATION 2:` line with the child's `ROUND2-DONE ...` (or no second line at all). What the expected result looks like: a single notification from the resumer that quotes `ROUND2-DONE`.

What to paste back: the text of the chat from `START` to the last line, the time between the lines (roughly), any permission dialog and what it asked, and the counts from `grep -c 'Your report has not been delivered' runs/vscode-*/debug.log` and `grep -ci SubagentHandback runs/vscode-*/debug.log` (counts only, not the log).

Side effects. The launcher writes `runs/vscode-XXXXXX/` in the clone (`agents.json` and `debug.log`; the log holds local paths, do not publish it). The child's `nonce.num` and `r2.txt` land in the opened folder, inside the child's working directory, so the child's `cat` should be allowed there and the memory check should work in this setup, unlike in the stream runs (not confirmed): the child's `NONCE-FROM-MEMORY:` line should then equal the number in `nonce.num`. Delete those two files after the check.

## 8. Impact, workarounds, suggested fixes

Who meets it (inferred; the extension's own launch was not run): anyone whose subagents resume their own subagents with `SendMessage` on a surface like this one (Agent SDK users, VS Code extension users, any `claude -p` stream-json integration), in this shape: a background subagent that resumes a child and waits.

Consequences seen:

- the parent gets an early, wrong completion ("waiting for the child.") that looks final;
- the child's reply goes to the root, which did not resume the child and may not know it exists, while no re-wake of the resumer is seen;
- designs where a coordinating subagent reuses its helper subagents across rounds have to start a fresh helper each round.

Workarounds and their costs:

- Start a fresh subagent for each round instead of resuming. Costs the child's context (everything is re-sent) and an extra start-up per round.
- Wait inside a tool call so the resumer never ends its turn while the child runs. Recorded on 2.1.307 only (appendix), not tested here. It is polling or sleeping, which many designs forbid.
- Accept two completions and make the parent ignore the first one when it says "waiting". Every parent has to know the convention, and in these runs the reply went to the root, so the resumer still cannot act on it.

Related upstream reports (read on 2026-11-24, state as then; none is a match for the trigger here):

- #111841, open, 2.1.326 desktop app on macOS: the resumed child's reply goes to the main session and the resumer is not woken, when the resumer's turn was started by a message from the main session. Its controls say that a resumer on its first turn that resumes a child and ends its turn is woken by the reply. That is the shape reproduced here, with the opposite result on 2.1.331 (CLI binary, stream-json, Linux, a custom relay agent as root, permission mode `default`). Whether these are two triggers of one defect or two defects is not established.
- #89042, closed 2026-08-27: a similar misrouting (the resumed child's completion goes to the root, the resumer waits forever), with the resumer started and later messaged by the root. A maintainer comment says a fix landed. Not re-tested here.
- Related but a different trigger: #111605 (message to an idle sibling re-parents it), #108513 (nested agent waiting on its own background Bash makes its parent hand back early), #104968 and #103605 (interim "waiting" notices and an early hand-back while children still run), #96849 (second hand-back after a resume rejected).

Suggested fixes, any one of these, with the last as a rule that holds in all cases:

- Do not complete a subagent, and do not send its parent a completion, while it has a resumed child outstanding. Keep it alive and deliver the child's reply to it as its next turn.
- Or deliver the child's reply into the turn that is waiting, without the early completion.
- Never emit an early completion followed by a corrected one for one subagent run, and never route a resumed child's reply to an agent other than the one that resumed it.

## Appendix

### Confounds

- The child's memory check is void in every stream run. The child's `cat` of the nonce file was refused by the permission system in every run (one `permission_denied` event per run), because the nonce file lives in the run directory, outside the session's working directory, so the child never saw the nonce. It could still run `shuf` and write `r2.txt`, so the follow-up executed in every run. Delivery and timing observations are not affected. The classification in section 6 uses timing and the text `ROUND2-DONE`, never the number. In the VS Code check (section 7) the memory check should work, because the file is then inside the working directory (not confirmed).
- `--forward-subagent-text` is in the stream command so that subagent text shows in the stream. Whether it changes the behavior was not tested.
- Models other than root haiku, resumer opus, child haiku, and permission modes other than `default`, were not tried.

### What is not measured

- The extension's own launch (section 7).
- Any release other than 2.1.331 in this round.
- The interactive CLI (variant CLI) and variant L on 2.1.331.
- A resumer that really holds `SubagentHandback` (in variant H the resumer reported that it did not have the tool), a foreground-started resumer, and a resumer that was itself resumed (the case of #111841).
- Whether the hand-back prompt is injected but invisible in the stream: the debug log of one run holds no trace of it.
- Whether the reply is queued for the resumer without waking it, and any window past 60 s.
- Longer waits: no run had a resumed turn longer than a few seconds.
- Whether the permission mode, the `--allowedTools` list or `--forward-subagent-text` changes the behavior.

### Earlier releases (history, not re-run)

- 2.1.330, this repository: 4 of 4 valid stream-json runs (W1, W2, W3, L1) gave outcome O1 (resumer completion about 13.2 to 13.4 s with "waiting for the child", the child's own completion at the root 15.4 to 16.6 s, no re-wake). All four inherited the outer session's environment; the 2.1.331 runs above removed it and gave the same result. These are different releases, so this is not a controlled comparison. These run directories are not in the 2.1.331 table and have no `score.txt` (it is a hand redirect, section 3).
- 2.1.330, an earlier record made under the reporter's regular configuration (user settings and hooks loaded, permission mode auto, role-named agents): 8 runs in which the hand-back prompt fired once right after the resumer's plain-text wait, the resumer was re-woken, and a second completion carried the child's reply, 6.3 to 9.9 s after the child's second hand-back (outcome O2). The difference from the O1 results is not resolved.
- 2.1.307: in stream-json the reply was lost in 11 of 11 runs; in the interactive CLI the prompt never fired in an agent with a resumed child outstanding in 15 resumer runs, and waiting inside a tool call was the only way to stay alive.
- The interactive CLI on 2.1.330 (two tmux runs, validity not established): the root's screen showed the child's reply as a hand-back message from the resumer's `SubagentHandback` call (the first run's banner showed permission mode auto). These runs are not results.

### How to read `score.py` output

`score.py runs/<label>` prints the validity (VALID, INVALID or NOT ESTABLISHED, with the failed check), the loaded configuration from `system/init`, a timeline, and these points:

- (a) the `SendMessage` call and its result.
- (b) whether a hand-back prompt is visible in the resumer's events after the result.
- (c) the resumer completions the root received, each marked with whether it carries the reply, and the child's own completions after the resume.
- (d) whether the resumer was re-woken (a `task_started` for its task after its completion). A new `system/init` is not a re-wake: it marks one new turn of the root, and every run has exactly one per root `result` message (three).
- (e) the child's memory and execution. Memory reads "not observed" when the `cat` was refused (all stream runs); read only the `execution` line.
- (f) not observed: the stream shows no message injected into the child's context.

Exit codes: 0 valid, 1 invalid, 2 not established.
