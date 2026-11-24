#!/usr/bin/env python3
"""Score one run of the subagent-resume hand-back repro.

Run with `python3 -I score.py <run dir>`.

Prints, in this order: the run's validity (VALID, INVALID or NOT ESTABLISHED, with the
checks behind it), what the session loaded, the timeline, and points (a) to (f):

  (a) the SendMessage result: its time and text
  (b) a hand-back prompt injected into the resumer while the child's resumed turn was outstanding
  (c) the resumer completions the root received, and whether each carries the child's reply
  (d) the re-wake: a task_started for the same task, and the delay to the completion with the reply
  (e) the child's memory (nonce stated before any tool call) and execution (r2.txt written)
  (f) the child-side carrier of the resume (the wrapper text the child received)

A point the stream or the run files cannot show is printed as "not observed", never as pass
or fail. Times are seconds since the driver started the session (receipt times).
Exit code: 0 valid, 1 invalid, 2 not established.
"""

import json
import os
import re
import sys

ENFORCE_RE = re.compile(r"Your report has not been delivered|handback-send-enforce")
SKIP_SYSTEM = {"hook_started", "hook_response", "status", "compact_boundary"}


def short(text, n=160):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[: n - 3] + "..."


def blocks(msg):
    content = (msg.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def result_text(block):
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def fmt(rel):
    return "n/a" if rel is None else f"{rel:9.3f}s"


def read_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def load_stream(path):
    recs = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
    return recs


class Run:
    def __init__(self, run_dir):
        self.dir = run_dir
        meta_text = read_file(os.path.join(run_dir, "meta.json"))
        self.meta = json.loads(meta_text) if meta_text else {}
        self.variant = self.meta.get("variant")
        nonce_text = read_file(os.path.join(run_dir, "nonce.num"))
        self.nonce = nonce_text.strip() if nonce_text and re.fullmatch(r"\d{6}", nonce_text.strip()) else None
        self.recs = load_stream(os.path.join(run_dir, "stream.jsonl"))
        self.events = []
        self.inits = []
        self.tool_uses = {}
        self.role_by_parent = {}
        self.task_role = {}
        self.task_started = []
        self.sdk_notifications = []
        self.denied_tool_uses = set()
        self.xml_notifications = []
        self.root_resumer_use = None
        self.child_spawn_use = None
        self.results = 0
        self.walk()

    # ---------- parsing ----------
    def who(self, parent, kinds_hint=None):
        if parent is None:
            return "root"
        return self.role_by_parent.get(parent, "unknown")

    def add(self, rec, who, kind, text, **extra):
        ev = {"rel": rec.get("rel"), "t": rec.get("t"), "who": who, "kind": kind, "text": text}
        ev.update(extra)
        self.events.append(ev)
        return ev

    def walk(self):
        for i, rec in enumerate(self.recs):
            if "sent" in rec:
                self.add(rec, "driver", "sent", "start message sent to claude stdin")
                continue
            msg = rec.get("msg")
            if not isinstance(msg, dict):
                if "raw" in rec:
                    self.add(rec, "?", "raw", rec["raw"])
                continue
            mtype = msg.get("type")
            parent = msg.get("parent_tool_use_id")
            if mtype == "system":
                self.on_system(rec, msg)
            elif mtype == "assistant":
                self.on_assistant(rec, msg, parent)
            elif mtype == "user":
                self.on_user(rec, msg, parent)
            elif mtype == "result":
                self.results += 1
                self.add(rec, "root", "result", f"result subtype={msg.get('subtype')} is_error={msg.get('is_error')}: "
                         f"{short(msg.get('result', ''), 200)}")

    def on_system(self, rec, msg):
        sub = msg.get("subtype")
        if sub == "init":
            self.inits.append((rec, msg))
            self.add(rec, "system", "init", f"init #{len(self.inits)} version={msg.get('claude_code_version')}")
        elif sub == "task_started":
            tid = msg.get("task_id")
            tu = msg.get("tool_use_id")
            desc = msg.get("description", "")
            role = None
            if tu is not None and tu == (self.root_resumer_use or {}).get("id"):
                role = "resumer"
            elif tu is not None and tu == (self.child_spawn_use or {}).get("id"):
                role = "child"
            elif "resumer" in str(desc).lower():
                role = "resumer"
            elif "child" in str(desc).lower():
                role = "child"
            if role and tid:
                self.task_role.setdefault(tid, role)
            self.task_started.append({"rel": rec.get("rel"), "task_id": tid, "role": self.task_role.get(tid),
                                      "tool_use_id": tu})
            self.add(rec, "system", "task_started", f"task_started task={short(tid, 24)} role={self.task_role.get(tid)} "
                     f"desc={short(desc, 60)}")
        elif sub == "task_notification":
            tid = msg.get("task_id")
            role = self.task_role.get(tid) or self.role_by_parent.get(msg.get("tool_use_id"))
            body = json.dumps(msg, ensure_ascii=False)
            note = {"rel": rec.get("rel"), "task_id": tid, "role": role, "status": msg.get("status"),
                    "body": body, "summary": msg.get("summary") or msg.get("result") or msg.get("handback_report")
                    or ""}
            self.sdk_notifications.append(note)
            self.add(rec, "system", "task_notification", f"task_notification task={short(tid, 24)} role={role} "
                     f"status={msg.get('status')}: {short(note['summary'], 200)}")
        elif sub == "permission_denied":
            if msg.get("tool_use_id"):
                self.denied_tool_uses.add(msg.get("tool_use_id"))
            self.add(rec, "system", sub, short(json.dumps(msg, ensure_ascii=False), 200))
        elif sub not in SKIP_SYSTEM:
            self.add(rec, "system", sub or "system", short(json.dumps(msg, ensure_ascii=False), 200))

    def on_assistant(self, rec, msg, parent):
        who = self.who(parent)
        bl = blocks(msg)
        if who == "unknown":
            guess = None
            for b in bl:
                if b.get("type") == "tool_use" and b.get("name") == "Bash":
                    guess = "child"
                if b.get("type") == "text" and re.search(r"NONCE-FROM-MEMORY|ROUND2-DONE|round 1 done", b.get("text", "")):
                    guess = "child"
            if guess:
                self.role_by_parent[parent] = guess
                who = guess
        for b in bl:
            if b.get("type") == "text":
                self.add(rec, who, "text", b.get("text", ""), parent=parent)
            elif b.get("type") == "tool_use":
                name = b.get("name")
                tu = {"id": b.get("id"), "name": name, "input": b.get("input") or {}, "who": who,
                      "rel": rec.get("rel"), "t": rec.get("t")}
                self.tool_uses[tu["id"]] = tu
                if name in ("Agent", "Task"):
                    st = (tu["input"].get("subagent_type") or "")
                    if st == "repro-resumer":
                        self.role_by_parent[tu["id"]] = "resumer"
                        if self.root_resumer_use is None:
                            self.root_resumer_use = tu
                    elif st == "repro-child":
                        self.role_by_parent[tu["id"]] = "child"
                        if self.child_spawn_use is None:
                            self.child_spawn_use = tu
                elif name == "SendMessage" and who == "resumer":
                    self.role_by_parent.setdefault(tu["id"], "child")
                self.add(rec, who, "tool_use", f"{name} {short(json.dumps(tu['input'], ensure_ascii=False), 150)}",
                         tool=name, tool_id=tu["id"], parent=parent)
            elif b.get("type") == "thinking":
                continue

    def on_user(self, rec, msg, parent):
        who = self.who(parent)
        for b in blocks(msg):
            if b.get("type") == "tool_result":
                tu = self.tool_uses.get(b.get("tool_use_id"), {})
                self.add(rec, who, "tool_result", result_text(b), tool=tu.get("name"), tool_id=b.get("tool_use_id"),
                         is_error=bool(b.get("is_error")), parent=parent)
            elif b.get("type") == "text":
                text = b.get("text", "")
                self.add(rec, who, "user_text", text, parent=parent)
                if who == "root" and "<task-notification>" in text:
                    def tag(name):
                        m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
                        return m.group(1).strip() if m else None
                    tid = tag("task-id")
                    self.xml_notifications.append({
                        "rel": rec.get("rel"), "task_id": tid, "status": tag("status"),
                        "role": self.task_role.get(tid) or self.role_by_parent.get(tag("tool-use-id")),
                        "body": text, "summary": tag("summary") or tag("result") or text})

    # ---------- helpers ----------
    def ev(self, **kw):
        return [e for e in self.events if all(e.get(k) == v for k, v in kw.items())]

    def carries_reply(self, body):
        """The reply is the child's round 2 final text: ROUND2-DONE, then the nonce or "unknown"
        when the child could not read it. None when there is no text to judge."""
        if not body or not body.strip():
            return None
        return re.search(r"ROUND2-DONE", body) is not None

    def first_index(self, pred):
        for i, e in enumerate(self.events):
            if pred(e):
                return i
        return None


def analyze(run):
    r = run
    A = {}
    sm_use = next((e for e in r.events if e["kind"] == "tool_use" and e.get("tool") == "SendMessage"
                   and e["who"] == "resumer"), None)
    sm_res = None
    if sm_use:
        sm_res = next((e for e in r.events if e["kind"] == "tool_result" and e.get("tool_id") == sm_use["tool_id"]), None)
    A["sm_use"], A["sm_res"] = sm_use, sm_res
    A["sm_idx"] = r.events.index(sm_res) if sm_res else None
    child_ret = None
    if r.child_spawn_use:
        child_ret = next((e for e in r.events if e["kind"] == "tool_result" and e.get("tool_id") == r.child_spawn_use["id"]),
                         None)
    A["child_ret"] = child_ret

    after = r.events[A["sm_idx"] + 1:] if A["sm_idx"] is not None else []

    # enforce-style prompts (visible user/system text)
    prompts = [e for e in r.events if e["kind"] in ("user_text", "system") and ENFORCE_RE.search(e["text"])]
    A["prompts_resumer"] = [e for e in prompts if e["who"] == "resumer" and e in after]
    A["prompts_other"] = [e for e in prompts if e not in A["prompts_resumer"]]

    # re-wake markers
    resumer_tasks = {t for t, role in r.task_role.items() if role == "resumer"}
    first_note_rel = None
    notes = [n for n in r.sdk_notifications if n["role"] == "resumer"]
    if notes:
        first_note_rel = notes[0]["rel"]
    rewakes = [t for t in r.task_started if t["task_id"] in resumer_tasks and first_note_rel is not None
               and t["rel"] is not None and t["rel"] > first_note_rel]
    inits_after_first = [rc for rc, _ in r.inits[1:]]
    A["rewakes"] = rewakes
    A["inits_extra"] = inits_after_first
    # a re-wake is a task_started for the resumer's task after its completion; a new system/init
    # is the root's own new turn (one per notification) and is not counted.
    cands = [t["rel"] for t in rewakes]
    A["rewake_rel"] = min(cands) if cands else None

    # child's second hand-back
    second = []
    sm_rel = A["sm_res"]["rel"] if A["sm_res"] else None
    for e in after:
        if e["who"] == "child" and e["kind"] == "tool_use" and e.get("tool") == "SubagentHandback":
            second.append((e["rel"], "child SubagentHandback tool_use"))
        elif e["who"] == "child" and e["kind"] == "text" and "ROUND2-DONE" in e["text"]:
            second.append((e["rel"], "child text carrying ROUND2-DONE"))
        elif e["kind"] == "system" and e.get("text", "").startswith("task_notification") and "role=child" in e["text"]:
            second.append((e["rel"], "child task_notification"))
    A["child_second"] = min(second) if second else (None, None)
    return A


def validity(run, A):
    r = run
    checks = []

    def chk(ok, msg):
        checks.append((ok, msg))

    variant = r.variant
    if variant not in ("W", "L", "H"):
        return "NOT ESTABLISHED", checks + [(None, f"unknown variant {variant!r} (meta.json missing or not a stream run)")]

    chk(bool(r.inits), "stream has a system/init line")
    ru = r.root_resumer_use
    chk(bool(ru) and ru["input"].get("run_in_background") is True and ru["who"] == "root",
        "the root spawned repro-resumer with run_in_background true")
    cs = r.child_spawn_use
    chk(bool(cs) and cs["who"] == "resumer" and cs["input"].get("run_in_background") is not True,
        "the resumer spawned repro-child in the foreground")
    chk(r.nonce is not None, "round 1 ran: nonce.num holds a 6-digit number")
    sm_uses = [e for e in r.events if e["kind"] == "tool_use" and e.get("tool") == "SendMessage" and e["who"] == "resumer"]
    chk(len(sm_uses) == 1, f"the resumer called SendMessage exactly once (saw {len(sm_uses)})")
    sm_res = A["sm_res"]
    if not sm_res:
        chk(False, "a SendMessage result was received")
        return "INVALID", checks
    chk(not sm_res.get("is_error") and bool(re.search(r"success|Resuming", sm_res["text"], re.I)),
        "the SendMessage result reports a successful resume")
    if A["child_ret"]:
        chk(A["child_ret"]["rel"] <= A["sm_use"]["rel"], "SendMessage was sent after the child's first return")
    else:
        chk(False, "the child's first return (Agent result) is in the stream")

    after = r.events[A["sm_idx"] + 1:]
    # resumer assistant activity after the SendMessage result, split at the boundary
    B_rel = None
    cands = [e["rel"] for e in A["prompts_resumer"]]
    if A["rewake_rel"] is not None:
        cands.append(A["rewake_rel"])
    if cands:
        B_rel = min(cands)
    res_calls = [e for e in after if e["who"] == "resumer" and e["kind"] == "tool_use"]
    res_text_turn = [e for e in after if e["who"] == "resumer" and e["kind"] in ("text", "tool_use")]
    first_action = res_text_turn[0] if res_text_turn else None

    state = None
    if variant == "H":
        if not res_calls:
            chk(False, "the resumer's next call after the SendMessage result is SubagentHandback (no call seen)")
        else:
            ok = res_calls[0].get("tool") == "SubagentHandback"
            chk(ok, f"the resumer's next call after the SendMessage result is SubagentHandback (saw {res_calls[0].get('tool')})")
    else:
        if first_action is None:
            chk(None, "the resumer's first action after the SendMessage result is visible in the stream (none seen)")
            state = "NOT ESTABLISHED"
        else:
            chk(first_action["kind"] == "text",
                f"the resumer's first action after the SendMessage result is a plain-text turn end "
                f"(saw {first_action['kind']}{' ' + str(first_action.get('tool')) if first_action['kind'] == 'tool_use' else ''})")
            before_b = [e for e in res_calls if B_rel is None or e["rel"] < B_rel]
            if B_rel is not None:
                chk(not before_b, "the resumer called no tool before the hand-back prompt or the re-wake "
                    f"(boundary at {B_rel:.3f}s; {len(before_b)} call(s) before it)")
            else:
                hb_only = res_calls and all(e.get("tool") == "SubagentHandback" for e in res_calls)
                if not res_calls:
                    chk(True, "the resumer called no tool after the SendMessage result")
                elif hb_only:
                    chk(None, "the resumer called SubagentHandback after its plain-text turn end, but no prompt or "
                        "re-wake is visible in the stream: cannot tell a voluntary hand-back from a prompted one")
                    state = "NOT ESTABLISHED"
                else:
                    chk(False, "the resumer called a tool (" + ", ".join(sorted({e.get('tool') for e in res_calls}))
                        + ") with no hand-back prompt or re-wake visible before it")
    if any(ok is False for ok, _ in checks):
        return "INVALID", checks
    if state or any(ok is None for ok, _ in checks):
        return "NOT ESTABLISHED", checks
    return "VALID", checks


def print_loaded(run):
    print("== LOADED CONFIGURATION (system/init of the first session start)")
    if not run.inits:
        print("  not observed (no init line)")
        return
    _, init = run.inits[0]
    print(f"  claude_code_version: {init.get('claude_code_version')}   permissionMode: {init.get('permissionMode')}   "
          f"model: {init.get('model')}")
    print(f"  tools: {', '.join(map(str, init.get('tools', [])))}")
    agents = init.get("agents", [])
    print(f"  agents: {', '.join(map(str, agents))}")
    foreign = [a for a in agents if not str(a).startswith("repro-")]
    print(f"  plugins: {json.dumps(init.get('plugins', []), ensure_ascii=False)}")
    print(f"  mcp_servers: {json.dumps(init.get('mcp_servers', []), ensure_ascii=False)}")
    for key in ("skills", "slash_commands"):
        if key in init:
            print(f"  {key}: {len(init[key])} entries")
    print(f"  residue to judge (agents not named repro-*; built-ins are expected): {', '.join(map(str, foreign)) or 'none'}")
    if len(run.inits) > 1:
        print(f"  inits in the stream: {len(run.inits)} (the root's own turns, one per notification; not a re-wake)")


def print_timeline(run):
    print("== TIMELINE (receipt time, who, event)")
    for e in run.events:
        txt = short(e["text"], 170)
        print(f"  {fmt(e['rel'])}  {e['who']:<8} {e['kind']:<17} {txt}")


def print_points(run, A):
    r = run
    print("== POINTS")
    # (a)
    if A["sm_res"]:
        print(f"(a) SendMessage: sent {fmt(A['sm_use']['rel'])}, result {fmt(A['sm_res']['rel'])} "
              f"(+{A['sm_res']['rel'] - A['sm_use']['rel']:.3f}s): {short(A['sm_res']['text'], 300)}")
    else:
        print("(a) SendMessage result: not observed")
    # (b)
    sec_rel, sec_src = A["child_second"]
    if A["prompts_resumer"]:
        for e in A["prompts_resumer"]:
            rel = ""
            outstanding = "unknown (child's second hand-back not observed)"
            if sec_rel is not None:
                rel = f", {e['rel'] - sec_rel:+.3f}s relative to the child's second hand-back ({sec_src} at {fmt(sec_rel)})"
                outstanding = "yes" if e["rel"] < sec_rel else "no (it came after the child's second hand-back)"
            print(f"(b) hand-back prompt injected into the resumer at {fmt(e['rel'])}{rel}; "
                  f"child's resumed turn outstanding: {outstanding}")
            print(f"    verbatim: {e['text']!r}")
    elif A["sm_res"]:
        n = sum(1 for e in r.events[A["sm_idx"] + 1:] if e["who"] == "resumer")
        if n:
            print(f"(b) no hand-back prompt seen in {n} resumer stream event(s) after the SendMessage result")
        else:
            print("(b) not observed (no resumer event after the SendMessage result)")
    else:
        print("(b) not observed")
    for e in A["prompts_other"]:
        print(f"    note: a hand-back prompt also appears for {e['who']} at {fmt(e['rel'])}: {e['text']!r}")
    # (c)
    print("(c) resumer completions the root received:")
    for label, notes in (("SDK task_notification events", [n for n in r.sdk_notifications if n["role"] in ("resumer", None)]),
                         ("model-visible <task-notification> messages", [n for n in r.xml_notifications
                                                                         if n["role"] in ("resumer", None)])):
        print(f"    {label}: {len(notes)}")
        for n in notes:
            cr = r.carries_reply(n["body"])
            crs = "not observed (no nonce)" if cr is None else ("yes" if cr else "no")
            print(f"      {fmt(n['rel'])} status={n['status']} carries the child's reply: {crs}: {short(n['summary'], 160)}")
    sm_rel_c = A["sm_res"]["rel"] if A["sm_res"] else None
    child_notes = [n for n in r.sdk_notifications if n["role"] == "child"
                   and (sm_rel_c is None or (n["rel"] is not None and n["rel"] > sm_rel_c))]
    print(f"    task_notification events for the child's own task after the resume: {len(child_notes)}")
    for n in child_notes:
        cr = r.carries_reply(n["summary"])
        crs = "not observed" if cr is None else ("yes" if cr else "no")
        print(f"      {fmt(n['rel'])} status={n['status']} carries the child's reply: {crs}: {short(n['summary'], 160)}")
    # (d)
    if not r.sdk_notifications and not r.inits:
        print("(d) not observed")
    else:
        if A["rewakes"]:
            for t in A["rewakes"]:
                print(f"(d) re-wake: task_started for the same task at {fmt(t['rel'])}")
        elif A["rewake_rel"] is not None:
            print(f"(d) re-wake: task_started for the same task at {fmt(A['rewake_rel'])}")
        else:
            print("(d) no re-wake seen (no task_started for the resumer task after its first completion; "
                  "a new system/init is the root's own turn and is not counted)")
        reply_notes = [n for n in r.sdk_notifications if n["role"] in ("resumer", None) and r.carries_reply(n["body"])]
        if reply_notes and sec_rel is not None:
            print(f"    delay from the child's second hand-back ({sec_src}, {fmt(sec_rel)}) to the completion carrying the "
                  f"reply ({fmt(reply_notes[0]['rel'])}): {reply_notes[0]['rel'] - sec_rel:.3f}s")
        elif reply_notes:
            print(f"    completion carrying the reply at {fmt(reply_notes[0]['rel'])}; delay not observed "
                  "(child's second hand-back not visible)")
        else:
            print("    no resumer completion carried the child's reply (see (c) for the child's own completion)")
    # (e)
    print("(e) child memory and execution:")
    sm_rel = A["sm_res"]["rel"] if A["sm_res"] else None
    child_has_text = any(e["who"] == "child" and e["kind"] == "text" for e in r.events)
    mem = "not observed"
    if sm_rel is not None and r.nonce:
        stated = None
        for e in r.events[A["sm_idx"] + 1:]:
            if e["who"] != "child":
                continue
            if e["kind"] == "tool_use":
                if stated is None:
                    stated = False
                break
            if e["kind"] == "text":
                stated = r.nonce in e["text"]
                if stated:
                    break
        if stated is True:
            mem = f"nonce {r.nonce} stated in text before any tool call"
        elif stated is False:
            mem = "FAIL: the child's first output after the resume lacks the nonce or came after a tool call"
        elif child_has_text:
            mem = "not observed (no child output after the resume in the stream)"
        else:
            mem = "not observed (child text blocks are not in the stream)"
    elif not r.nonce:
        mem = "not observed (no nonce.num)"
    refused = [tu for tu in r.tool_uses.values() if tu["who"] == "child" and tu["name"] == "Bash"
               and tu["id"] in r.denied_tool_uses and re.search(r"\bcat\b", str(tu["input"].get("command", "")))]
    if refused and not mem.startswith("nonce") :
        mem = "not observed (the child's cat of nonce.num was refused by the permission system)"
    print(f"    memory: {mem}")
    r2 = os.path.join(r.dir, "r2.txt")
    content = read_file(r2)
    if content is None:
        print("    execution: r2.txt not written")
    else:
        mt = os.path.getmtime(r2)
        start = r.meta.get("start_epoch")
        when = f" at {mt - start:.3f}s" if start else ""
        print(f"    execution: r2.txt written{when}, content {content.strip()!r}"
              f"{'' if content.strip() == 'followup' else ' (expected followup)'}")
    # (f)
    carriers = [e for e in r.events[(A["sm_idx"] or 0) + 1:] if e["who"] == "child" and e["kind"] == "user_text"
                and re.search(r"sent a message|<agent-message", e["text"])]
    if carriers:
        for e in carriers[:2]:
            print(f"(f) child-side carrier at {fmt(e['rel'])}, verbatim:\n{e['text'][:1500]}")
    else:
        print("(f) not observed (the stream shows no message injected into the child's context)")


def score_stream(run):
    A = analyze(run)
    state, checks = validity(run, A)
    print(f"== VALIDITY: {state}  (variant {run.variant}, label {run.meta.get('label')})")
    for ok, msg in checks:
        print(f"  {'ok  ' if ok else ('FAIL' if ok is False else '??  ')} {msg}")
    m = run.meta
    if m:
        print(f"  run: elapsed {m.get('elapsed_seconds')}s, result messages {m.get('result_messages')}, "
              f"timed_out {m.get('timed_out')}, exit {m.get('exit_code')}, models {m.get('models')}")
    print_loaded(run)
    print_timeline(run)
    print_points(run, A)
    return {"VALID": 0, "INVALID": 1}.get(state, 2)


def score_cli(run_dir):
    meta_text = read_file(os.path.join(run_dir, "meta.json"))
    meta = json.loads(meta_text) if meta_text else {}
    print(f"== VALIDITY: NOT ESTABLISHED  (variant {meta.get('variant', 'CLI')}, label {meta.get('label')})")
    print("  the interactive CLI has no machine-readable stream: the resumer's calls cannot be checked from the files;")
    print("  judge validity by reading pane.log (what the root screen showed) and debug.log")
    nonce = (read_file(os.path.join(run_dir, "nonce.num")) or "").strip()
    ran = "yes" if re.fullmatch(r"\d{6}", nonce) else "no"
    print(f"  round 1 ran (nonce.num holds a 6-digit number): {ran}")
    print("== TIMELINE (from pane.log: first time each root line appeared on screen)")
    pane = read_file(os.path.join(run_dir, "pane.log")) or ""
    start = meta.get("start_epoch")
    seen = {}
    for m in re.finditer(r"^=== (\d+(?:\.\d+)?) ===\n(.*?)(?=^=== \d|\Z)", pane, re.S | re.M):
        ts, body = float(m.group(1)), m.group(2)
        for line in body.splitlines():
            mm = re.search(r"(NOTIFICATION \d+:.*|waiting for the child.*|started\.?$)", line)
            if mm and mm.group(1) not in seen:
                seen[mm.group(1)] = ts
    for text, ts in sorted(seen.items(), key=lambda kv: kv[1]):
        print(f"  {fmt(ts - start) if start else ts}  root screen: {short(text, 170)}")
    if not seen:
        print("  not observed")
    print("== POINTS")
    print("(a) not observed (no stream)")
    dbg = read_file(os.path.join(run_dir, "debug.log")) or ""
    hits = [l for l in dbg.splitlines() if ENFORCE_RE.search(l)]
    if hits:
        print(f"(b) hand-back prompt text found in debug.log ({len(hits)} line(s)); first: {short(hits[0], 300)}")
        print("    which agent it was injected into is not observed")
    elif dbg:
        print("(b) no hand-back prompt text in debug.log (the log may not record injected prompts)")
    else:
        print("(b) not observed (no debug.log)")
    n_notes = len([k for k in seen if k.startswith("NOTIFICATION")])
    print(f"(c) root screen shows {n_notes} NOTIFICATION line(s) (screen-derived, not the harness's own count)")
    print("(d) not observed (no stream)")
    r2 = os.path.join(run_dir, "r2.txt")
    c = read_file(r2)
    if c is None:
        print("(e) memory: not observed; execution: r2.txt not written")
    else:
        print(f"(e) memory: not observed (no child text in the files); execution: r2.txt written, content {c.strip()!r}")
    print("(f) not observed (no stream)")
    return 2


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(sys.argv) == 2 else 2
    run_dir = os.path.abspath(sys.argv[1])
    if os.path.exists(os.path.join(run_dir, "stream.jsonl")):
        return score_stream(Run(run_dir))
    if os.path.exists(os.path.join(run_dir, "pane.log")):
        return score_cli(run_dir)
    print(f"score.py: no stream.jsonl or pane.log in {run_dir}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
