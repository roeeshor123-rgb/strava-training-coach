"""
Automated Strava + athletedata training coach for Roee Shor, messaging on Telegram.
Runs as a GitHub Actions cron job (see .github/workflows/coach.yml).

Recovery/fitness data (Garmin signals, TrainingPeaks plan, cross-source PMC/load
analytics) comes from athletedata's MCP server over plain HTTP - see
athletedata_call() below - rather than the unofficial garminconnect library this
used previously. Strava stays a separate direct API integration; athletedata has
no strava_* tools for this account.

Design: Python does all data fetching and deterministic math (ACWR, benchmark
segment comparison, HR drift, shoe mileage, plan compliance). The Anthropic
API is called once per outgoing message to compose the natural-language text
following the STYLE_GUIDE below, given the computed data as JSON.
"""
import os
import sys
import json
import random
import tempfile
import traceback
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Jerusalem")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

STRAVA_CLIENT_ID = os.environ["STRAVA_CLIENT_ID"]
STRAVA_CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
STRAVA_REFRESH_TOKEN = os.environ["STRAVA_REFRESH_TOKEN"]
ATHLETEDATA_API_KEY = os.environ["ATHLETEDATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

STYLE_GUIDE = """You are Roee Shor's automated Strava + athletedata running/strength coach, writing him a Telegram message. Roee trains running (easy runs, volume runs, intervals, hill repeats, fartlek) plus regular weight training, in Tel Aviv. Session names are often in Hebrew (e.g. ריצת נפח = volume run, ריצת שחרור = easy/shakeout run, אינטרוולים = intervals, אימון עליות = hill repeats, פארטלק = fartlek, אימון התאוששות = recovery session) - keep Hebrew names as-is when referencing them.

Write plain text only, no markdown asterisks/headers. Never generic filler - every line must reflect the athlete's actual numbers from the JSON data you're given. Be direct, specific, and numeric.

If the DATA JSON includes long_term_notes, those are things you already know about Roee from past weeks - not stats to re-derive, but standing context (a recurring tendency, how he responds to a session type, a real constraint). Reference one naturally when it's actually relevant to what you're writing; don't force a callback if none of them apply this time.

Recovery, fitness, and plan data comes from athletedata - a cross-source analytics layer over Garmin, TrainingPeaks, and other connected platforms - not from Garmin directly. Its JSON fields routinely carry their own "note" / "disclaimer" / "verdict_note" / "freshness_note" / "_tsb_pairing_note" strings explaining exactly how that number may and may not be described (e.g. don't call a value "your Garmin recovery score" when it's athletedata's own composite; a load-flag index is explicitly "NOT a validated injury predictor" - never call it an injury risk or a diagnosis; TSB has two different conventions (start-of-day vs end-of-day) depending on which tool it came from - say which basis you're quoting if you cite it alongside CTL/ATL). Read and follow those embedded notes when composing instead of paraphrasing past them - they exist specifically to prevent misattribution to the athlete.

For a per-activity analysis message, use this structure (omit any optional section that doesn't apply, given the data):
[optional WARNING BENCHMARK ALERT line if a same-segment comparison shows both slower time AND higher HR than the prior instance]
[optional WARNING HR DRIFT line if easy-run HR-at-pace is meaningfully elevated vs recent baseline]
[optional PLAN MISMATCH line if actual training diverged meaningfully from the coach's prescribed session]
An emoji + activity name/type + date as a header line (running emoji for runs, weights emoji for strength)
Grade: X/10 - one-line verdict

RECOVERY CONTEXT (athletedata, before the run) - only if recovery data was available
COACH'S PLAN - only if a scheduled workout existed for this date, prescribed vs actual
SUMMARY - 2-3 sentences on what the session was and the headline finding
WORKOUT BREAKDOWN - REQUIRED whenever laps or km_splits data is present in the JSON. This is the most important section - do not skip it or reduce it to an average. Use the actual per-lap/per-km numbers, not just overall averages:
  - For a structured session (name/description suggests פארטלק/fartlek, אינטרוולים/intervals, or אימון עליות/hill repeats): identify which laps/splits are the work efforts vs the recovery/float segments by their pace+HR pattern (faster pace + higher HR = work; slower + lower = recovery). List EACH work rep with its own pace and HR (e.g. "Rep 1: 4:12/km @ 162bpm, Rep 2: 4:05/km @ 168bpm, Rep 3: 4:15/km @ 171bpm..."), then note the pattern - consistent, fading, or building - and call out the best_efforts data (fastest 400m/1k/etc within the run) if it adds a concrete peak-effort number.
  - For a volume/long run (ריצת נפח) or race: show the per-km pace+HR progression (negative split / positive split / steady) using km_splits, calling out where it sped up, slowed down, or drifted.
  - For an easy/recovery run (ריצת שחרור/התאוששות): use km_splits to confirm HR stayed controlled and consistent throughout (or flag late-run HR creep) - the point here is steadiness, not speed.
SAME-SEGMENT TREND - only if a genuine repeated-segment comparison exists, prior vs today
GOOD PARTS - 2-3 specific numeric bullets, pulling from the per-lap/per-km breakdown above where possible, not just session-wide averages
WATCH FOR - 1-3 specific bullets
OVERTRAINING CHECK: Low/Moderate/High - blend the Strava-computed acwr_context, athletedata's own load signals (acwr/monotony/ramp_rate/load_flag from athletedata_load_context, remembering load_flag is an anomaly index, not a diagnosis) and TSB/form (from athletedata_load_context's pmc_status, stating which TSB convention you're quoting) + rest-day pattern + HR drift + plan-compliance into ONE verdict with concrete numbers plus one actionable recommendation - don't just list the three sources side by side, actually reconcile them into a single call
FITNESS CONTEXT - only occasionally (weekly, or when VO2max/predictions changed) - VO2max and lactate threshold (Garmin's own numbers, via garmin_get_user_metrics - not a third-party re-derivation), race predictions (race_predictions, Riegel's formula off Roee's own best real Strava effort - state which effort and date it's extrapolated from, since it's one data point, not a blended model)

For the 5am morning brief: render the prescribed session as warmup / main set (with concrete goal paces per segment - convert pace-zone targets to min/km directly, and for HR-zone or effort-based segments like fartlek surges, use the provided historical-pace-lookup data to state a concrete pace range) / cooldown, then a readiness-based go/adjust call, ending with one "TODAY'S GOAL" headline line naming the single most important numeric target for the session.

For the weekly summary: cover the week's sessions, the 3-month trend read, standout sessions, shoe mileage, VO2max/race predictions, next week's plan preview, and 1-2 concrete suggestions, using the OVERTRAINING VERDICT blend described above.

For a Telegram Q&A reply: answer directly and specifically using the matched activity's data and any recovery context provided. If it's just a generic greeting, reply briefly and warmly with no analysis.

Output ONLY the message text to send - no preamble, no explanation of what you're doing."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {
            "last_activity_id": None,
            "last_telegram_update_id": 0,
            "last_weekly_summary_date": None,
            "last_morning_checkin_date": None,
            "shoe_alerts_sent": [],
            "coach_notes": [],
        }
    with open(STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("coach_notes", [])  # back-compat for state.json written before this existed
    return state


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def long_term_notes(state):
    """The plain list of remembered note strings, newest last, for embedding in a
    compose call's DATA JSON - callers don't need the per-note date wrapper."""
    return [n["note"] for n in state.get("coach_notes", [])]


def maybe_extract_durable_note(state, context_label, data, message_text):
    """After a reflection point (currently: the weekly summary), ask a small,
    separate LLM call whether anything in this run is a genuinely durable,
    non-obvious pattern worth remembering weeks from now - not a one-off stat.
    This is the coach's persistent memory across runs: most calls return nothing,
    and only real signal accumulates in state['coach_notes'] (capped at 20,
    oldest dropped first) instead of every run starting from a blank slate."""
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 200,
        "thinking": {"type": "disabled"},
        "system": (
            "You extract long-term coaching memory for Roee Shor from one automated message. "
            "Given the message just sent and the data behind it, decide if there is ONE short, "
            "durable, non-obvious pattern worth remembering weeks from now - a recurring tendency, "
            "how he responds to a type of session, a real recurring constraint. Not a one-off stat "
            "from this week, and not anything generically true of any athlete. If nothing qualifies, "
            "output exactly: NONE. Otherwise output ONE plain sentence, no preamble, no quotes."
        ),
        "messages": [{
            "role": "user",
            "content": f"CONTEXT: {context_label}\n\nMESSAGE SENT:\n{message_text}\n\nDATA (JSON):\n"
                       f"{json.dumps(data, ensure_ascii=False, default=str)}",
        }],
    }
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=body, timeout=60,
        )
        r.raise_for_status()
        text = "".join(b["text"] for b in r.json()["content"] if b["type"] == "text").strip()
    except Exception:
        print("durable-note extraction failed:", file=sys.stderr)
        traceback.print_exc()
        return
    if not text or text.strip().upper() == "NONE":
        return
    state.setdefault("coach_notes", [])
    state["coach_notes"].append({"date": datetime.now(TZ).date().isoformat(), "note": text})
    state["coach_notes"] = state["coach_notes"][-20:]


# ---------------------------------------------------------------------------
# Strava
# ---------------------------------------------------------------------------

_strava_access_token = None


def strava_access_token():
    global _strava_access_token
    if _strava_access_token:
        return _strava_access_token
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    _strava_access_token = r.json()["access_token"]
    return _strava_access_token


def strava_get(path, params=None):
    headers = {"Authorization": f"Bearer {strava_access_token()}"}
    r = requests.get(f"https://www.strava.com/api/v3/{path}", headers=headers, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def strava_list_activities(after_epoch=None, per_page=30, page=1):
    params = {"per_page": per_page, "page": page}
    if after_epoch:
        params["after"] = after_epoch
    return strava_get("athlete/activities", params)


def strava_get_activity(activity_id):
    return strava_get(f"activities/{activity_id}", {"include_all_efforts": "true"})


def strava_get_gear(gear_id):
    return strava_get(f"gear/{gear_id}")


def _mps_to_pace_per_km(speed_mps):
    if not speed_mps:
        return None
    sec_per_km = 1000 / speed_mps
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}/km"


def summarize_activity_detail(detail):
    """Compact, LLM-friendly extraction of the per-lap/per-km structure from a
    Strava DetailedActivity - the raw laps/splits_metric/segment_efforts objects
    are full of nested map/segment metadata that just adds noise and buries the
    numbers that actually matter for a rep-by-rep or per-km breakdown."""
    out = {
        "name": detail.get("name"),
        "sport_type": detail.get("sport_type"),
        "distance_km": round((detail.get("distance") or 0) / 1000, 2),
        "moving_time_s": detail.get("moving_time"),
        "elapsed_time_s": detail.get("elapsed_time"),
        "average_heartrate": detail.get("average_heartrate"),
        "max_heartrate": detail.get("max_heartrate"),
        "average_pace": _mps_to_pace_per_km(detail.get("average_speed")),
        "total_elevation_gain_m": detail.get("total_elevation_gain"),
        "suffer_score": detail.get("suffer_score"),
        "description": detail.get("description"),
    }
    laps = detail.get("laps") or []
    if laps:
        out["laps"] = [{
            "lap": i + 1,
            "distance_m": round(lp.get("distance", 0)),
            "time_s": lp.get("moving_time"),
            "pace": _mps_to_pace_per_km(lp.get("average_speed")),
            "avg_hr": lp.get("average_heartrate"),
            "max_hr": lp.get("max_heartrate"),
        } for i, lp in enumerate(laps)]
    splits = detail.get("splits_metric") or []
    if splits:
        out["km_splits"] = [{
            "km": sp.get("split"),
            "time_s": sp.get("moving_time"),
            "pace": _mps_to_pace_per_km(sp.get("average_speed")),
            "avg_hr": sp.get("average_heartrate"),
            "elevation_diff_m": sp.get("elevation_difference"),
        } for sp in splits]
    best_efforts = detail.get("best_efforts") or []
    if best_efforts:
        out["best_efforts"] = [{"name": be.get("name"), "time_s": be.get("elapsed_time")} for be in best_efforts]
    return out


# ---------------------------------------------------------------------------
# athletedata (Garmin signals + TrainingPeaks plan + cross-source load/PMC
# analytics, via athletedata's MCP server over plain streamable-HTTP)
# ---------------------------------------------------------------------------

ATHLETEDATA_URL = f"https://mcp.athletedata.health/mcp?apiKey={ATHLETEDATA_API_KEY}"

_athletedata_call_id = 0


def athletedata_call(tool_name, arguments=None):
    """Call one athletedata MCP tool. This is a plain JSON-RPC 2.0 POST to a
    streamable-HTTP MCP endpoint (auth via the apiKey query param) - no session
    handshake or MCP SDK needed, confirmed against the live server. Raises on
    transport/tool errors; use athletedata_safe() at call sites that should
    degrade gracefully instead."""
    global _athletedata_call_id
    _athletedata_call_id += 1
    r = requests.post(
        ATHLETEDATA_URL,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": _athletedata_call_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        },
        timeout=30,
    )
    r.raise_for_status()
    payload = None
    for line in r.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[len("data:"):].strip())
            break
    if payload is None:
        raise RuntimeError(f"athletedata: no data line in response for {tool_name}: {r.text[:300]}")
    if "error" in payload:
        raise RuntimeError(f"athletedata tool {tool_name} error: {payload['error']}")
    result = payload["result"]
    if result.get("isError"):
        raise RuntimeError(f"athletedata tool {tool_name} returned isError: {result}")
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (json.JSONDecodeError, TypeError):
                return block["text"]
    return result


def athletedata_safe(tool_name, arguments=None):
    """Call an athletedata tool, returning None on any failure. Unlike the old
    garminconnect-based garmin_safe(), each call is an independent HTTP request -
    one tool erroring doesn't blank out every other athletedata call for the rest
    of this run the way a single Garmin login failure used to."""
    try:
        return athletedata_call(tool_name, arguments)
    except Exception:
        print(f"athletedata call {tool_name} failed:", file=sys.stderr)
        traceback.print_exc()
        return None


_planned_workouts_cache = {}


def planned_workout_for_date(iso_date):
    """get_planned_workouts (TrainingPeaks' forward plan, Terra-bridged through
    athletedata) returns a date-range window rather than a single day, so fetch
    and cache the window covering iso_date and reuse it for later lookups in the
    same run instead of one call per date."""
    if iso_date not in _planned_workouts_cache:
        d = date.fromisoformat(iso_date)
        start = (d - timedelta(days=10)).isoformat()
        end = (d + timedelta(days=14)).isoformat()
        result = athletedata_safe("get_planned_workouts", {"start_date": start, "end_date": end})
        for s in (result or {}).get("sessions") or []:
            _planned_workouts_cache[s["date"]] = s
    return _planned_workouts_cache.get(iso_date)


_daily_metrics_cache = {}


def daily_metrics_row_for_date(iso_date, context_days=1):
    """Return the get_daily_metrics row for iso_date (athletedata's merged-across-
    providers HRV/RHR/sleep/readiness/ACWR/monotony/injury-risk/CTL/ATL/TSB rollup
    for that day), fetching+caching a small window around it if not already cached."""
    if iso_date not in _daily_metrics_cache:
        d = date.fromisoformat(iso_date)
        start = (d - timedelta(days=context_days)).isoformat()
        end = (d + timedelta(days=context_days)).isoformat()
        result = athletedata_safe("get_daily_metrics", {"start": start, "end": end})
        for row in (result or {}).get("rows") or []:
            _daily_metrics_cache[row["date"]] = row
    return _daily_metrics_cache.get(iso_date)


def daily_metrics_range(start_date, end_date):
    """Return {date: row} for [start_date, end_date], fetching+caching whichever
    dates in that range aren't already cached."""
    d0, d1 = date.fromisoformat(start_date), date.fromisoformat(end_date)
    needed = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    missing = [d for d in needed if d not in _daily_metrics_cache]
    if missing:
        result = athletedata_safe("get_daily_metrics", {"start": min(missing), "end": max(missing)})
        for row in (result or {}).get("rows") or []:
            _daily_metrics_cache[row["date"]] = row
    return {d: _daily_metrics_cache[d] for d in needed if d in _daily_metrics_cache}


def athletedata_load_context():
    """Current (as-of-today) cross-source load/fitness signals: athletedata's own
    ACWR/monotony/ramp-rate + load-anomaly flag (NOT a validated injury predictor -
    see its disclaimer) + PMC (CTL/ATL/TSB). This is the athletedata leg of the
    three-way OVERTRAINING CHECK blend, alongside the existing Strava-only
    acwr_context() and per-activity HR-drift/plan-compliance checks."""
    return {
        "load_balance": athletedata_safe("get_load_balance", {"days": 28}),
        "load_flag": athletedata_safe("get_load_flag"),
        "pmc_status": athletedata_safe("get_pmc_status"),
    }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _check_telegram_response(r):
    """Telegram returns HTTP 200 with {"ok": false, ...} on many failures (bad chat_id,
    message too long, malformed text), so raise_for_status() alone is not enough."""
    ok = False
    try:
        ok = r.json().get("ok", False)
    except Exception:
        pass
    if not r.ok or not ok:
        raise RuntimeError(f"Telegram send failed: HTTP {r.status_code} body={r.text[:500]}")


TELEGRAM_MAX_LEN = 4096


def _chunk_text(text, limit=TELEGRAM_MAX_LEN - 100):
    """Split on paragraph/line boundaries so a long composed message (e.g. weekly
    summary) sends as multiple messages instead of failing Telegram's 4096-char cap."""
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def tg_send_message(text):
    for chunk in _chunk_text(text):
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
            timeout=30,
        )
        _check_telegram_response(r)


def tg_send_photo(path):
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID},
            files={"photo": f},
            timeout=60,
        )
    _check_telegram_response(r)


def tg_get_updates(offset):
    r = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
        params={"offset": offset, "_": random.randint(0, 10 ** 9)},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("result", [])


# ---------------------------------------------------------------------------
# Anthropic (LLM composition)
# ---------------------------------------------------------------------------

def llm_compose(task_instructions, data):
    body = {
        "model": "claude-sonnet-5",
        "max_tokens": 4096,
        "thinking": {"type": "disabled"},
        "system": STYLE_GUIDE,
        "messages": [{
            "role": "user",
            "content": f"{task_instructions}\n\nDATA (JSON):\n{json.dumps(data, ensure_ascii=False, default=str)}",
        }],
    }
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=120,
    )
    r.raise_for_status()
    resp = r.json()
    text = "".join(b["text"] for b in resp["content"] if b["type"] == "text").strip()
    if not text:
        raise RuntimeError(f"LLM returned empty text (stop_reason={resp.get('stop_reason')}); refusing to send an empty message")
    return text


# ---------------------------------------------------------------------------
# Deterministic math helpers
# ---------------------------------------------------------------------------

def acwr_context(now):
    """Sum relative_effort (Strava suffer_score) trailing 7 days vs preceding 7 days."""
    after_epoch = int((now - timedelta(days=14)).timestamp())
    acts = []
    page = 1
    while True:
        batch = strava_list_activities(after_epoch=after_epoch, per_page=100, page=page)
        if not batch:
            break
        acts.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    trailing, preceding = 0, 0
    days_with_activity = set()
    for a in acts:
        start = datetime.fromisoformat(a["start_date_local"].replace("Z", "")).replace(tzinfo=TZ)
        days_ago = (now.date() - start.date()).days
        effort = a.get("suffer_score") or 0
        if 0 <= days_ago < 7:
            trailing += effort
            days_with_activity.add(start.date())
        elif 7 <= days_ago < 14:
            preceding += effort
        if 0 <= days_ago < 14:
            days_with_activity.add(start.date())

    acwr = round(trailing / preceding, 2) if preceding else None
    no_rest_day = len(days_with_activity) >= 14
    return {"trailing_7d_effort": trailing, "preceding_7d_effort": preceding, "acwr": acwr,
            "zero_full_rest_day_in_14d": no_rest_day}


# Standard race/best-effort distances, mapped to meters. Keys after the lookup
# normalization below (lowercase, spaces/hyphens -> "_") match the exact names
# Strava's DetailedActivity.best_efforts uses ("10 mile" -> "10_mile",
# "Half-Marathon" -> "half_marathon", etc). Only 3k+ efforts are used as a Riegel
# reference in strava_race_predictions() - Riegel extrapolates badly from a short
# sprint effort out to marathon distance; "3k" itself is never a real Strava
# best_efforts name (their fixed set jumps 2 mile -> 5k) but stays as a valid
# *target* distance to predict for.
STANDARD_DISTANCES_M = {
    "3k": 3000, "5k": 5000, "10k": 10000, "15k": 15000, "10_mile": 16090.3,
    "20k": 20000, "half_marathon": 21097.5, "30k": 30000, "marathon": 42195,
}


def riegel_predict(reference_time_s, reference_distance_m, target_distance_m, exponent=1.06):
    return reference_time_s * (target_distance_m / reference_distance_m) ** exponent


def strava_race_predictions(now, lookback_days=120, max_runs_scanned=25):
    """Race-time predictions from Roee's own real Strava data only - Riegel's
    formula off his single best recent effort at 3k+, not a third-party model.
    (athletedata's get_performance_estimates was tried first, but that's
    athletedata's own cross-source algorithm, not a Garmin/Strava passthrough -
    dropped in favor of this after the numbers didn't hold up.) Garmin's own
    VO2max/lactate-threshold estimate (garmin_get_user_metrics, genuinely
    Garmin-computed) is fetched separately as a cross-check, not blended in here."""
    after_epoch = int((now - timedelta(days=lookback_days)).timestamp())
    acts = []
    page = 1
    while True:
        batch = strava_list_activities(after_epoch=after_epoch, per_page=100, page=page)
        if not batch:
            break
        acts.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    runs = [a for a in acts if a.get("sport_type") == "Run"][:max_runs_scanned]

    best = None  # (equivalent_10k_time_s, name, distance_m, time_s, date)
    for a in runs:
        try:
            detail = strava_get_activity(a["id"])
        except Exception:
            continue
        for be in detail.get("best_efforts") or []:
            key = (be.get("name") or "").lower().replace(" ", "_").replace("-", "_")
            dist_m = STANDARD_DISTANCES_M.get(key)
            t = be.get("elapsed_time")
            if not dist_m or dist_m < 3000 or not t:
                continue
            equiv_10k = riegel_predict(t, dist_m, 10000)
            if best is None or equiv_10k < best[0]:
                best = (equiv_10k, be.get("name"), dist_m, t, a["start_date_local"][:10])

    if not best:
        return None
    _, name, dist_m, t, when = best
    return {
        "reference_effort": name,
        "reference_distance_m": dist_m,
        "reference_time_s": t,
        "reference_date": when,
        "method": "Riegel's formula (exponent 1.06), extrapolated from this single best real Strava effort - not a blended or third-party model",
        "predicted_times_s": {label: round(riegel_predict(t, dist_m, d)) for label, d in STANDARD_DISTANCES_M.items()},
    }


def find_benchmark_comparison(activity_detail, sport_type, before_epoch):
    """Look at up to 2 recent same-sport activities for a shared segment_id, return best comparison."""
    segs = activity_detail.get("segment_efforts") or []
    if not segs:
        return None
    recent = strava_list_activities(after_epoch=before_epoch - 60 * 86400, per_page=10)
    candidates = [a for a in recent if a.get("sport_type") == sport_type and a["id"] != activity_detail["id"]][:3]
    for cand in candidates:
        cand_detail = strava_get_activity(cand["id"])
        cand_segs = {s["segment"]["id"]: s for s in (cand_detail.get("segment_efforts") or [])}
        for s in segs:
            sid = s["segment"]["id"]
            if sid in cand_segs:
                prior = cand_segs[sid]
                return {
                    "segment_name": s["segment"]["name"],
                    "prior_date": cand_detail["start_date_local"],
                    "prior_time_s": prior["elapsed_time"],
                    "prior_hr": prior.get("average_heartrate"),
                    "today_time_s": s["elapsed_time"],
                    "today_hr": s.get("average_heartrate"),
                }
    return None


def shoe_mileage_check(activity, state):
    """Read-only: does NOT mutate state. Caller commits alert keys to
    state['shoe_alerts_sent'] only after the message carrying them is confirmed sent,
    so a failed send doesn't silently swallow the alert on retry."""
    gear_id = activity.get("gear_id")
    if not gear_id:
        return None, [], []
    gear = strava_get_gear(gear_id)
    km = round(gear["distance"] / 1000, 1)
    alerts = []
    new_keys = []
    for threshold in (500, 700):
        key = f"{gear_id}_{threshold}"
        if km >= threshold and key not in state["shoe_alerts_sent"]:
            alerts.append({"threshold": threshold, "brand": gear.get("brand_name"), "model": gear.get("model_name"), "km": km})
            new_keys.append(key)
    return {"brand": gear.get("brand_name"), "model": gear.get("model_name"), "km": km}, alerts, new_keys


# ---------------------------------------------------------------------------
# Step 1: 5am morning brief
# ---------------------------------------------------------------------------

def step1_morning_brief(state, now):
    today = now.date().isoformat()
    if now.hour != 5 or state.get("last_morning_checkin_date") == today:
        return
    workout = planned_workout_for_date(today)
    if not workout:
        state["last_morning_checkin_date"] = today
        return

    readiness = athletedata_safe("get_readiness_today")
    daily_row = daily_metrics_row_for_date(today)
    stress = athletedata_safe("garmin_get_stress", {"start_date": today, "end_date": today})

    # pull recent activities of similar name for pace-lookup context
    recent = strava_list_activities(after_epoch=int((now - timedelta(days=45)).timestamp()), per_page=30)
    title = workout.get("title", "")
    keyword = next((k for k in ["פארטלק", "אינטרוולים", "אימון עליות", "ריצת נפח", "שחרור"] if k in title), None)
    similar = [a for a in recent if keyword and keyword in a.get("name", "")][:3]
    similar_detail = [summarize_activity_detail(strava_get_activity(a["id"])) for a in similar]

    data = {
        "workout_title": title,
        "workout_structure": workout,
        "readiness_today": readiness,
        "daily_metrics_today": daily_row,
        "stress_and_body_battery": stress,
        "similar_recent_sessions": similar_detail,
        "long_term_notes": long_term_notes(state),
    }
    text = llm_compose(
        "Compose the 5am daily training brief per the morning-brief instructions in your system prompt. "
        "Define a concrete goal pace for every segment of the main set.",
        data,
    )
    tg_send_message(text)
    state["last_morning_checkin_date"] = today


# ---------------------------------------------------------------------------
# Step 2: answer Telegram questions
# ---------------------------------------------------------------------------

def step2_telegram_qa(state, now):
    updates = tg_get_updates(state["last_telegram_update_id"] + 1)
    for u in updates:
        msg = u.get("message")
        if not msg or str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            state["last_telegram_update_id"] = u["update_id"]
            continue
        text = msg.get("text", "")
        if not text:
            state["last_telegram_update_id"] = u["update_id"]
            continue
        recent = strava_list_activities(after_epoch=int((now - timedelta(days=21)).timestamp()), per_page=30)
        # give the LLM full lap/split detail for recent runs (last 14 days, capped) so it can
        # actually answer pace/HR-per-segment questions, not just session-wide averages
        run_candidates = [a for a in recent if a.get("sport_type") == "Run"
                           and (now.date() - datetime.fromisoformat(a["start_date_local"].replace("Z", "")).date()).days <= 14][:15]
        detailed_runs = []
        for a in run_candidates:
            try:
                detailed_runs.append(summarize_activity_detail(strava_get_activity(a["id"])))
            except Exception:
                traceback.print_exc()
        data = {
            "question": text,
            "today": now.date().isoformat(),
            "recent_activities_summary": recent,
            "recent_runs_with_lap_and_split_detail": detailed_runs,
            "long_term_notes": long_term_notes(state),
        }
        reply = llm_compose(
            "Answer this Telegram message from Roee. If it references a specific past workout/day, "
            "identify the matching activity from recent_activities_summary (by weekday/relative date/sport/keywords), "
            "then use the matching entry in recent_runs_with_lap_and_split_detail (matched by name/date) for a real "
            "rep-by-rep or per-km answer if the question is about pace/HR/structure - not just session averages. "
            "If generic (hi/thanks), reply briefly and warmly with no analysis. "
            "If you can't confidently identify the activity, ask one short clarifying question.",
            data,
        )
        tg_send_message(reply)
        # only advance past this update once the reply is confirmed sent, so a failure
        # mid-batch retries just the unsent messages, not the whole batch
        state["last_telegram_update_id"] = u["update_id"]


# ---------------------------------------------------------------------------
# Step 3: new activity push
# ---------------------------------------------------------------------------

def step3_new_activity_push(state, now):
    recent = strava_list_activities(after_epoch=int((now - timedelta(days=3)).timestamp()), per_page=10)
    recent.sort(key=lambda a: a["start_date_local"])
    last_id = state.get("last_activity_id")
    new_acts = recent if last_id is None else [a for a in recent if int(a["id"]) > int(last_id)]

    for a in new_acts:
        detail = strava_get_activity(a["id"])
        acwr = acwr_context(now)
        before_epoch = int(datetime.fromisoformat(a["start_date_local"].replace("Z", "")).replace(tzinfo=TZ).timestamp())
        benchmark = find_benchmark_comparison(detail, a.get("sport_type"), before_epoch)
        shoe_info, shoe_alerts, new_shoe_alert_keys = shoe_mileage_check(a, state)

        activity_date = a["start_date_local"][:10]
        workout = planned_workout_for_date(activity_date)

        daily_row = daily_metrics_row_for_date(activity_date)
        stress = athletedata_safe("garmin_get_stress", {"start_date": activity_date, "end_date": activity_date})
        load_context = athletedata_load_context()
        # race predictions are deliberately NOT fetched per-activity - the style guide
        # already scopes FITNESS CONTEXT to "occasionally", and computing them means
        # scanning ~25 runs' worth of Strava detail (see strava_race_predictions),
        # which belongs in the weekly summary, not every single new-activity push

        # easy-run HR drift baseline
        hr_drift = None
        name = a.get("name", "")
        if any(k in name for k in ["שחרור", "התאוששות"]) and a.get("average_heartrate") and a.get("average_speed"):
            baseline = [
                x for x in strava_list_activities(after_epoch=int((now - timedelta(days=30)).timestamp()), per_page=20)
                if x["id"] != a["id"] and any(k in x.get("name", "") for k in ["שחרור", "התאוששות"])
            ][:4]
            baseline_detail = [strava_get_activity(x["id"]) for x in baseline]
            ratios = [
                bd["average_heartrate"] / bd["average_speed"]
                for bd in baseline_detail if bd.get("average_heartrate") and bd.get("average_speed")
            ]
            if ratios:
                today_ratio = a["average_heartrate"] / a["average_speed"]
                baseline_avg = sum(ratios) / len(ratios)
                hr_drift = {"today_ratio": today_ratio, "baseline_avg_ratio": baseline_avg,
                            "pct_diff": round((today_ratio - baseline_avg) / baseline_avg * 100, 1)}

        data = {
            "activity": a,
            "activity_detail": summarize_activity_detail(detail),
            "acwr_context": acwr,
            "benchmark_segment_comparison": benchmark,
            "shoe_info": shoe_info,
            "shoe_mileage_alerts": shoe_alerts,
            "coach_plan_for_this_date": workout,
            "recovery_context": {
                "daily_metrics_on_activity_date": daily_row,
                "stress_and_body_battery_on_activity_date": stress,
            },
            "athletedata_load_context": load_context,
            "easy_run_hr_drift": hr_drift,
            "long_term_notes": long_term_notes(state),
        }
        text = llm_compose(
            "Compose the per-activity analysis message for this newly-completed activity, per the "
            "per-activity-message instructions in your system prompt.",
            data,
        )
        tg_send_message(text)
        # only commit these once the send is confirmed successful (see shoe_mileage_check docstring)
        state["shoe_alerts_sent"].extend(new_shoe_alert_keys)
        state["last_activity_id"] = str(a["id"])


# ---------------------------------------------------------------------------
# Step 4: Sunday weekly summary
# ---------------------------------------------------------------------------

def make_weekly_charts(activities, daily_rows_14d, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update({"font.size": 12, "axes.titlesize": 15, "axes.titleweight": "bold",
                          "axes.grid": True, "grid.alpha": 0.3, "figure.facecolor": "white",
                          "axes.facecolor": "white", "legend.frameon": False})

    # weekly load bar chart, last 13 weeks
    by_week = {}
    for a in activities:
        start = datetime.fromisoformat(a["start_date_local"].replace("Z", "")).date()
        week_start = start - timedelta(days=start.weekday())
        by_week.setdefault(week_start, 0)
        by_week[week_start] += a.get("suffer_score") or 0
    weeks = sorted(by_week.keys())[-13:]
    loads = [by_week[w] for w in weeks]
    acwrs = [None] + [round(loads[i] / loads[i - 1], 2) if loads[i - 1] else None for i in range(1, len(loads))]

    def band_color(a):
        if a is None:
            return "#9aa0a6"
        if a < 0.8:
            return "#4c8bf5"
        if a <= 1.3:
            return "#34a853"
        if a <= 1.5:
            return "#fbbc04"
        return "#ea4335"

    fig, ax = plt.subplots(figsize=(11, 6))
    x = np.arange(len(weeks))
    ax.bar(x, loads, color=[band_color(a) for a in acwrs], width=0.65, zorder=3)
    if len(loads) >= 4:
        roll = np.convolve(loads, np.ones(4) / 4, mode="valid")
        ax.plot(x[3:], roll, color="#202124", linewidth=2.5, marker="o", markersize=4, label="4-week rolling avg")
    for xi, l, a in zip(x, loads, acwrs):
        ax.annotate(f"{a:.2f}" if a is not None else "-", (xi, l), textcoords="offset points",
                    xytext=(0, 6), ha="center", fontsize=9, color="#5f6368")
    ax.set_xticks(x)
    ax.set_xticklabels([w.strftime("%b %d") for w in weeks], rotation=30, ha="right")
    ax.set_ylabel("Weekly relative effort (sum)")
    ax.set_title("Weekly Training Load — last 13 weeks (ACWR labeled above bars)")
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    path_a = os.path.join(out_dir, "chart_a_weekly_load.png")
    fig.savefig(path_a, dpi=150)
    plt.close(fig)

    # HRV + RHR, last 14 days (single athletedata get_daily_metrics window, passed in
    # by the caller and already cached/deduped there - not one call per day)
    days = [(datetime.now(TZ).date() - timedelta(days=i)) for i in range(13, -1, -1)]
    hrv_vals, rhr_vals = [], []
    for d in days:
        row = daily_rows_14d.get(d.isoformat())
        hrv_vals.append(row.get("hrv") if row else None)
        rhr_vals.append(row.get("restingHr") if row else None)

    fig, ax1 = plt.subplots(figsize=(11, 6))
    ax2 = ax1.twinx()
    ax2.grid(False)
    xd = np.arange(len(days))
    if any(v is not None for v in hrv_vals):
        ax1.plot(xd, hrv_vals, color="#1a73e8", marker="o", linewidth=2.5, label="HRV (ms)")
    if any(v is not None for v in rhr_vals):
        ax2.plot(xd, rhr_vals, color="#ea4335", marker="s", linewidth=2, linestyle="--", label="Resting HR (bpm)")
    ax1.set_xticks(xd)
    ax1.set_xticklabels([d.strftime("%b %d") for d in days], rotation=30, ha="right")
    ax1.set_ylabel("HRV, ms", color="#1a73e8")
    ax2.set_ylabel("Resting HR, bpm", color="#ea4335")
    ax1.set_title("HRV & Resting HR — last 14 days")
    fig.tight_layout()
    path_c = os.path.join(out_dir, "chart_c_hrv_rhr.png")
    fig.savefig(path_c, dpi=150)
    plt.close(fig)

    return [path_a, path_c]


def step4_weekly_summary(state, now):
    today = now.date().isoformat()
    if now.weekday() != 6 or not (7 <= now.hour <= 11) or state.get("last_weekly_summary_date") == today:
        return

    after_epoch = int((now - timedelta(weeks=13)).timestamp())
    all_acts = []
    page = 1
    while True:
        batch = strava_list_activities(after_epoch=after_epoch, per_page=100, page=page)
        if not batch:
            break
        all_acts.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    this_week = [a for a in all_acts if (now.date() - datetime.fromisoformat(a["start_date_local"].replace("Z", "")).date()).days < 7]
    last_week = [a for a in all_acts if 7 <= (now.date() - datetime.fromisoformat(a["start_date_local"].replace("Z", "")).date()).days < 14]

    next_week_workouts = []
    for i in range(7):
        d = (now.date() + timedelta(days=i)).isoformat()
        w = planned_workout_for_date(d)
        if w:
            next_week_workouts.append({"date": d, "title": w.get("title")})

    load_context = athletedata_load_context()
    # race predictions: Strava-only (Riegel off Roee's own best real effort), plus
    # Garmin's own VO2max/threshold as a separate cross-check - NOT athletedata's
    # get_performance_estimates, which is athletedata's own cross-source algorithm
    # rather than a Garmin/Strava passthrough and didn't hold up against real numbers
    race_predictions = strava_race_predictions(now)
    user_metrics = athletedata_safe("garmin_get_user_metrics", {
        "start_date": (now - timedelta(days=90)).date().isoformat(), "end_date": today,
    })

    gear_all = []
    seen_gear = set()
    for a in all_acts:
        gid = a.get("gear_id")
        if gid and gid not in seen_gear:
            seen_gear.add(gid)
            try:
                gear_all.append(strava_get_gear(gid))
            except Exception:
                pass

    daily_rows_14d = daily_metrics_range((now - timedelta(days=13)).date().isoformat(), today)

    tmp_dir = tempfile.mkdtemp()
    try:
        chart_paths = make_weekly_charts(all_acts, daily_rows_14d, tmp_dir)
    except Exception:
        traceback.print_exc()
        chart_paths = []

    acwr = acwr_context(now)
    data = {
        "this_week_activities": this_week,
        "last_week_activities": last_week,
        "acwr_context": acwr,
        "athletedata_load_context": load_context,
        "race_predictions": race_predictions,
        "user_metrics_90d": user_metrics,
        "next_week_plan": next_week_workouts,
        "shoes": gear_all,
        "long_term_notes": long_term_notes(state),
    }
    text = llm_compose(
        "Compose the Sunday weekly summary message per the weekly-summary instructions in your system prompt.",
        data,
    )

    for p in chart_paths:
        try:
            tg_send_photo(p)
        except Exception:
            traceback.print_exc()
    tg_send_message(text)
    state["last_weekly_summary_date"] = today
    maybe_extract_durable_note(state, "Sunday weekly summary", data, text)


# ---------------------------------------------------------------------------
# Test ping - manual liveness/integration check, triggered via workflow_dispatch
# ---------------------------------------------------------------------------

def send_test_ping():
    """Sends one Telegram message confirming the bot is reachable, and actually
    exercises the Strava and athletedata integrations rather than just replying
    with a static string - so a successful ping is real evidence those secrets
    and connections work, not just that the process could start."""
    lines = ["Coach is live and reachable."]

    try:
        strava_access_token()
        lines.append("Strava: OK (token refreshed)")
    except Exception:
        lines.append("Strava: FAILED to refresh token - check STRAVA_* secrets")

    readiness = athletedata_safe("get_readiness_today")
    if readiness and readiness.get("readiness_score") is not None:
        lines.append(f"athletedata: OK (readiness {readiness['readiness_score']}, verdict {readiness.get('verdict')})")
    else:
        lines.append("athletedata: no data returned - check ATHLETEDATA_API_KEY")

    tg_send_message("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if os.environ.get("PING_TEST", "").strip().lower() == "true":
        send_test_ping()
        return
    now = datetime.now(TZ)
    state = load_state()
    try:
        step1_morning_brief(state, now)
    except Exception:
        traceback.print_exc()
    try:
        step2_telegram_qa(state, now)
    except Exception:
        traceback.print_exc()
    try:
        step3_new_activity_push(state, now)
    except Exception:
        traceback.print_exc()
    try:
        step4_weekly_summary(state, now)
    except Exception:
        traceback.print_exc()
    save_state(state)


if __name__ == "__main__":
    main()
