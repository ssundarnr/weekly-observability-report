#!/usr/bin/env python3
"""Weekly observability report on New Relic Workflow Automation (WA). See README.md.

Needs only weekly-report.yaml next to it (Python 3.10+, stdlib only):
  python weekly_report.py --destinations             # list the account's email destinations (pick DESTINATION_ID)
  python weekly_report.py --check                    # validate the workflow YAML against the account
  python weekly_report.py --deploy                   # create or update the workflow
  python weekly_report.py --run --confirm-send       # run it now: calls Autopilot (aCCU) and sends the email
  python weekly_report.py --run --confirm-send --log-level TRACE   # same, and logs every step's inputs and outputs
  python weekly_report.py --schedule                 # show what would be scheduled (default Mondays 08:00 Europe/Dublin)
  python weekly_report.py --schedule --confirm-schedule   # create the schedule, or replace an existing one
  python weekly_report.py --schedule --cron "0 7 * * 2" --timezone Europe/London --confirm-schedule   # change the time
  python weekly_report.py --unschedule               # delete the schedule
  python weekly_report.py --delete                   # delete the workflow

Needs the rc_*.py sources too (maintainers):
  python weekly_report.py --preview                  # run the whole report locally on live data, write the HTML (sends nothing)
  python weekly_report.py --build                    # rebuild weekly-report.yaml
  python weekly_report.py --deploy --stage compute   # test workflow: queries and scoring only (no Autopilot, no email)
  python weekly_report.py --run --stage compute
  python weekly_report.py --package DIR              # copy the guide (as README.md), weekly-report.yaml and this script into DIR

Config (env vars, or KEY=VALUE lines in ~/.nr-weekly.env): NR_API_KEY (User key, NRAK-...), NR_ACCOUNT_ID,
NR_REGION (us or eu, default us), DESTINATION_ID (email destination for the report),
NR_REPORT_CRON and NR_REPORT_TIMEZONE (schedule time; default "0 8 * * 1" in Europe/Dublin).

WA limits that shape this file (verified 2026-11-04, see WEEKLY_REPORT_HANDOFF.md): a script is at most 10,000 characters
AFTER ${{ }} interpolation, and a ${{ }} ends at the first '}}'. So jq selectors trim every query result before it reaches a
script, nested jq objects are written '} }', and the md step cuts Autopilot's reply before turning it into HTML.
"""
import argparse
import ast
import base64
import contextlib
import html
import io
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

import urllib.error
import urllib.request


def load_env_file():
    path = pathlib.Path(os.getenv("NR_ENV_FILE", "~/.nr-weekly.env")).expanduser()
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ")
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_env_file()
NR_API_KEY = os.getenv("NR_API_KEY", "")
NR_ACCOUNT_ID = int(os.getenv("NR_ACCOUNT_ID", "0") or 0)
NR_REGION = os.getenv("NR_REGION", "us").lower()
NERDGRAPH_URL = os.getenv("NERDGRAPH_URL", "https://api.eu.newrelic.com/graphql" if NR_REGION == "eu" else "https://api.newrelic.com/graphql")


def nerdgraph(query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        NERDGRAPH_URL, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"API-Key": NR_API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {NERDGRAPH_URL}: {e.reason}") from None
    if body.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in body["errors"]))
    return body["data"]


def nrql(q: str) -> list:
    query = "{ actor { account(id: %d) { nrql(query: %s) { results } } } }" % (NR_ACCOUNT_ID, json.dumps(q))
    return nerdgraph(query)["actor"]["account"]["nrql"]["results"]


HERE = pathlib.Path(__file__).resolve().parent
DESTINATION_ID = os.getenv("DESTINATION_ID", "")
DEST_PLACEHOLDER = "REPLACE_WITH_EMAIL_DESTINATION_ID"
# The rc_*.py sources are needed only to rebuild the YAML (--build, --preview, --stage compute). Without them,
# --check and --deploy use the shipped weekly-report.yaml.
HAVE_SOURCES = all((HERE / f).is_file() for f in ("rc_incidents.py", "rc_apm.py", "rc_infra.py", "rc_dem.py", "rc_render.py", "rc_markdown.py"))
NAMES = {"full": "weekly-observability-report", "compute": "weekly-observability-report-compute-test"}
YAML_FILE = HERE / "weekly-report.yaml"
PREVIEW_FILE = HERE / "weekly_report_preview.html"
PROMPT_FILE = HERE / "weekly_report_prompt_preview.txt"
SCRIPT_LIMIT = 10000


def _inner(priority, extra=""):
    return ("FROM NrAiIncident SELECT latest(event) AS lastEvent%s WHERE priority = '%s' AND event IN ('open','close') "
            "FACET incidentId LIMIT MAX" % (extra, priority))


# Key order is the source numbering in the email. Subqueries take their time range on the outer query.
QUERIES = {
    "inc_crit": "FROM (%s) SELECT count(*) AS total, filter(count(*), WHERE lastEvent = 'open') AS openNow SINCE 7 days ago"
                % _inner("critical"),
    "inc_warn": "FROM (%s) SELECT count(*) AS total, filter(count(*), WHERE lastEvent = 'open') AS openNow SINCE 7 days ago"
                % _inner("warning"),
    "inc_opened": "FROM NrAiIncident SELECT filter(count(*), WHERE event = 'open') AS opened FACET priority "
                  "SINCE 7 days ago COMPARE WITH 1 week ago",
    "inc_open_rows": "FROM (%s) SELECT latest(title) AS title, latest(entityName) AS entityName, min(openedAt) AS openedAt "
                     "WHERE lastEvent = 'open' FACET incidentId LIMIT 1000 SINCE 7 days ago"
                     % _inner("critical", ", min(openTime) AS openedAt, latest(title) AS title, latest(entity.name) AS entityName"),
    "counts": "SELECT count(*) FROM NrAiIncident, Transaction, SystemSample, SyntheticCheck, ServiceLevelSnapshot "
              "FACET eventType() SINCE 7 days ago",
    "apm": "FROM Transaction SELECT count(*) AS reqs, percentage(count(*), WHERE error IS true) AS errPct, "
           "percentile(duration, 95) AS p95 FACET appName SINCE 7 days ago COMPARE WITH 1 week ago LIMIT 100",
    "hosts": "FROM SystemSample SELECT max(cpuPercent), max(memoryUsedPercent), max(diskUsedPercent) FACET hostname "
             "SINCE 7 days ago COMPARE WITH 1 week ago LIMIT 100",
    "synth_overall": "FROM SyntheticCheck SELECT count(*) AS checks, percentage(count(*), WHERE result = 'FAILED') AS failPct "
                     "SINCE 7 days ago COMPARE WITH 1 week ago",
    "synth_monitors": "FROM SyntheticCheck SELECT percentage(count(*), WHERE result = 'FAILED') AS failPct, count(*) AS checks "
                      "FACET monitorName SINCE 7 days ago LIMIT 100",
    "sls": "FROM ServiceLevelSnapshot SELECT latest(remainingErrorBudget) AS budget, latest(entity.name) AS name "
           "FACET entity.guid SINCE 1 day ago LIMIT 100",
    "br": "FROM PageViewTiming SELECT percentile(largestContentfulPaint, 75) AS lcp, count(largestContentfulPaint) AS nl, "
          "percentile(interactionToNextPaint, 75) AS inp, count(interactionToNextPaint) AS ni, "
          "percentile(cumulativeLayoutShift, 75) AS cls, count(cumulativeLayoutShift) AS nc "
          "FACET appName SINCE 7 days ago COMPARE WITH 1 week ago LIMIT 100",
    "js": "FROM PageView, JavaScriptError SELECT filter(count(*), WHERE eventType() = 'PageView') AS pv, "
          "filter(count(*), WHERE eventType() = 'JavaScriptError') AS jse FACET appName SINCE 7 days ago COMPARE WITH 1 week ago LIMIT 100",
    "mob": "FROM MobileSession, MobileCrash, MobileRequest, MobileRequestError SELECT "
           "filter(count(*), WHERE eventType() = 'MobileSession') AS sessions, filter(count(*), WHERE eventType() = 'MobileCrash') AS crashes, "
           "filter(count(*), WHERE eventType() = 'MobileRequest') AS reqs, filter(count(*), WHERE eventType() = 'MobileRequestError') AS reqErrs "
           "FACET appName SINCE 7 days ago COMPARE WITH 1 week ago LIMIT 100",
    "mob_ver": "FROM MobileCrash SELECT count(*) AS crashes FACET appName, appVersion SINCE 7 days ago LIMIT 3",
}
# Browser and mobile apps in the account (NerdGraph, not NRQL): an area with no apps is left out, not reported as a gap.
ENTITY_GQL = "query($q: String!) { actor { entitySearch(query: $q) { types { domain count } } } }"
ENTITY_SEARCH = "domain IN ('BROWSER', 'MOBILE') AND type = 'APPLICATION' AND accountId = "
# The account name for the email heading. The account ID is a template in the query text.
ACCOUNT_GQL = "{ actor { account(id: ACCOUNT_ID) { name } } }"
# Source numbering in the email: the NRQL queries, then the app lookup.
SOURCES = dict(QUERIES, apps="NerdGraph entitySearch: " + ENTITY_SEARCH + "<this account>")

# jq selectors, applied to {results: [...]}; each query step exposes the trimmed value as outputs.d
R2 = "((%s * 100 | round) / 100)"
SELECTORS = {
    "counts": '.results | map({(.["eventType()"]): .count}) | add // {}',
    "inc_crit": ".results[0] // {total: 0, openNow: 0}",
    "inc_warn": ".results[0] // {total: 0, openNow: 0}",
    "inc_opened": ".results | map({p: .priority, c: .comparison, o: .opened})",
    "inc_open_rows": '.results | map(select(.openedAt != null)) | sort_by(.openedAt) | '
                     '{n: length, t: (.[:10] | map({t: ((.title // "untitled") | .[0:160]), '
                     'e: ((.entityName // "unknown") | .[0:60]), o: .openedAt}))}',
    "apm": '.results | (map(select(.comparison == "previous"))) as $pr | (map(select(.comparison != "previous"))) as $cr'
           ' | ($pr | map({key: .appName, value: .}) | from_entries) as $pm'
           ' | ($cr | map({a: (.appName | .[0:36]), r: ((.reqs // 0) | round), e: ' + R2 % "(.errPct // 0)" +
           ', p: ((((.p95["95"] // 0) * 10000) | round) / 10),'
           ' q: (($pm[.appName].p95["95"] // null) | if . == null then null else ((. * 10000 | round) / 10) end),'
           ' pr: (($pm[.appName].reqs // 0) | round)})) as $it'
           ' | ("__MINR__" | tonumber) as $mr'
           # s: the 5 services whose p95 rose most against last week, among those with enough requests in both weeks
           ' | {n: ($it | length), e: ($it | sort_by(-.e) | .[:12]), l: ($it | sort_by(-.p) | .[:5]),'
           ' s: ($it | map(select(.r >= $mr and .pr >= $mr and (.q // 0) > 0)) | sort_by(-(.p / .q)) | .[:5]),'
           ' pv: ($pr | sort_by(-(.errPct // 0)) | .[:12] | map([((.reqs // 0) | round), ' + R2 % "(.errPct // 0)" + "]))}",
    "hosts": '.results | (map(select(.comparison == "previous"))) as $pr | (map(select(.comparison != "previous"))) as $cr'
             ' | ($cr | map(. + {pk: ([(.["max.cpuPercent"] // 0), (.["max.memoryUsedPercent"] // 0), '
             '(.["max.diskUsedPercent"] // 0)] | max)})) as $c'
             ' | ($pr | map([(.["max.cpuPercent"] // 0), (.["max.memoryUsedPercent"] // 0), (.["max.diskUsedPercent"] // 0)]'
             ' | max | round) | sort | reverse | .[:15]) as $pp'
             ' | {n: ($c | length), pp: $pp, peaks: ($c | map(.pk | round) | sort | reverse | .[:15]),'
             ' top: ($c | sort_by(-.pk) | .[:5] | map({h: (.hostname | .[0:30]), u: ((.["max.cpuPercent"] // 0) | round),'
             ' m: ((.["max.memoryUsedPercent"] // 0) | round), d: ((.["max.diskUsedPercent"] // 0) | round)}))}',
    "synth_overall": ".results | map({c: .comparison, f: " + R2 % "(.failPct // 0)" + "})",
    "synth_monitors": '.results | {n: length, top: (sort_by(-(.failPct // 0)) | .[:10] | map({m: ((.monitorName // "unnamed") | .[0:40]),'
                      " f: " + R2 % "(.failPct // 0)" + ", n: (.checks | round)}))}",
    "sls": '.results | map(select(.budget != null)) | {n: length, low: (sort_by(.budget) | .[:8] | '
           'map({n: ((.name // "unnamed") | .[0:50]), b: (.budget | round)}))}',
}
# Browser and mobile rows are compact arrays [app, previous (1) or current (0), values...]: for each metric, this week's
# worst apps among those with enough samples, each with its previous-week row. The area takes its worst value, so keeping
# the worst app per metric keeps the status right. n counts all of this week's apps.
APP_PREV = '((.appName // "unnamed") | .[0:30]), (if .comparison == "previous" then 1 else 0 end)'
# The sample and request minimums are workflow inputs, written into the selectors as templates (values in the local preview).
MIN_INPUTS = {"__MINB__": "minBrowserSamples", "__MINM__": "minMobileSessions", "__MINR__": "minRequests"}


def worst_apps(min_token, picks):
    # picks: (sample count, value to rank by, how many apps to keep)
    keep = " + ".join('($cr | map(select((%s // 0) >= $ms)) | sort_by(-(%s)) | .[:%d] | map(.appName // "unnamed"))'
                      % pick for pick in picks)
    return ('.results | (map(select(.comparison != "previous"))) as $cr | ("%s" | tonumber) as $ms'
            ' | (%s | map({key: ., value: 1}) | from_entries) as $tm'
            ' | {n: ($cr | length), r: (map(select($tm[.appName // "unnamed"] != null)) | map([' % (min_token, keep))


def selector(key, values=None):
    s = SELECTORS[key]
    for token, name in MIN_INPUTS.items():
        s = s.replace(token, str(values[name]) if values else "${{ .workflowInputs.%s }}" % name)
    return s


SELECTORS.update({
    "br": worst_apps("__MINB__", [(".nl", '.lcp["75"] // 0', 2), (".ni", '.inp["75"] // 0', 2), (".nc", '.cls["75"] // 0', 2)])
          + APP_PREV + ', ' + R2 % '(.lcp["75"] // 0)' + ', (.nl // 0), '
          '(((.inp["75"] // 0) * 1000) | round), (.ni // 0), ((((.cls["75"] // 0) * 1000) | round) / 1000), (.nc // 0)]))}',
    "js": worst_apps("__MINB__", [(".pv", "if (.pv // 0) > 0 then (.jse // 0) / .pv else 0 end", 6)])
          + APP_PREV + ', (.pv // 0), (.jse // 0)]))}',
    "mob": worst_apps("__MINM__", [(".sessions", "if (.sessions // 0) > 0 then (.crashes // 0) / .sessions else 0 end", 3),
                                   (".sessions", "if (.reqs // 0) > 0 then (.reqErrs // 0) / (.reqs + (.reqErrs // 0)) else 0 end", 3)])
           + APP_PREV + ', (.sessions // 0), (.crashes // 0), (.reqs // 0), (.reqErrs // 0)]))}',
    # A two-attribute FACET returns facet: [appName, appVersion]
    "mob_ver": '.results | map([((.facet[0] // "unnamed") | tostring | .[0:30]), ((.facet[1] // "unknown") | tostring | .[0:20]), '
               '(.crashes // 0)]) | .[:1]',
})

# Workflow inputs. Typed as Float (Int for the minimums): the run form in the WA UI sends numbers, and a String
# input then fails with "must be of type String" (2026-11-10). The scripts read them with float().
INPUT_DEFAULTS = {
    "errRedPct": 5, "errAmberPct": 1, "p95IncreasePct": 20, "minRequests": 100,
    "hostAmberPct": 85, "hostRedPct": 95, "synthAmberPct": 5, "synthRedPct": 50, "sloAmberBudgetPct": 25,
    # Browser: Google's Core Web Vitals cut-offs at p75 (good up to amber, poor above red); JS errors per 100 page views.
    "lcpAmberSec": 2.5, "lcpRedSec": 4, "inpAmberMs": 200, "inpRedMs": 500, "clsAmber": 0.1, "clsRed": 0.25,
    "jsErrAmberPct": 5, "jsErrRedPct": 25, "minBrowserSamples": 20,
    # Mobile: crashes per 100 sessions; HTTP errors per 100 requests.
    "crashAmberPct": 1, "crashRedPct": 3, "mobileHttpAmberPct": 5, "mobileHttpRedPct": 20, "minMobileSessions": 50,
}

INT_INPUTS = {"minRequests", "minBrowserSamples", "minMobileSessions"}

# What the WA script sandbox rejected in probes (2026-11-04); the real scan may reject more.
ALLOWED_IMPORTS = {"json"}
BANNED_NAMES = {"globals", "locals", "eval", "exec", "compile", "open", "__import__", "vars", "dir", "getattr",
                "setattr", "delattr", "input", "breakpoint"}


def P(step):
    return f"(.steps.{step}.outputs.payload | fromjson)"


def cfg_expr(keys):
    return "{" + ", ".join(f"{k}: .workflowInputs.{k}" for k in keys) + "}"


def D(key):
    return f".steps.q_{key}.outputs.d"


# (step name, source file, jq expression for INPUT)
SCRIPTS = [
    ("sa", "rc_incidents.py", "{now: now, c: %s, w: %s, o: %s, t: %s}" % (D("inc_crit"), D("inc_warn"), D("inc_opened"), D("inc_open_rows"))),
    ("sb", "rc_apm.py", "{cfg: %s, ap: %s, ec: %s}" % (cfg_expr(["minRequests", "errAmberPct", "errRedPct", "p95IncreasePct"]), D("apm"), D("counts"))),
    ("sc", "rc_infra.py", "{cfg: %s, ec: %s, h: %s, so: %s, sm: %s, sl: %s}" % (
        cfg_expr(["hostAmberPct", "hostRedPct", "synthAmberPct", "synthRedPct", "sloAmberBudgetPct"]), D("counts"), D("hosts"),
        D("synth_overall"), D("synth_monitors"), D("sls"))),
    ("sd", "rc_dem.py", "{cfg: %s, ent: (.steps.apps.outputs.data.actor.entitySearch.types // null), br: %s, js: %s, mob: %s, mv: %s}" % (
        cfg_expr(["lcpAmberSec", "lcpRedSec", "inpAmberMs", "inpRedMs", "clsAmber", "clsRed", "jsErrAmberPct", "jsErrRedPct",
                  "minBrowserSamples", "crashAmberPct", "crashRedPct", "mobileHttpAmberPct", "mobileHttpRedPct", "minMobileSessions"]),
        D("br"), D("js"), D("mob"), D("mob_ver"))),
]
# Area order in the health summary: Alert events, Browser, Mobile, APM services, Hosts, Synthetics, Service levels.
AREAS_EXPR = f"([{P('sa')}.area] + {P('sd')}.areas + [{P('sb')}.area] + {P('sc')}.areas)"
GAPS_EXPR = f"({P('sa')}.gaps + {P('sb')}.gaps + {P('sc')}.gaps + {P('sd')}.gaps)"
# Object values must be simple terms in the WA jq dialect (`{t: (expr).top}` fails), so bind the payloads first.
# Every text field is capped, so rc_render has a fixed worst case under SCRIPT_LIMIT (measured with test data).


def clip(expr, n):
    # Cut to n characters, ending in "..." when cut. Quotes, backslashes and control characters become ' / and a space:
    # after '| tojson | tojson' each would cost up to 4 characters. explode/implode avoids quote escapes in the template.
    return (f'(({expr} // "") | tostring | (if length > {n} then .[0:{n - 3}] + "..." else . end) | explode'
            ' | map(if . == 34 then 39 elif . == 92 then 47 elif . < 32 then 32 else . end) | implode)')


RENDER_CAPS = {"headline": 100, "delta": 70, "row": 100, "gap": 110, "gaps": 4, "title": 80, "entity": 40, "account": 50}


def render_input(caps):
    return (f"{P('sa')} as $a | {P('sb')} as $b | {P('sc')} as $c | {P('sd')} as $d | "
            "{a: (([$a.area] + $d.areas + [$b.area] + $c.areas) | map(. + {headline: %s, delta: %s, rows: (.rows | .[:2] | map(%s))})), "
            % (clip(".headline", caps["headline"]), clip(".delta", caps["delta"]), clip(".", caps["row"])) +
            # Gaps beyond the cap are counted in a last line, never dropped silently.
            "g: (($a.gaps + $b.gaps + $c.gaps + $d.gaps) as $G | if ($G | length) > %d then ($G | .[:%d] | map(%s)) + "
            "[((($G | length) - %d) | tostring) + \" more data gaps not shown.\"] else ($G | map(%s)) end), "
            % (caps["gaps"], caps["gaps"] - 1, clip(".", caps["gap"]), caps["gaps"] - 1, clip(".", caps["gap"])) +
            "t: ($a.top | map(. + {t: %s, e: %s})), n: $a.n, " % (clip(".t", caps["title"]), clip(".e", caps["entity"])) +
            "m: ($a.m + $b.m + $c.m + $d.m), gen: $a.gen, ok: $ap.ok, id: (.workflowConstants.accountId | tostring), "
            "an: %s}" % clip(".steps.acct.outputs.data.actor.account.name", caps["account"]))


RENDER_INPUT = render_input(RENDER_CAPS)
# Tested 2026-11-04: Autopilot did not see `context` items by name and asked for the incidents, so the
# facts go in the prompt text itself. Each fact is capped at 1,000 characters.
ACCOUNT_EXPR = ".workflowConstants.accountId | tostring"
ENTITY_Q_EXPR = f"{json.dumps(ENTITY_SEARCH)} + ({ACCOUNT_EXPR})"
FACTS = [
    ("report_window", f'"7 days ending " + {P("sa")}.gen + ", compared with the 7 days before. Account " + ({ACCOUNT_EXPR}) + "."'),
    ("health_summary", f'"Health summary: " + ({AREAS_EXPR} | map(.name + " " + .status) | join("; "))'),
    ("alert_event_1", f'{P("sa")}.ctx[0]'), ("alert_event_2", f'{P("sa")}.ctx[1]'), ("alert_event_3", f'{P("sa")}.ctx[2]'),
    ("apm", f'{P("sb")}.ctx[0]'), ("hosts", f'{P("sc")}.ctx[0]'), ("synthetics", f'{P("sc")}.ctx[1]'),
    ("browser", f'{P("sd")}.ctx[0]'), ("mobile", f'{P("sd")}.ctx[1]'),
    ("data_gaps", f'"Data gaps: " + ({GAPS_EXPR} | if length == 0 then "none" else join(" ") end)'),
]
PROMPT_INTRO = (
    "Write the Probable Causes and Recommended Actions for the weekly observability report on New Relic account {ACCOUNT}. "
    "The report's facts are listed below under FACTS. Cover only the critical alert events alert_event_1 to alert_event_3, and call them alert events, not incidents. "
    "Use exactly two plain-text headings: PROBABLE CAUSES and RECOMMENDED ACTIONS. "
    "Give each alert event one line per heading. Use only the FACTS and your own NRQL on account {ACCOUNT}, and cite the NRQL "
    "behind every number. State the evidence, or write unknown. Never guess. No data is never healthy. "
    "If a number in FACTS disagrees with what you find, add a line starting DISAGREEMENT: after the headings. "
    "Do not ask questions. If you cannot do the analysis, reply with one line only: NO ANALYSIS: followed by the reason, "
    "worded to complete the sentence 'Autopilot gave no analysis because ...'.")
FACTS_EXPR = "[" + ", ".join(f'"{d}: " + (({e}) // "none" | tostring | .[0:1000])' for d, e in FACTS) + '] | join("\\n")'
# The account id is read at run time, so the same YAML works in any account.
PROMPT_EXPR = (f" + ({ACCOUNT_EXPR}) + ".join(json.dumps(part) for part in (PROMPT_INTRO + "\n\nFACTS\n").split("{ACCOUNT}"))
               + f" + ({FACTS_EXPR})")
# Binds $ap = {ok, text}. ok is true when the reply names causes or actions in any wording (a 2026-11-04 test run ignored the
# required headings and wrote "Root Cause" and "Recommended Actions"); otherwise text says why there is no analysis.
AP_EXPR = (
    '(.steps.ask.outputs.answer | if type == "string" then (. as $s | try fromjson catch $s) else . end) as $ans'
    ' | (if ($ans | type) == "object" then ([$ans.card.body[]? | select(.type == "Text") | .value] | join("\\n\\n"))'
    ' elif $ans == null then "" else ($ans | tostring) end) as $raw'
    ' | (if (.steps.ask.outputs.success | tostring) != "true" then {ok: false, text: ("Autopilot gave no analysis because'
    ' the Autopilot step failed: " + ((.steps.ask.outputs.errorMessage // "no error message") | tostring | .[0:600]))}'
    ' elif ($raw | contains("NO ANALYSIS:")) then {ok: false, text: ("Autopilot gave no analysis because "'
    ' + ($raw | split("NO ANALYSIS:") | .[1] | ltrimstr(" ") | .[0:600]))}'
    ' elif ($raw | length) == 0 then {ok: false, text: "Autopilot gave no analysis because it returned an empty answer."}'
    ' elif ($raw | ascii_downcase | contains("probable cause") or contains("root cause") or contains("recommended action"))'
    ' then {ok: true, text: $raw}'
    ' else {ok: false, text: ("Autopilot gave no analysis because its reply named no causes or actions.'
    ' Its reply began: " + ($raw | .[0:600]))} end) as $ap | ')
RENDER_INPUT = AP_EXPR + RENDER_INPUT
# Input of the md step: the Autopilot text, cut so the md script stays under SCRIPT_LIMIT after interpolation.
MD_BUDGET = SCRIPT_LIMIT - (len((HERE / "rc_markdown.py").read_text()) if HAVE_SOURCES else 0) - 300
MD_INPUT = (AP_EXPR + '$ap.text as $t | ([8000, 7000, 6000, 5000, 4000, 3000, 2000] | map(. as $n | $t | .[0:$n])'
            f' | map(select(tojson | tojson | length <= {MD_BUDGET})) | .[0]) // ($t | .[0:1500])'
            ' | if length < ($t | length) then . + "\\n\\n(Autopilot\'s reply was cut to fit the email.)" else . end')


def sources_html():
    items = "".join("<div style='font-size:11px;color:#555;margin:2px 0'>[%d] %s</div>" % (i + 1, html.escape(q))
                    for i, q in enumerate(SOURCES.values()))
    return "<h2 style='font-size:14px;margin:22px 0 4px'>Sources (NRQL behind every number)</h2>" + items + "</div>"


def message_expr(with_sources=True):
    """Email body: head and tail from the render script, Autopilot text as HTML from the md step, static sources."""
    ap_head = ("<h2 style='font-size:16px;margin:22px 0 6px;border-bottom:1px solid #ddd'>"
               "3. Probable causes and recommended actions (Autopilot)</h2>"
               "<div style='font-size:13px;line-height:1.45'>")
    return (
        f'{P("r1")} as $r | $r.head + {json.dumps(ap_head)} + .steps.md.outputs.payload + "</div>" + $r.tail'
        + (f' + {json.dumps(sources_html())}' if with_sources else ''))


def no_double_brace(expr):
    while "}}" in expr:
        expr = expr.replace("}}", "} }")
    return expr


def tpl(expr):
    return "${{ " + no_double_brace(expr) + " }}"


def source(name):
    return (HERE / name).read_text()


def sandbox_check(code, label):
    problems = []
    for node in ast.walk(ast.parse(code.replace("__INPUT__", "'{}'").replace("__REF__", "'{}'"))):
        if isinstance(node, ast.Import):
            problems += [f"import {a.name}" for a in node.names if a.name not in ALLOWED_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            problems.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            problems.append(f"name {node.id}")
    if "{{" in code:
        problems.append("'{{' in script text would be read as a template")
    if problems:
        sys.exit(f"{label} would be rejected by the WA script sandbox: {sorted(set(problems))}")


def ref_literal():
    return json.dumps(json.dumps({k: i + 1 for i, k in enumerate(SOURCES)}))


def script_text(file_name, input_literal):
    return source(file_name).replace("__REF__", ref_literal()).replace("__INPUT__", input_literal)


# ---------------- YAML ----------------
def yq(text):
    return "'" + text.replace("'", "''") + "'"


def block(code):
    return "\n".join(("        " + line) if line else "" for line in code.rstrip().splitlines())


def script_step(name, file_name, input_expr, selectors=""):
    sandbox_check(source(file_name), file_name)
    code = script_text(file_name, tpl(input_expr + " | tojson | tojson"))
    return (f"  - name: {name}\n    type: action\n    action: script.run\n    version: '1'\n    inputs:\n"
            f"      runtime: PYTHON_3_13\n      script: |\n{block(code)}{selectors}")


def build_yaml(stage):
    inputs = "".join(f"  {k}:\n    type: {'Int' if k in INT_INPUTS else 'Float'}\n    required: false\n    defaultValue: {v}\n"
                     for k, v in INPUT_DEFAULTS.items())
    if stage == "full":
        inputs = f"  destinationId:\n    type: String\n    required: false\n    defaultValue: '{DEST_PLACEHOLDER}'\n" + inputs
    out = [f"name: {NAMES[stage]}", "description: Weekly observability report with health summary and Autopilot causes",
           "workflowInputs:\n" + inputs.rstrip("\n"), "", "steps:"]
    for k, q in QUERIES.items():
        out.append(f"  - name: q_{k}\n    type: action\n    action: newrelic.nrdb.query\n    version: '1'\n"
                   f"    inputs:\n      query: {yq(q)}\n      selectors:\n        - name: d\n          expression: {yq(selector(k))}")
    out.append("  - name: apps\n    type: action\n    action: newrelic.nerdgraph.execute\n    version: '1'\n    inputs:\n"
               f"      graphql: {yq(ENTITY_GQL)}\n      variables:\n        q: {yq(tpl(ENTITY_Q_EXPR))}")
    out.append("  - name: acct\n    type: action\n    action: newrelic.nerdgraph.execute\n    version: '1'\n    inputs:\n"
               f"      graphql: {yq(no_double_brace(ACCOUNT_GQL).replace('ACCOUNT_ID', tpl('.workflowConstants.accountId')))}")
    for name, file_name, expr in SCRIPTS:
        out.append(script_step(name, file_name, expr))
    summary = f'{AREAS_EXPR} | map(.name + " " + .status) | join("; ")'
    if stage == "compute":
        # The email step runs here too (without Autopilot, so section 3 says the step failed), to test RENDER_INPUT in WA jq.
        # A state value is limited to 3,000 characters, so the step itself is the test, not a state expression.
        out.append(script_step("r1", "rc_render.py", RENDER_INPUT))
        out.append("  - name: save\n    type: state\n    inputs:\n"
                   f"      summary: {yq('${{ ' + no_double_brace(summary) + ' }}')}\n"
                   f"      subject: {yq(tpl('.steps.r1.outputs.payload | fromjson | .subject'))}")
        return "\n".join(out) + "\n"
    out.append("  - name: ask\n    type: action\n    action: newrelic.autopilot.run\n    version: '1'\n    inputs:\n"
               f"      prompt: {yq(tpl(PROMPT_EXPR))}\n      context:\n"
               f"        - description: accountId\n          value: {yq(tpl(ACCOUNT_EXPR))}\n      selectors:\n"
               "        - name: answer\n          expression: "
               + yq("(.response | objects | .finalAnswer) // (.response | strings) // (.response | tostring)"))
    out.append(script_step("r1", "rc_render.py", RENDER_INPUT))
    out.append(script_step("md", "rc_markdown.py", MD_INPUT))
    out.append("  - name: email\n    type: action\n    action: newrelic.notification.sendEmail\n    version: '1'\n"
               "    inputs:\n      destinationId: '${{ .workflowInputs.destinationId }}'\n"
               f"      subject: {yq(tpl(P('r1') + '.subject'))}\n"
               f"      message: {yq(tpl(message_expr()))}")
    out.append("  - name: record\n    type: action\n    action: newrelic.ingest.sendEvents\n    version: '1'\n"
               f"    inputs:\n      events: {yq(tpl(P('r1') + '.events'))}")
    out.append("  - name: save\n    type: state\n    inputs:\n"
               "      ask_success: '${{ .steps.ask.outputs.success }}'\n"
               "      ask_error: '${{ (.steps.ask.outputs.errorMessage // \"\") | tostring | .[0:1000] }}'\n"
               f"      ask_analysed: {yq(tpl(P('r1') + '.events[0].autopilotOk'))}\n"
               "      email_success: '${{ .steps.email.outputs.success }}'\n"
               "      email_error: '${{ (.steps.email.outputs.errorMessage // \"\") | tostring | .[0:1000] }}'\n"
               "      record_success: '${{ .steps.record.outputs.success }}'\n"
               "      record_error: '${{ (.steps.record.outputs.errorMessage // \"\") | tostring | .[0:1000] }}'\n"
               f"      summary: {yq(tpl(summary))}")
    return "\n".join(out) + "\n"


# ---------------- NerdGraph ----------------
def scope():
    return f'{{id: "{int(NR_ACCOUNT_ID)}", type: ACCOUNT}}'


def validate(yaml_text):
    b64 = base64.b64encode(yaml_text.encode()).decode()
    r = nerdgraph(f'{{ actor {{ account(id: {int(NR_ACCOUNT_ID)}) {{ workflowAutomation {{ workflowDefinitionValidation('
                  f'definition: {{yaml: "{b64}"}}) {{ valid errors {{ message }} }} }} }} }} }}')
    return r["actor"]["account"]["workflowAutomation"]["workflowDefinitionValidation"]


def workflow_yaml(stage):
    """The YAML to deploy: rebuilt from the rc_*.py sources when present (and saved as weekly-report.yaml),
    else the shipped weekly-report.yaml. The email destination placeholder is filled from DESTINATION_ID."""
    if HAVE_SOURCES:
        yaml_text = build_yaml(stage)
        if stage == "full":
            YAML_FILE.write_text(yaml_text)
    elif stage == "full" and YAML_FILE.is_file():
        yaml_text = YAML_FILE.read_text()
    else:
        sys.exit(f"Needs {YAML_FILE.name} next to this script" + (" (or the rc_*.py sources for --stage compute)." if stage == "compute" else "."))
    if stage == "full":
        if not DESTINATION_ID:
            sys.exit("Set DESTINATION_ID (an email destination id; list them with --destinations).")
        yaml_text = yaml_text.replace(DEST_PLACEHOLDER, DESTINATION_ID)
    return yaml_text


def destinations():
    r = nerdgraph('{ actor { account(id: %d) { aiNotifications { destinations(filters: {type: EMAIL}) '
                  '{ entities { id name active properties { key value } } } } } } }' % NR_ACCOUNT_ID)
    ents = r["actor"]["account"]["aiNotifications"]["destinations"]["entities"]
    for e in ents:
        emails = ", ".join(x["value"] for x in e["properties"] if x["key"] == "email")
        print(f"{e['id']}  {'active  ' if e['active'] else 'inactive'}  {e['name']}  ({emails})")
    if not ents:
        print("No email destinations. Create one in Alerts > Destinations, then run this again.")


def package(target):
    """Copy the hand-over files into target: the quick start guide as README.md, the workflow YAML, this script."""
    out = pathlib.Path(target).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    if HAVE_SOURCES:
        YAML_FILE.write_text(build_yaml("full"))
    for src, dst in (("QUICKSTART.md", "README.md"), (YAML_FILE.name, YAML_FILE.name),
                     (pathlib.Path(__file__).name, pathlib.Path(__file__).name)):
        (out / dst).write_bytes((HERE / src).read_bytes())
        print("Copied", out / dst)
    for f in sorted((HERE / "docs").glob("*")) if (HERE / "docs").is_dir() else []:
        (out / "docs").mkdir(exist_ok=True)
        (out / "docs" / f.name).write_bytes(f.read_bytes())
        print("Copied", out / "docs" / f.name)
    (out / ".gitignore").write_text("__pycache__/\n*.env\nweekly_report_preview.html\nweekly_report_prompt_preview.txt\n")


def deploy(stage):
    yaml_text = workflow_yaml(stage)
    v = validate(yaml_text)
    if not v["valid"]:
        sys.exit("Workflow YAML is invalid:\n- " + "\n- ".join(e["message"] for e in v["errors"]))
    print("YAML valid.")
    b64 = base64.b64encode(yaml_text.encode()).decode()
    try:
        nerdgraph(f'mutation {{ workflowAutomationCreateWorkflowDefinition(scope: {scope()} definition: {{yaml: "{b64}"}}) '
                  f'{{ definition {{ name }} }} }}')
        print("Created", NAMES[stage])
    except RuntimeError as e:
        if "exist" not in str(e).lower():
            raise
        r = nerdgraph(f'mutation {{ workflowAutomationUpdateWorkflowDefinition(scope: {scope()} definition: {{yaml: "{b64}"}}) '
                      f'{{ definition {{ name version }} }} }}')
        print("Updated", r["workflowAutomationUpdateWorkflowDefinition"]["definition"])


def delete(stage):
    nerdgraph(f'mutation {{ workflowAutomationDeleteWorkflowDefinition(scope: {scope()} '
              f'definition: {{name: "{NAMES[stage]}"}}) {{ definition {{ name }} }} }}')
    print("Deleted", NAMES[stage])


def poll(run_id, keys):
    key_list = ", ".join(f'"{k}"' for k in keys)
    t0 = time.monotonic()
    while True:
        info = nerdgraph(f'{{ actor {{ account(id: {int(NR_ACCOUNT_ID)}) {{ workflowAutomation {{ workflowRunInfo(runId: "{run_id}") '
                         f'{{ status logUrl state(keys: [{key_list}]) {{ key value }} }} }} }} }} }}')
        info = info["actor"]["account"]["workflowAutomation"]["workflowRunInfo"]
        if info["status"] not in ("STARTED", "RUNNING") or time.monotonic() - t0 > 900:
            return info
        print(f"  ... {info['status']} ({int(time.monotonic() - t0)}s)", end="\r", flush=True)
        time.sleep(5)


def failure_logs(run_id):
    time.sleep(30)  # log ingest lag
    for r in nrql(f"SELECT message FROM Log WHERE `workflowAutomation.runId` = '{run_id}' AND message LIKE '%failed%' "
                  f"SINCE 1 hour ago LIMIT 5"):
        print(r["message"][:800])


def run(stage, log_level="INFO"):
    run_id = nerdgraph(f'mutation {{ workflowAutomationStartWorkflowRun(definition: {{name: "{NAMES[stage]}"}} scope: {scope()} '
                       f'idempotencyKey: "{uuid.uuid4()}" options: {{logLevel: {log_level}}}) {{ runId }} }}'
                       )["workflowAutomationStartWorkflowRun"]["runId"]
    print("run", run_id)
    keys = (["summary", "subject"] if stage == "compute" else
            ["ask_success", "ask_analysed", "ask_error", "email_success", "email_error", "record_success", "record_error", "summary"])
    info = poll(run_id, keys)
    print(" " * 40)
    print("status", info["status"], info.get("logUrl") or "")
    for kv in info.get("state") or []:
        print(f"  {kv['key']}: {kv['value']}")
    if info["status"] != "COMPLETED":
        failure_logs(run_id)


# Schedule time: --cron/--timezone, else NR_REPORT_CRON/NR_REPORT_TIMEZONE (env or ~/.nr-weekly.env), else Monday 08:00 Dublin.
CRON = os.getenv("NR_REPORT_CRON", "0 8 * * 1")
TIMEZONE = os.getenv("NR_REPORT_TIMEZONE", "Europe/Dublin")


def schedules_for(name):
    r = nerdgraph(f'{{ actor {{ account(id: {int(NR_ACCOUNT_ID)}) {{ workflowAutomation {{ schedules {{ results {{ '
                  f'definition {{ name version }} schedule {{ id }} }} }} }} }} }} }}')
    res = r["actor"]["account"]["workflowAutomation"]["schedules"]["results"] or []
    return [x for x in res if x["definition"]["name"] == name]


def workflow_version(name):
    r = nerdgraph(f'{{ actor {{ account(id: {int(NR_ACCOUNT_ID)}) {{ workflowAutomation {{ workflow(name: "{name}") '
                  f'{{ definition {{ name version }} }} }} }} }} }}')
    return r["actor"]["account"]["workflowAutomation"]["workflow"]["definition"]["version"]


def schedule(confirm, cron, timezone):
    """NerdGraph has no schedule update, so a change creates the new schedule first, then deletes the old one(s).
    If the create fails, the old schedule stays."""
    name = NAMES["full"]
    existing = schedules_for(name)
    version = workflow_version(name)
    print(f"{name}: deployed version {version}; existing schedules: {[(x['schedule']['id'], 'v%s' % x['definition']['version']) for x in existing]}")
    print(f"Would {'replace them with' if existing else 'create'}: cron '{cron}' in {timezone}, pinned to version {version}, "
          "overlap SKIP.")
    if not confirm:
        sys.exit("Nothing changed. Add --confirm-schedule to apply it.")
    r = nerdgraph(f'mutation {{ workflowAutomationCreateSchedule(scope: {scope()} '
                  f'definition: {{name: "{name}", version: {version}, scopeType: ACCOUNT}} cronExpression: {json.dumps(cron)} '
                  f'timezone: {json.dumps(timezone)} scheduleName: "{name}-weekly" overlapPolicy: SKIP) {{ schedule {{ id }} }} }}')
    print("Created schedule", r["workflowAutomationCreateSchedule"])
    for x in existing:
        nerdgraph(f'mutation {{ workflowAutomationDeleteSchedule(accountId: {int(NR_ACCOUNT_ID)} schedule: {{id: "{x["schedule"]["id"]}"}}) {{ __typename }} }}')
        print("Deleted old schedule", x["schedule"]["id"])


def unschedule():
    found = schedules_for(NAMES["full"])
    for x in found:
        nerdgraph(f'mutation {{ workflowAutomationDeleteSchedule(accountId: {int(NR_ACCOUNT_ID)} schedule: {{id: "{x["schedule"]["id"]}"}}) {{ __typename }} }}')
        print("Deleted schedule", x["schedule"]["id"])
    if not found:
        print("No schedule found.")


# ---------------- local preview: same selectors, expressions and scripts as the workflow ----------------
def jq(filter_, obj, raw=False):
    args = ["jq", "-c"] + (["-r"] if raw else []) + [no_double_brace(filter_)]
    r = subprocess.run(args, input=json.dumps(obj), capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"jq failed: {r.stderr.strip()[:500]}\nfilter: {filter_[:300]}")
    return r.stdout.strip() if raw else json.loads(r.stdout)


def run_script(file_name, input_obj):
    literal = json.dumps(json.dumps(input_obj))  # what '| tojson | tojson' produces
    code = script_text(file_name, literal)
    size = len(code)
    if size > SCRIPT_LIMIT:
        sys.exit(f"{file_name}: {size} characters after interpolation, over the {SCRIPT_LIMIT} limit")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, file_name, "exec"), {"__name__": "__sandbox__"})
    return out.getvalue().strip(), size


def preview():
    print("Fetching live data...")
    state = {"workflowInputs": dict(INPUT_DEFAULTS, destinationId=DESTINATION_ID), "steps": {},
             "workflowConstants": {"accountId": NR_ACCOUNT_ID}}
    for k, q in QUERIES.items():
        rows = nrql(q)
        state["steps"][f"q_{k}"] = {"outputs": {"d": jq(selector(k, INPUT_DEFAULTS), {"results": rows})}}
        print(f"  {k}: {len(rows)} row(s) -> {len(json.dumps(state['steps'][f'q_{k}']['outputs']['d']))} chars after trim")
    q = jq(ENTITY_Q_EXPR, state, raw=True)
    state["steps"]["apps"] = {"outputs": {"data": nerdgraph(ENTITY_GQL, {"q": q})}}
    print("  apps:", json.dumps(state["steps"]["apps"]["outputs"]["data"]["actor"]["entitySearch"]["types"]))
    state["steps"]["acct"] = {"outputs": {"data": nerdgraph(ACCOUNT_GQL.replace("ACCOUNT_ID", str(NR_ACCOUNT_ID)))}}
    state["now"] = time.time()
    for name, file_name, expr in SCRIPTS:
        # jq `now` is the local clock here, as it is the WA clock in the workflow
        payload, size = run_script(file_name, jq(expr, state))
        state["steps"][name] = {"outputs": {"payload": payload}}
        print(f"  {name} ({file_name}): script {size} of {SCRIPT_LIMIT} characters")
    prompt = jq(PROMPT_EXPR, state, raw=True)
    PROMPT_FILE.write_text(prompt)
    print(f"Autopilot prompt: {len(prompt)} chars, written to {PROMPT_FILE.name}")
    # Autopilot is not called locally; a saved real reply (markdown) stands in for it when present.
    sample = (HERE / "autopilot_reply_sample.md")
    reply = sample.read_text() if sample.is_file() else "PROBABLE CAUSES\n(preview)\n\nRECOMMENDED ACTIONS\n(preview)"
    card = json.dumps({"card": {"body": [{"type": "Text", "value": reply}]}})
    cases = {"analysis": {"success": True, "answer": card},
             "no analysis": {"success": True, "answer": "NO ANALYSIS: it found no data for these entities."},
             "off format": {"success": True, "answer": "Could you please share the incident IDs?"},
             "empty": {"success": True, "answer": ""},
             "step failed": {"success": False, "errorMessage": "ADVANCED_DAILY_SI_LIMIT_EXCEEDED"}}
    for label, outputs in cases.items():
        print(f"  Autopilot reply check, {label}:", jq(AP_EXPR + "$ap", {"steps": {"ask": {"outputs": outputs}}}))
    state["steps"]["ask"] = {"outputs": cases["analysis"]}
    payload, size = run_script("rc_render.py", jq(RENDER_INPUT, state))
    print(f"  r1 (rc_render.py): script {size} of {SCRIPT_LIMIT} characters")
    r1 = json.loads(payload)
    state["steps"]["r1"] = {"outputs": {"payload": payload}}
    long_card = json.dumps({"card": {"body": [{"type": "Text", "value": "Root cause line.\n" * 1500}]}})
    md_long = jq(MD_INPUT, {"steps": {"ask": {"outputs": {"success": True, "answer": long_card}}}})
    print(f"  md (rc_markdown.py), 25,500-char reply: script {run_script('rc_markdown.py', md_long)[1]} of {SCRIPT_LIMIT}")
    payload, size = run_script("rc_markdown.py", jq(MD_INPUT, state))
    print(f"  md (rc_markdown.py): script {size} of {SCRIPT_LIMIT} characters")
    state["steps"]["md"] = {"outputs": {"payload": payload}}
    body = jq(message_expr(), state, raw=True)
    PREVIEW_FILE.write_text(body)
    print("Subject:", r1["subject"])
    print("Health summary:", jq(AREAS_EXPR + " | map(.name + \" \" + .status) | join(\"; \")", state))
    print("Event:", json.dumps(r1["events"][0])[:500])
    print("Wrote", PREVIEW_FILE, f"({len(body)} chars)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ("destinations", "check", "preview", "build", "deploy", "run", "delete", "schedule", "unschedule"):
        p.add_argument("--" + flag, action="store_true")
    p.add_argument("--confirm-schedule", action="store_true", help="required to actually create or replace the schedule")
    p.add_argument("--cron", default=CRON, help="schedule cron, for --schedule (default %(default)s)")
    p.add_argument("--timezone", default=TIMEZONE, help="schedule timezone, for --schedule (default %(default)s)")
    p.add_argument("--stage", choices=["compute", "full"], default="full")
    p.add_argument("--log-level", choices=["NONE", "INFO", "DEBUG", "TRACE"], default="INFO", help="run log level for --run")
    p.add_argument("--confirm-send", action="store_true", help="required for --run on the full stage (calls Autopilot and sends email)")
    p.add_argument("--package", metavar="DIR", help="copy the hand-over files (README.md, YAML, this script) into DIR")
    a = p.parse_args()
    if a.package:
        return package(a.package)
    if not NR_API_KEY.startswith("NRAK-") or not NR_ACCOUNT_ID:
        sys.exit("Set NR_API_KEY (a User key, NRAK-...) and NR_ACCOUNT_ID, in the environment or ~/.nr-weekly.env.")
    if (a.preview or a.build) and not HAVE_SOURCES:
        sys.exit("--preview and --build need the rc_*.py source files; use --check and --deploy with the shipped YAML.")
    if a.destinations:
        destinations()
    elif a.preview:
        preview()
    elif a.check:
        v = validate(workflow_yaml(a.stage))
        print("YAML valid." if v["valid"] else "YAML invalid:\n- " + "\n- ".join(e["message"] for e in v["errors"]))
    elif a.build:
        YAML_FILE.write_text(build_yaml("full"))
        print("Wrote", YAML_FILE)
    elif a.deploy:
        deploy(a.stage)
    elif a.run:
        if a.stage == "full" and not a.confirm_send:
            sys.exit("Refusing to run the full workflow without --confirm-send (it calls Autopilot and sends an email).")
        run(a.stage, a.log_level)
    elif a.delete:
        delete(a.stage)
    elif a.schedule:
        schedule(a.confirm_schedule, a.cron, a.timezone)
    elif a.unschedule:
        unschedule()
    else:
        p.print_help()


if __name__ == "__main__":
    main()
