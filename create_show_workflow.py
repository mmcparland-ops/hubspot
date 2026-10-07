#!/usr/bin/env python3
"""Create a contact-based HubSpot show-outreach workflow (Automation v4 API, beta).

Usage:
    HUBSPOT_TOKEN=... python3 create_show_workflow.py shows/usssa-2026-joe.json [--dry-run]

Rules this script follows:
  * It never hard-codes action type IDs or field formats. It reads existing
    workflows in the portal and copies the shape of a "send marketing email"
    action, a "delay until a date" action, and a list-membership enrollment
    trigger. If it can't find a template for one of these, it stops.
  * It stops if a workflow with the same name already exists.
  * It creates the workflow with isEnabled = false and never enables it.
  * Unenroll-on-meeting-booked goes in only if an existing workflow shows the
    goal format the API uses. Otherwise it's skipped and the script says so.

Pass --template-flow-id ID (repeatable) to say which workflows to copy from.
Pass --dry-run to print the payload without creating anything.
Standard library only.
"""
import argparse
import copy
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.hubapi.com"
PORTAL_ID = "5528322"
EDIT_URL = "https://app.hubspot.com/workflows/{portal}/platform/flow/{flow_id}/edit"
CONTACT_OBJECT_TYPE = "0-1"
MEETING_BOOKED_PROPERTY = "engagements_last_meeting_booked"
READ_ONLY_KEYS = {"id", "revisionId", "createdAt", "updatedAt", "uuid", "crmObjectCreationStatus"}


def die(msg):
    print(f"STOP: {msg}", file=sys.stderr)
    sys.exit(1)


def api(method, path, body=None):
    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        die("HUBSPOT_TOKEN is not set")
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        die(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')}")


def list_flows():
    flows, after = [], None
    while True:
        page = api("GET", "/automation/v4/flows" + (f"?after={after}" if after else ""))
        flows.extend(page.get("results", []))
        after = page.get("paging", {}).get("next", {}).get("after")
        if not after:
            return flows


# ---------- template discovery ----------

def walk(obj):
    """Yield every dict nested anywhere in obj."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def is_email_action(a):
    return "content_id" in a.get("fields", {})


def is_static_date_delay(a):
    f = a.get("fields", {})
    return (isinstance(f.get("date"), dict) and f["date"].get("type") == "STATIC_VALUE"
            and "time_of_day" in f)


def find_list_filter(criteria):
    for d in walk(criteria):
        if d.get("filterType") == "IN_LIST" and "listId" in d:
            return d
    return None


def find_goal(flow):
    """Return (key, value) of a non-empty goal-like top-level field, if any."""
    for k, v in flow.items():
        if "goal" in k.lower() and v:
            return k, v
    return None


def discover_templates(flow_summaries, explicit_ids):
    want = {"email": None, "delay": None, "enroll": None, "flow": None, "goal": None}
    ids = explicit_ids or [f["id"] for f in flow_summaries
                           if f.get("objectTypeId", CONTACT_OBJECT_TYPE) == CONTACT_OBJECT_TYPE]
    for fid in ids:
        flow = api("GET", f"/automation/v4/flows/{fid}")
        if flow.get("objectTypeId") != CONTACT_OBJECT_TYPE:
            continue
        want["flow"] = want["flow"] or flow
        for a in flow.get("actions", []):
            if want["email"] is None and is_email_action(a):
                want["email"] = (fid, a)
            if want["delay"] is None and is_static_date_delay(a):
                want["delay"] = (fid, a)
        crit = flow.get("enrollmentCriteria") or {}
        if want["enroll"] is None and find_list_filter(crit):
            want["enroll"] = (fid, crit)
        if want["goal"] is None and find_goal(flow):
            want["goal"] = (fid, find_goal(flow))
        if all(want[k] for k in ("email", "delay", "enroll", "goal")):
            break
    missing = [k for k in ("email", "delay", "enroll") if not want[k]]
    if missing:
        die("No existing contact workflow to copy for: " + ", ".join(missing)
            + ". Build one by hand (or pass --template-flow-id) so the format can be copied.")
    return want


# ---------- payload construction ----------

def same_type(template_value, new_value):
    return str(new_value) if isinstance(template_value, str) else new_value


def build_enrollment(template_crit, list_id):
    crit = copy.deepcopy(template_crit)
    # Keep only the branch path that leads to the IN_LIST filter, with one filter.
    def prune(branch):
        for f in branch.get("filters", []):
            if f.get("filterType") == "IN_LIST":
                nf = copy.deepcopy(f)
                nf["listId"] = same_type(f["listId"], list_id)
                branch["filters"] = [nf]
                branch["filterBranches"] = []
                return True
        for child in branch.get("filterBranches", []):
            if prune(child):
                branch["filterBranches"] = [child]
                branch["filters"] = []
                return True
        return False

    for key, val in list(crit.items()):
        if isinstance(val, dict) and prune(val):
            break
    else:
        die("Could not rebuild the list-membership enrollment filter from the template")
    crit["shouldReEnroll"] = False
    if "reEnrollmentTriggersFilterBranches" in crit:
        crit["reEnrollmentTriggersFilterBranches"] = []
    if "unEnrollObjectsNotMeetingCriteria" in crit:
        crit["unEnrollObjectsNotMeetingCriteria"] = False
    return crit


def build_email(template, content_id):
    a = copy.deepcopy(template)
    a["fields"]["content_id"] = same_type(template["fields"]["content_id"], content_id)
    return a


def build_delay(template, date_str, hour, minute):
    a = copy.deepcopy(template)
    f = a["fields"]
    # HubSpot date values are midnight UTC of the calendar date, in epoch ms.
    d = dt.date.fromisoformat(date_str)
    ms = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    f["date"]["staticValue"] = same_type(f["date"].get("staticValue"), ms)
    tod = f["time_of_day"]
    if isinstance(tod, dict):
        tod["hour"], tod["minute"] = hour, minute
    else:
        die(f"Unexpected time_of_day format in template: {tod!r}")
    if "delta" in f:
        f["delta"] = same_type(f["delta"], 0)
    return a


def build_goal(goal_template):
    """Rebuild the template's goal with one 'last meeting booked is known' filter.
    Only works if the template already has an IS_KNOWN property filter to copy."""
    key, val = goal_template
    for d in walk(val):
        op = d.get("operation")
        if d.get("filterType") == "PROPERTY" and isinstance(op, dict) and op.get("operator") == "IS_KNOWN":
            nf = copy.deepcopy(d)
            nf["property"] = MEETING_BOOKED_PROPERTY
            goal = copy.deepcopy(val)
            def replace(branch):
                if "filters" in branch:
                    branch["filters"] = [nf]
                    branch["filterBranches"] = []
                    return True
                for c in branch.get("filterBranches", []):
                    if replace(c):
                        branch["filterBranches"] = [c]
                        return True
                return False
            if isinstance(goal, dict) and replace(goal):
                return key, goal
    return None


def build_flow(cfg, t):
    actions, next_id = [], 1
    for step in cfg["steps"]:
        if "email" in step:
            a = build_email(t["email"][1], step["email"])
        else:
            a = build_delay(t["delay"][1], step["delay_until"], step["hour"], step["minute"])
        a["actionId"] = str(next_id)
        a.pop("connection", None)
        actions.append(a)
        next_id += 1
    for cur, nxt in zip(actions, actions[1:]):
        cur["connection"] = {"edgeType": "STANDARD", "nextActionId": nxt["actionId"]}

    flow = {k: v for k, v in t["flow"].items() if k not in READ_ONLY_KEYS}
    flow.update({
        "name": cfg["name"],
        "isEnabled": False,
        "objectTypeId": CONTACT_OBJECT_TYPE,
        "startActionId": actions[0]["actionId"],
        "nextAvailableActionId": str(next_id),
        "actions": actions,
        "enrollmentCriteria": build_enrollment(t["enroll"][1], cfg["enrollment_list_id"]),
    })
    # Don't carry over settings from the template workflow.
    for k, empty in (("suppressionListIds", []), ("blockedDates", []), ("timeWindows", []),
                     ("customProperties", {}), ("dataSources", [])):
        if k in flow:
            flow[k] = empty
    for k in list(flow):
        if "goal" in k.lower():
            flow.pop(k)
    flow.pop("description", None)

    goal_note = "skipped: no existing workflow shows the goal/unenrollment format the API uses"
    if cfg.get("unenroll_on_meeting_booked") and t["goal"]:
        g = build_goal(t["goal"][1])
        if g:
            flow[g[0]] = g[1]
            goal_note = f"added via '{g[0]}' (format copied from flow {t['goal'][0]})"
        else:
            goal_note = (f"skipped: flow {t['goal'][0]} has a goal, but not a property "
                         "'is known' filter I could copy safely")
    return flow, goal_note


# ---------- summary ----------

def summarize(flow):
    lines = [f"Name:       {flow.get('name')}",
             f"Flow ID:    {flow.get('id')}",
             f"Object:     {flow.get('objectTypeId')} (contacts)",
             f"Enabled:    {flow.get('isEnabled')}"]
    crit = flow.get("enrollmentCriteria") or {}
    lf = find_list_filter(crit)
    lines.append(f"Trigger:    contact is a member of list {lf.get('listId') if lf else '??'}"
                 f" (criteria type {crit.get('type')})")
    lines.append(f"Re-enroll:  {'ON' if crit.get('shouldReEnroll') else 'off'}")
    g = find_goal(flow)
    lines.append(f"Goal:       {json.dumps(g[1]) if g else 'none'}")
    by_id = {a["actionId"]: a for a in flow.get("actions", [])}
    aid, n = flow.get("startActionId"), 1
    while aid and aid in by_id:
        a = by_id[aid]
        f = a.get("fields", {})
        if is_email_action(a):
            desc = f"Send marketing email, content ID {f['content_id']}"
        elif "date" in f and "time_of_day" in f:
            ms = int(f["date"].get("staticValue", 0))
            day = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date()
            tod = f["time_of_day"]
            desc = (f"Delay until {day.strftime('%A, %B %d, %Y')} at "
                    f"{tod.get('hour'):02d}:{tod.get('minute', 0):02d} (portal time zone)")
        else:
            desc = f"Other action: {json.dumps(f)}"
        lines.append(f"Step {n}:     [{a.get('actionTypeId')}] {desc}")
        aid = (a.get("connection") or {}).get("nextActionId")
        n += 1
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("config")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--template-flow-id", action="append", default=[])
    args = p.parse_args()
    cfg = json.load(open(args.config))

    # 1. Stop if the workflow already exists.
    flows = list_flows()
    dup = [f for f in flows if f.get("name") == cfg["name"]]
    if dup:
        die(f"Workflow '{cfg['name']}' already exists (id {dup[0].get('id')}). Not creating another.")

    # 2. Delay times use the portal time zone, so it has to match the config.
    tz = api("GET", "/account-info/v3/details").get("timeZone")
    if tz != cfg.get("timezone", "America/New_York"):
        die(f"Portal time zone is {tz!r}, config expects {cfg.get('timezone')!r}; "
            "the delay times would be wrong.")

    # 3. Copy formats from existing workflows.
    t = discover_templates(flows, args.template_flow_id)
    print(f"Templates: email action from flow {t['email'][0]}, delay action from flow "
          f"{t['delay'][0]}, enrollment from flow {t['enroll'][0]}")
    flow, goal_note = build_flow(cfg, t)
    print(f"Unenroll on meeting booked: {goal_note}")

    if args.dry_run:
        print(json.dumps(flow, indent=2))
        return

    # 4. Create (disabled), then read it back.
    created = api("POST", "/automation/v4/flows", flow)
    fid = created["id"]
    fetched = api("GET", f"/automation/v4/flows/{fid}")
    if fetched.get("isEnabled"):
        print("WARNING: workflow came back ENABLED. Turn it off in HubSpot.", file=sys.stderr)
    print("\n" + summarize(fetched))
    print("\nEdit link: " + EDIT_URL.format(portal=PORTAL_ID, flow_id=fid))


if __name__ == "__main__":
    main()
