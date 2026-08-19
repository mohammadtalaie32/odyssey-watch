#!/usr/bin/env python3
"""Watch Cineplex for The Odyssey in IMAX 70mm and alert when seats open up.

Polls Cineplex's public showtimes API (the same one www.cineplex.com calls) for a
given film at a given set of theatres, filters to a specific experience combo
(IMAX + 70mm by default), and fires notifications when something changes in a way
you'd actually want to know about:

  NEW_SHOWTIME  a showtime we've never seen before (a new date went on sale)
  REOPENED      a showtime that was sold out now has seats (someone cancelled)
  SEATS_UP      seats jumped by at least `seats_up_threshold` (bulk return)
  LOW_SEATS     seats dropped to/below `low_seats_threshold` (last chance)

Stdlib only. Python 3.8+.

  ./odyssey_watch.py --once            one poll, then exit (for cron/launchd)
  ./odyssey_watch.py --daemon          poll forever on config interval
  ./odyssey_watch.py --status          print current availability, no alerts
  ./odyssey_watch.py --test-notify     send a test alert through every notifier
"""

import argparse
import gzip
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
# Overridable so the same script works locally and on a CI runner, where the
# config is committed but the state lives in a restored cache directory.
CONFIG_PATH = os.environ.get("ODYSSEY_CONFIG") or os.path.join(HERE, "config.json")
STATE_PATH = os.environ.get("ODYSSEY_STATE") or os.path.join(HERE, "state.json")
LOG_PATH = os.environ.get("ODYSSEY_LOG") or os.path.join(HERE, "watch.log")

API_BASE = "https://apis.cineplex.com/prod/cpx/theatrical/api/v1"
TICKETING_BASE = "https://apis.cineplex.com/prod/ticketing/api/v1"
# Public key embedded in Cineplex's own web bundle; not a secret credential.
API_KEY = "dcdac5601d864addbc2675a2e96cb1f8"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Human-readable labels for the alert kinds, most urgent first.
ALERT_ORDER = ["REOPENED", "NEW_SHOWTIME", "SEATS_UP", "LOW_SEATS"]
ALERT_LABEL = {
    "REOPENED": "SEATS FREED UP",
    "NEW_SHOWTIME": "NEW SHOWTIME ON SALE",
    "SEATS_UP": "MORE SEATS AVAILABLE",
    "LOW_SEATS": "ALMOST GONE",
}


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Cineplex API
# --------------------------------------------------------------------------


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "Ocp-Apim-Subscription-Key": API_KEY,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    # The API gzips inconsistently, so sniff the magic bytes rather than trust the header.
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def api_get(path, params):
    return get_json(
        "%s/%s?%s" % (API_BASE, path.lstrip("/"), urllib.parse.urlencode(params))
    )


# --------------------------------------------------------------------------
# Seat maps
#
# The showtimes API only reports a total seat count, which says nothing about
# where those seats are. These two endpoints are what the seat-map preview page
# itself calls, and both are plain reads needing no cart, login or session:
#
#   /v1/theatre/{t}/showtime/{s}/seat-layout                  rows and seat ids
#   /v1/theatre/{t}/showtime/{s}/seat-availability?preview=1  id -> Available
#
# There is also a seat-availability-for-cart variant; it needs a checkout cart,
# and nothing here touches it.
# --------------------------------------------------------------------------


def seat_url(theatre_id, showtime_id, leaf):
    return "%s/theatre/%s/showtime/%s/%s" % (
        TICKETING_BASE, theatre_id, showtime_id, leaf
    )


def fetch_seat_rows(theatre_id, showtime_id):
    """Map every seat id in this auditorium to its row label.

    The layout is a property of the room, not the showing, so one fetch per
    theatre is reused for every showtime there.
    """
    data = get_json(seat_url(theatre_id, showtime_id, "seat-layout"))
    rows = {}
    for area in ("standardSeats", "dboxSeats", "balconySeats"):
        block = data.get(area) or {}
        for row in block.get("rows") or []:
            label = row.get("label")
            if not label:
                continue  # spacer rows have a null label and no seats
            for seat in row.get("seats") or []:
                # Type matters: rows can contain Wheelchair and Companion
                # spaces, which shouldn't count as "a seat opened up".
                rows[seat["id"]] = "%s|%s" % (label, seat.get("type") or "Standard")
    return rows


def fetch_free_seats(theatre_id, showtime_id):
    """Seat ids currently bookable for this showtime."""
    data = get_json(
        seat_url(theatre_id, showtime_id, "seat-availability") + "?preview=true"
    )
    statuses = data.get("seatAvailabilities") or {}
    return [sid for sid, status in statuses.items() if status == "Available"]


def find_film(name_fragment):
    """Resolve a film name to its Cineplex film id. Used by --find-film."""
    data = api_get("movies", {"language": "en"})
    frag = name_fragment.lower()
    return [m for m in data.get("items", []) if frag in m.get("name", "").lower()]


def find_theatres(name_fragment):
    """Resolve theatre names to ids. Used by --find-theatre."""
    data = api_get("theatres", {"language": "en"})
    frag = name_fragment.lower()
    out = []
    for bucket in ("favouriteTheatres", "nearbyTheatres", "otherTheatres"):
        for t in data.get(bucket) or []:
            if frag in t.get("theatreName", "").lower():
                out.append(t)
    return out


def fetch_sessions(film_id, theatre):
    """Return a flat list of matching sessions for one theatre.

    One request covers the whole bookable window (~30 days), so a poll costs
    exactly one request per theatre.
    """
    data = api_get(
        "showtimes",
        {"language": "en", "locationId": theatre["id"], "filmId": film_id},
    )
    out = []
    for block in data:
        for day in block.get("dates") or []:
            for movie in day.get("movies") or []:
                for exp in movie.get("experiences") or []:
                    types = exp.get("experienceTypes") or []
                    yield_types = [t.strip().lower() for t in types]
                    for session in exp.get("sessions") or []:
                        out.append(
                            {
                                "id": str(session.get("vistaSessionId")),
                                "theatre_id": theatre["id"],
                                "theatre": theatre.get("name")
                                or block.get("theatre", str(theatre["id"])),
                                "movie": movie.get("name"),
                                "experience_types": types,
                                "_types_lc": yield_types,
                                "start": session.get("showStartDateTime"),
                                "start_utc": session.get("showStartDateTimeUtc"),
                                "auditorium": session.get("auditorium"),
                                "seats": session.get("seatsRemaining"),
                                "sold_out": bool(session.get("isSoldOut")),
                                "in_past": bool(session.get("isInThePast")),
                                "online": bool(session.get("isShowtimeEnabledOnline")),
                                # ticketingUrl is deliberately NOT used: it's an
                                # apis.cineplex.com route that 401s without the
                                # subscription key, so it's dead in a browser.
                                "seatmap_url": session.get("seatMapUrl"),
                                "deeplink": session.get("deeplinkUrl"),
                            }
                        )
    return out


def matches_experience(session, required, forbidden):
    types = session["_types_lc"]
    if any(r.strip().lower() not in types for r in required):
        return False
    if any(f.strip().lower() in types for f in forbidden):
        return False
    return True


def collect(cfg):
    """Poll every configured theatre. Returns (sessions, errors)."""
    required = cfg.get("require_experience_types", [])
    forbidden = cfg.get("exclude_experience_types", [])
    sessions, errors = [], []
    for theatre in cfg["theatres"]:
        try:
            found = fetch_sessions(cfg["film_id"], theatre)
        except Exception as exc:  # network/API hiccup: report, keep other theatres
            errors.append("%s: %s" % (theatre.get("name", theatre["id"]), exc))
            continue
        for s in found:
            if s["in_past"] or not matches_experience(s, required, forbidden):
                continue
            sessions.append(s)
    sessions.sort(key=lambda s: (s["start"] or "", s["theatre"]))
    return sessions, errors


# --------------------------------------------------------------------------
# Change detection
# --------------------------------------------------------------------------


def required_rows(cfg):
    return [r.strip().upper() for r in (cfg.get("require_rows") or []) if r.strip()]


def resolve_rows(cfg, sessions, state, log_fn=None):
    """Attach a `row_seats` count to each session: free seats in the wanted rows.

    Seat maps are per showtime, so checking all ~226 every poll would mean ~450
    requests. Two things keep that down to a handful:

      * the row layout is cached per theatre (two fetches, ever), and
      * availability is only re-read when the showtime's total seat count has
        actually moved since the last poll — if the total is unchanged, the
        seats behind it are unchanged too.

    Returns the number of availability requests made.
    """
    wanted = set(required_rows(cfg))
    if not wanted:
        return 0
    kinds = set(cfg.get("seat_types") or ["Standard"])

    layouts = state.setdefault("layouts", {})
    known = state.get("sessions", {})
    calls = 0

    tick = state.get("tick", 0) + 1
    state["tick"] = tick

    # A changed total is the cheap signal that seats moved, but it can miss a
    # cancellation in F offset by a booking in A within the same interval. So
    # each poll also refreshes the stalest few showtimes outright, which means
    # any missed change self-heals within a full sweep instead of persisting.
    sweep = max(0, int(cfg.get("seat_refresh_per_poll", 30)))
    live = [s for s in sessions if seats_of(s) > 0]
    stale = sorted(live, key=lambda s: (known.get(s["id"]) or {}).get("checked", 0))
    due = {s["id"] for s in stale[:sweep]}

    for s in sessions:
        tid = str(s["theatre_id"])
        prev = known.get(s["id"]) or {}
        total = seats_of(s)

        if total <= 0:
            s["row_seats"] = 0
            s["row_labels"] = []
            s["checked"] = tick
            continue

        cached = prev.get("seats") == total and "row_seats" in prev
        if cached and s["id"] not in due:
            s["row_seats"] = prev["row_seats"]
            s["row_labels"] = prev.get("row_labels", [])
            s["checked"] = prev.get("checked", 0)
            continue

        try:
            if tid not in layouts:
                layouts[tid] = fetch_seat_rows(s["theatre_id"], s["id"])
            rows = layouts[tid]
            free = fetch_free_seats(s["theatre_id"], s["id"])
            calls += 1
        except Exception as exc:
            # Fall back to the previous answer rather than inventing one; a
            # missing seat map must never manufacture an alert.
            if log_fn:
                log_fn("WARN seat map failed for %s - %s" % (s["id"], exc))
            s["row_seats"] = prev.get("row_seats", 0)
            s["row_labels"] = prev.get("row_labels", [])
            s["checked"] = prev.get("checked", 0)
            continue

        hits = []
        for sid in free:
            entry = rows.get(sid)
            if not entry:
                continue
            label, _, seat_type = entry.partition("|")
            if label in wanted and seat_type in kinds:
                hits.append(label)
        s["row_seats"] = len(hits)
        s["row_labels"] = sorted(set(hits))
        s["checked"] = tick

    return calls


def seats_of(session):
    if session["sold_out"]:
        return 0
    return session["seats"] if session["seats"] is not None else 0


def counted_seats(cfg, session):
    """The seat count every alert rule is judged against.

    With require_rows set this is the number of free seats in those rows, so a
    showtime with 40 seats all in the front rows counts as zero.
    """
    if required_rows(cfg) and "row_seats" in session:
        return session["row_seats"]
    return seats_of(session)


def available(session):
    return session["online"] and not session["sold_out"] and seats_of(session) > 0


def bookable(cfg, session):
    return (
        session["online"]
        and not session["sold_out"]
        and counted_seats(cfg, session) > 0
    )


def diff(cfg, sessions, state):
    """Compare this poll against the last one and return a list of alerts."""
    known = state.get("sessions", {})
    first_run = not known
    up_threshold = cfg.get("seats_up_threshold", 4)
    # A show going 44 -> 48 seats is noise; the same jump on a show that had 3
    # left is the thing you actually want to hear about. Only treat a seat
    # increase as newsworthy if the show was scarce to begin with.
    up_only_below = cfg.get("seats_up_only_below", 0) or float("inf")
    low_threshold = cfg.get("low_seats_threshold", 0)
    watch_low = low_threshold > 0 and "LOW_SEATS" in cfg.get("alert_on", [])
    enabled = set(cfg.get("alert_on", ALERT_ORDER))

    alerts = []
    for s in sessions:
        prev = known.get(s["id"])
        # With require_rows set, "seats" here means seats in those rows only, so
        # every rule below is automatically row-aware.
        now_seats = counted_seats(cfg, s)
        now_avail = bookable(cfg, s)

        if prev is None:
            # On the very first run everything is "new"; only shout about it if
            # the user opted in, otherwise just seed the baseline quietly.
            if now_avail and "NEW_SHOWTIME" in enabled:
                if not first_run or cfg.get("alert_on_first_run", False):
                    alerts.append(("NEW_SHOWTIME", s, None))
            continue

        was_avail = prev.get("bookable", prev.get("available", False))
        was_seats = prev.get(
            "row_seats" if required_rows(cfg) else "seats", prev.get("seats", 0)
        )

        if now_avail and not was_avail and "REOPENED" in enabled:
            alerts.append(("REOPENED", s, was_seats))
        elif (
            now_avail
            and was_avail
            and was_seats < up_only_below
            and now_seats - was_seats >= up_threshold
            and "SEATS_UP" in enabled
        ):
            alerts.append(("SEATS_UP", s, was_seats))
        elif (
            watch_low
            and now_avail
            and was_seats > low_threshold >= now_seats
        ):
            alerts.append(("LOW_SEATS", s, was_seats))

    return alerts, first_run


def snapshot(cfg, sessions):
    out = {}
    for s in sessions:
        entry = {
            "seats": seats_of(s),
            "available": available(s),
            "bookable": bookable(cfg, s),
            "start": s["start"],
            "theatre": s["theatre"],
        }
        if "row_seats" in s:
            entry["row_seats"] = s["row_seats"]
            entry["row_labels"] = s.get("row_labels", [])
            entry["checked"] = s.get("checked", 0)
        out[s["id"]] = entry
    return out


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def pretty_time(iso):
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return iso or "?"
    return dt.strftime("%a %b %-d, %-I:%M %p")


def booking_link(session):
    """A link that actually opens in a browser.

    seatMapUrl goes straight to the seat map, so the seat count in an alert can
    be checked against Cineplex directly; deeplinkUrl is Cineplex's own redirect
    to the movie page with the session preselected.
    """
    return (
        session.get("seatmap_url")
        or session.get("deeplink")
        or "https://www.cineplex.com/movie/the-odyssey"
    )


def format_alerts(cfg, alerts):
    """Render alerts into (subject, plain-text body)."""
    by_kind = {}
    for kind, s, prev_seats in alerts:
        by_kind.setdefault(kind, []).append((s, prev_seats))

    top = next(k for k in ALERT_ORDER if k in by_kind)
    n = len(alerts)
    subject = "%s %s - %s %s" % (
        cfg.get("emoji", "\U0001f3ac"),
        ALERT_LABEL[top],
        cfg.get("film_name", "Movie"),
        cfg.get("experience_label", "IMAX 70mm"),
    )
    if n > 1:
        subject += " (%d showtimes)" % n

    lines = []
    for kind in ALERT_ORDER:
        rows = by_kind.get(kind)
        if not rows:
            continue
        lines.append("%s:" % ALERT_LABEL[kind])
        for s, prev_seats in rows:
            change = ""
            if prev_seats is not None:
                change = " (was %d)" % prev_seats
            lines.append(
                "  %s  -  %s"
                % (pretty_time(s["start"]), s["theatre"])
            )
            if required_rows(cfg):
                # Name the rows so the claim can be checked on the seat map.
                where = ", ".join(s.get("row_labels") or []) or "-"
                lines.append(
                    "    %d seat%s in row%s %s%s  (%d in the room)"
                    % (s.get("row_seats", 0),
                       "" if s.get("row_seats") == 1 else "s",
                       "" if len(s.get("row_labels") or []) == 1 else "s",
                       where, change, seats_of(s))
                )
            else:
                lines.append(
                    "    %d seat%s left%s"
                    % (seats_of(s), "" if seats_of(s) == 1 else "s", change)
                )
            lines.append("    %s" % booking_link(s))
        lines.append("")

    lines.append("Odyssey watcher - %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    return subject, "\n".join(lines).strip()


def format_status(sessions):
    if not sessions:
        return "No matching showtimes found."
    lines = []
    by_theatre = {}
    for s in sessions:
        by_theatre.setdefault(s["theatre"], []).append(s)
    for theatre, rows in by_theatre.items():
        avail = [r for r in rows if available(r)]
        lines.append(
            "%s  -  %d/%d showtimes have seats" % (theatre, len(avail), len(rows))
        )
        for r in rows:
            flag = "SOLD OUT" if not available(r) else "%d seats" % seats_of(r)
            lines.append("   %-24s %s" % (pretty_time(r["start"]), flag))
        lines.append("")
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Notifiers
# --------------------------------------------------------------------------


def env_or(value):
    """Allow config values to indirect through env vars: "env:MY_SECRET"."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


def env_list(value):
    """Resolve a config value that should end up as a list of strings.

    Accepts a real list, or a single "env:VAR" holding a comma/space/newline
    separated set — which is what a CI secret has to be, since a secret can only
    ever be one string.
    """
    if isinstance(value, str):
        value = env_or(value)
        return [p for p in value.replace(",", " ").split() if p]
    out = []
    for item in value or []:
        resolved = env_or(item)
        if resolved:
            out.append(str(resolved))
    return out


def notify_ntfy(conf, subject, body):
    """Push to an ntfy.sh topic. Anyone subscribed to the topic gets it."""
    topic = env_or(conf.get("topic", ""))
    if not topic:
        raise ValueError("ntfy: no topic configured")
    server = conf.get("server", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(
        "%s/%s" % (server, topic),
        data=body.encode("utf-8"),
        headers={
            "Title": subject.encode("ascii", "ignore").decode() or "Odyssey alert",
            "Priority": str(conf.get("priority", "urgent")),
            "Tags": conf.get("tags", "movie_camera"),
        },
        method="POST",
    )
    token = env_or(conf.get("token", ""))
    if token:
        req.add_header("Authorization", "Bearer %s" % token)
    urllib.request.urlopen(req, timeout=20).read()


def notify_webhook(conf, subject, body):
    """Discord or Slack incoming webhook."""
    url = env_or(conf.get("url", ""))
    if not url:
        raise ValueError("webhook: no url configured")
    text = "**%s**\n```\n%s\n```" % (subject, body)
    mention = conf.get("mention", "")
    if mention:
        text = "%s\n%s" % (mention, text)
    style = conf.get("style", "discord")
    payload = {"content": text} if style == "discord" else {"text": text}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=20).read()


def notify_telegram(conf, subject, body):
    token = env_or(conf.get("bot_token", ""))
    chat_ids = env_list(conf.get("chat_ids"))
    if not token or not chat_ids:
        raise ValueError("telegram: need bot_token and chat_ids")
    for chat_id in chat_ids:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": "%s\n\n%s" % (subject, body),
                "disable_web_page_preview": "true",
            }
        ).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token, data=data
        )
        urllib.request.urlopen(req, timeout=20).read()


def telegram_chat_ids(token):
    """List chat ids that have messaged the bot.

    Getting a chat id is the fiddly part of Telegram setup: each person has to
    message the bot once, then their id shows up here.
    """
    url = "https://api.telegram.org/bot%s/getUpdates" % token
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    seen = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            ) or chat.get("username", "")
            seen[chat["id"]] = "%s [%s]" % (name or "?", chat.get("type", "?"))
    return seen


def notify_email(conf, subject, body):
    """SMTP email to a list of recipients (Gmail app password works fine)."""
    host = conf.get("smtp_host", "smtp.gmail.com")
    port = int(conf.get("smtp_port", 587))
    user = env_or(conf.get("username", ""))
    password = env_or(conf.get("password", ""))
    sender = env_or(conf.get("from", "")) or user
    recipients = env_list(conf.get("to"))
    if not (user and password and recipients):
        raise ValueError("email: need username, password and at least one recipient")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(user, password)
            smtp.send_message(msg)


def notify_macos(conf, subject, body):
    """Local desktop banner + sound, so you get it even if push is down."""
    first_line = body.split("\n")[0] if body else ""

    def as_applescript(text):
        # AppleScript can't parse the \udXXX surrogate escapes json.dumps emits for
        # emoji, so keep the literal characters and only escape quotes/backslashes.
        return json.dumps(text, ensure_ascii=False)

    script = 'display notification %s with title %s sound name %s' % (
        as_applescript(first_line),
        as_applescript(subject),
        as_applescript(conf.get("sound", "Glass")),
    )
    subprocess.run(["osascript", "-e", script], check=False, timeout=20)
    if conf.get("speak"):
        subprocess.run(["say", conf["speak"]], check=False, timeout=20)


NOTIFIERS = {
    "ntfy": notify_ntfy,
    "webhook": notify_webhook,
    "telegram": notify_telegram,
    "email": notify_email,
    "macos": notify_macos,
}


def send_all(cfg, subject, body):
    """Fire every enabled notifier. One failure never blocks the others."""
    sent, failed = [], []
    for name, conf in (cfg.get("notify") or {}).items():
        if not conf.get("enabled"):
            continue
        fn = NOTIFIERS.get(name)
        if fn is None:
            failed.append("%s: unknown notifier" % name)
            continue
        try:
            fn(conf, subject, body)
            sent.append(name)
        except Exception as exc:
            failed.append("%s: %s" % (name, exc))
    return sent, failed


# --------------------------------------------------------------------------
# State + config
# --------------------------------------------------------------------------


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE_PATH)  # atomic, so a crash mid-write can't corrupt state


def load_config():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("No config at %s - copy config.example.json to config.json" % CONFIG_PATH)
    return cfg


# --------------------------------------------------------------------------
# Poll
# --------------------------------------------------------------------------


def poll_once(cfg, state, dry_run=False):
    sessions, errors = collect(cfg)
    for err in errors:
        log("WARN fetch failed - %s" % err)

    if not sessions and errors:
        # Every theatre failed; don't let a total outage wipe the baseline and
        # then re-alert on everything once the API comes back.
        return state, 0

    seat_calls = resolve_rows(cfg, sessions, state, log_fn=log)

    alerts, first_run = diff(cfg, sessions, state)
    avail = sum(1 for s in sessions if available(s))
    rows = required_rows(cfg)
    detail = ""
    if rows:
        in_rows = sum(1 for s in sessions if bookable(cfg, s))
        detail = ", %d with seats in %s (%d seat map%s read)" % (
            in_rows, "".join(rows), seat_calls, "" if seat_calls == 1 else "s"
        )
    log(
        "poll: %d showtimes, %d with seats%s, %d alert(s)%s"
        % (len(sessions), avail, detail, len(alerts),
           " [seeding baseline]" if first_run else "")
    )

    if alerts:
        subject, body = format_alerts(cfg, alerts)
        if dry_run:
            log("DRY RUN - would send:\n%s\n%s" % (subject, body))
        else:
            sent, failed = send_all(cfg, subject, body)
            log("notified via %s" % (", ".join(sent) or "nobody"))
            for f in failed:
                log("WARN notify failed - %s" % f)

    state = {
        "sessions": snapshot(cfg, sessions),
        "layouts": state.get("layouts", {}),  # per-theatre, fetched once
        "last_poll": datetime.now().isoformat(timespec="seconds"),
        "available_count": avail,
        "total_count": len(sessions),
    }
    if not dry_run:
        save_state(state)
    return state, len(alerts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="single poll then exit (cron mode)")
    ap.add_argument("--daemon", action="store_true", help="poll forever on the config interval")
    ap.add_argument("--status", action="store_true", help="print availability, send nothing")
    ap.add_argument("--dry-run", action="store_true", help="detect changes but don't send or save")
    ap.add_argument("--test-notify", action="store_true", help="send a test alert everywhere")
    ap.add_argument("--reset", action="store_true", help="wipe saved state (next poll re-seeds)")
    ap.add_argument("--find-film", metavar="NAME", help="look up a film id by name")
    ap.add_argument("--find-theatre", metavar="NAME", help="look up theatre ids by name")
    ap.add_argument(
        "--telegram-chats",
        action="store_true",
        help="list chat ids of everyone who has messaged your Telegram bot",
    )
    ap.add_argument("--config", metavar="PATH", help="config file (default config.json)")
    ap.add_argument("--state", metavar="PATH", help="state file (default state.json)")
    args = ap.parse_args()

    global CONFIG_PATH, STATE_PATH
    if args.config:
        CONFIG_PATH = os.path.abspath(args.config)
    if args.state:
        STATE_PATH = os.path.abspath(args.state)
        os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)

    if args.find_film:
        for m in find_film(args.find_film):
            print("%-8s %s  (%s)" % (m["id"], m["name"], m.get("releaseDate", "")[:10]))
        return
    if args.find_theatre:
        for t in find_theatres(args.find_theatre):
            print("%-8s %s" % (t["theatreId"], t["theatreName"]))
        return

    cfg = load_config()

    if args.telegram_chats:
        token = env_or((cfg.get("notify", {}).get("telegram") or {}).get("bot_token", ""))
        if not token:
            sys.exit("No Telegram bot token - set ODYSSEY_TELEGRAM_TOKEN")
        found = telegram_chat_ids(token)
        if not found:
            print("Nobody has messaged the bot yet. Have each person send it any")
            print("message (e.g. /start), then run this again.")
            return
        print("Add these to notify.telegram.chat_ids in config.json:")
        for chat_id, who in found.items():
            print("  %-16s %s" % (chat_id, who))
        return

    if args.reset:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
        log("state cleared")
        return

    if args.test_notify:
        sent, failed = send_all(
            cfg,
            "%s TEST - Odyssey watcher is live" % cfg.get("emoji", "\U0001f3ac"),
            "If you can read this, alerts are wired up correctly.\n"
            "You'll get a message like this the moment IMAX 70mm seats open up.",
        )
        log("test sent via %s" % (", ".join(sent) or "nobody"))
        for f in failed:
            log("FAIL %s" % f)
        if not sent:
            sys.exit(1)
        return

    if args.status:
        sessions, errors = collect(cfg)
        for err in errors:
            print("WARN %s" % err, file=sys.stderr)
        print(format_status(sessions))
        return

    state = load_json(STATE_PATH, {})

    if args.daemon:
        interval = int(cfg.get("poll_interval_seconds", 120))
        backoff = interval
        log("daemon start - polling every %ds" % interval)
        while True:
            try:
                state, _ = poll_once(cfg, state, dry_run=args.dry_run)
                backoff = interval
            except KeyboardInterrupt:
                log("stopped")
                return
            except Exception as exc:
                # Unexpected failure: back off up to 15 min so we don't hammer.
                log("ERROR %s" % exc)
                backoff = min(backoff * 2, 900)
            time.sleep(backoff)

    poll_once(cfg, state, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
