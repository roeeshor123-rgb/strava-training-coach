"""
Automated Strava + Garmin training coach for Roee Shor, messaging on Telegram.
Runs as a GitHub Actions cron job (see .github/workflows/coach.yml).

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
GARMIN_EMAIL = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

STYLE_GUIDE = """You are Roee Shor's automated Strava + Garmin running/strength coach, writing him a Telegram message. Roee trains running (easy runs, volume runs, intervals, hill repeats, fartlek) plus regular weight training, in Tel Aviv. Session names are often in Hebrew (e.g. ריצת נפח = volume run, ריצת שחרור = easy/shakeout run, אינטרוולים = intervals, אימון עליות = hill repeats, פארטלק = fartlek, אימון התאוששות = recovery session) - keep Hebrew names as-is when referencing them.

Write plain text only, no markdown asterisks/headers. Never generic filler - every line must reflect the athlete's actual numbers from the JSON data you're given. Be direct, specific, and numeric.

For a per-activity analysis message, use this structure (omit any optional section that doesn't apply, given the data):
[optional WARNING BENCHMARK ALERT line if a same-segment comparison shows both slower time AND higher HR than the prior instance]
[optional WARNING HR DRIFT line if easy-run HR-at-pace is meaningfully elevated vs recent baseline]
[optional PLAN MISMATCH line if actual training diverged meaningfully from the coach's prescribed session]
An emoji + activity name/type + date as a header line (running emoji for runs, weights emoji for strength)
Grade: X/10 - one-line verdict

RECOVERY CONTEXT (Garmin, before the run) - only if Garmin data was available
COACH'S PLAN - only if a scheduled workout existed for this date, prescribed vs actual
SUMMARY - 2-3 sentences on what the session was and the headline finding
SAME-SEGMENT TREND - only if a genuine repeated-segment comparison exists, prior vs today
GOOD PARTS - 2-3 specific numeric bullets
WATCH FOR - 1-3 specific bullets
OVERTRAINING CHECK: Low/Moderate/High - blend ACWR + Garmin signals + rest-day pattern + HR drift + plan-compliance into ONE verdict with concrete numbers plus one actionable recommendation
FITNESS CONTEXT - only occasionally (weekly, or when VO2max/predictions changed) - VO2max, race predictions

For the 5am morning brief: render the prescribed session as warmup / main set (with concrete goal paces per segment - convert pace-zone targets to min/km directly, and for HR-zone or effort-based segments like fartlek surges, use the provided historical-pace-lookup data to state a concrete pace range) / cooldown, then a readiness-based go/adjust call, ending with one "TODAY'S GOAL" headline line naming the single most important numeric target for the session.

For the weekly summary: cover the week's sessions, the 3-month trend read, standout sessions, shoe mileage, VO2max/race predictions, next week's plan preview, and 1-2 concrete suggestions, using the OVERTRAINING VERDICT blend described above.

For a Telegram Q&A reply: answer directly and specifically using the matched activity's data and any Garmin context provided. If it's just a generic greeting, reply briefly and warmly with no analysis.

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
        }
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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


# ---------------------------------------------------------------------------
# Garmin
# ---------------------------------------------------------------------------

_garmin_client = None
_garmin_failed = False


def garmin():
    """Returns a logged-in Garmin client, or None if login fails (never raises)."""
    global _garmin_client, _garmin_failed
    if _garmin_client is not None:
        return _garmin_client
    if _garmin_failed:
        return None
    try:
        from garminconnect import Garmin
        client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        client.login()
        _garmin_client = client
        return client
    except Exception:
        print("Garmin login failed, skipping Garmin enrichment for this run:", file=sys.stderr)
        traceback.print_exc()
        _garmin_failed = True
        return None


def garmin_safe(fn, *args, **kwargs):
    """Call a garmin client method, returning None on any failure."""
    client = garmin()
    if client is None:
        return None
    try:
        return getattr(client, fn)(*args, **kwargs)
    except Exception:
        print(f"Garmin call {fn} failed:", file=sys.stderr)
        traceback.print_exc()
        return None


def garmin_scheduled_workout_for_date(iso_date):
    d = date.fromisoformat(iso_date)
    months = {(d.year, d.month)}
    if d.day <= 3:
        prev = d.replace(day=1) - timedelta(days=1)
        months.add((prev.year, prev.month))
    if d.day >= 28:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        months.add((nxt.year, nxt.month))
    for (y, m) in months:
        cal = garmin_safe("get_scheduled_workouts", y, m)
        if not cal:
            continue
        for item in cal.get("calendarItems", []):
            if item.get("itemType") == "workout" and item.get("date") == iso_date:
                return item
    return None


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
        "max_tokens": 2000,
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
    return "".join(b["text"] for b in r.json()["content"] if b["type"] == "text").strip()


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
    workout = garmin_scheduled_workout_for_date(today)
    if not workout:
        state["last_morning_checkin_date"] = today
        return

    detail = garmin_safe("get_workout_by_id", workout["workoutId"])
    readiness = garmin_safe("get_training_readiness", today)
    hrv = garmin_safe("get_hrv_data", today)
    sleep = garmin_safe("get_sleep_data", today)
    bb = garmin_safe("get_body_battery", today)

    # pull recent activities of similar name for pace-lookup context
    recent = strava_list_activities(after_epoch=int((now - timedelta(days=45)).timestamp()), per_page=30)
    title = workout.get("title", "")
    keyword = next((k for k in ["פארטלק", "אינטרוולים", "אימון עליות", "ריצת נפח", "שחרור"] if k in title), None)
    similar = [a for a in recent if keyword and keyword in a.get("name", "")][:3]
    similar_detail = [strava_get_activity(a["id"]) for a in similar]

    data = {
        "workout_title": title,
        "workout_structure": detail,
        "readiness": readiness[0] if readiness else None,
        "hrv": hrv.get("hrvSummary") if hrv else None,
        "sleep_hours": round(sleep["dailySleepDTO"]["sleepTimeSeconds"] / 3600, 1) if sleep and sleep.get("dailySleepDTO") else None,
        "body_battery": bb,
        "similar_recent_sessions": similar_detail,
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
        data = {"question": text, "today": now.date().isoformat(), "recent_activities": recent}
        reply = llm_compose(
            "Answer this Telegram message from Roee. If it references a specific past workout/day, "
            "identify the matching activity from recent_activities (by weekday/relative date/sport/keywords) "
            "and answer using its data. If generic (hi/thanks), reply briefly and warmly with no analysis. "
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
        workout = garmin_scheduled_workout_for_date(activity_date)
        workout_detail = garmin_safe("get_workout_by_id", workout["workoutId"]) if workout else None

        readiness = garmin_safe("get_training_readiness", activity_date)
        hrv = garmin_safe("get_hrv_data", activity_date)
        sleep = garmin_safe("get_sleep_data", activity_date)
        bb = garmin_safe("get_body_battery", activity_date)
        training_status = garmin_safe("get_training_status", activity_date)
        race_pred = garmin_safe("get_race_predictions")

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
            "activity_detail": detail,
            "acwr_context": acwr,
            "benchmark_segment_comparison": benchmark,
            "shoe_info": shoe_info,
            "shoe_mileage_alerts": shoe_alerts,
            "coach_plan_for_this_date": workout_detail,
            "recovery_context": {
                "readiness": readiness[-1] if readiness else None,
                "hrv": hrv.get("hrvSummary") if hrv else None,
                "sleep_hours": round(sleep["dailySleepDTO"]["sleepTimeSeconds"] / 3600, 1) if sleep and sleep.get("dailySleepDTO") else None,
                "body_battery": bb,
                "training_status": training_status,
            },
            "race_predictions": race_pred,
            "easy_run_hr_drift": hr_drift,
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

def make_weekly_charts(activities, out_dir):
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

    # HRV + RHR, last 14 days
    days = [(datetime.now(TZ).date() - timedelta(days=i)) for i in range(13, -1, -1)]
    hrv_vals, rhr_vals = [], []
    for d in days:
        h = garmin_safe("get_hrv_data", d.isoformat())
        r = garmin_safe("get_rhr_day", d.isoformat())
        hrv_vals.append(h.get("hrvSummary", {}).get("lastNightAvg") if h else None)
        rhr_val = None
        try:
            rhr_val = r["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"][0]["value"]
        except Exception:
            pass
        rhr_vals.append(rhr_val)

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
        w = garmin_scheduled_workout_for_date(d)
        if w:
            next_week_workouts.append({"date": d, "title": w.get("title")})

    training_status = garmin_safe("get_training_status", today)
    race_pred = garmin_safe("get_race_predictions")

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

    tmp_dir = tempfile.mkdtemp()
    try:
        chart_paths = make_weekly_charts(all_acts, tmp_dir)
    except Exception:
        traceback.print_exc()
        chart_paths = []

    acwr = acwr_context(now)
    data = {
        "this_week_activities": this_week,
        "last_week_activities": last_week,
        "acwr_context": acwr,
        "training_status": training_status,
        "race_predictions": race_pred,
        "next_week_plan": next_week_workouts,
        "shoes": gear_all,
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
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
