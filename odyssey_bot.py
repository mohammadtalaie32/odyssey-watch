#!/usr/bin/env python3
"""Interactive Telegram bot for the Odyssey watcher.

The watcher pushes alerts at you; this lets you pull. It answers button taps
with live Cineplex data so you can check availability yourself — and every
showtime carries a Book button straight to Cineplex's seat map, so you can
verify the numbers rather than take the bot's word for them.

    ./odyssey_bot.py            long-poll for button taps until stopped
    ./odyssey_bot.py --menu     push the menu to everyone in chat_ids, then exit

Menu -> theatre -> day -> showtimes with seat counts and Book links.

Stdlib only, same as the watcher. Needs ODYSSEY_TELEGRAM_TOKEN.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import odyssey_watch as ow

API = "https://api.telegram.org/bot%s/%s"

# Cineplex data is cached briefly so a burst of button taps doesn't turn into a
# burst of API calls. Seat counts don't move meaningfully inside 30 seconds.
_CACHE = {"at": 0.0, "sessions": None}
CACHE_TTL = 30

# Row lookups are cached here between taps: seat layouts are per theatre and
# never change, so this stays warm after the first use.
ROW_STATE_PATH = "bot-rows.json"
_ROWS = None


def row_state():
    global _ROWS
    if _ROWS is None:
        _ROWS = ow.load_json(ROW_STATE_PATH, {})
    return _ROWS


def save_row_state():
    if _ROWS is not None:
        try:
            with open(ROW_STATE_PATH, "w") as fh:
                json.dump(_ROWS, fh)
        except OSError:
            pass


def resolve(cfg, subset):
    """Fill in row_seats for just the showtimes about to be displayed.

    Scoping this to what's on screen keeps a tap responsive — four seat maps,
    not two hundred — and means the number shown is read live rather than
    inherited from an earlier poll.
    """
    if not ow.required_rows(cfg) or not subset:
        return
    st = row_state()
    ow.resolve_rows(cfg, subset, st)
    sessions = st.setdefault("sessions", {})
    for s in subset:
        if "row_seats" in s:
            sessions[s["id"]] = {
                "seats": ow.seats_of(s),
                "row_seats": s["row_seats"],
                "row_labels": s.get("row_labels", []),
                "checked": s.get("checked", 0),
            }
    save_row_state()


def row_note(cfg, s):
    """' · 2 in F,G' style suffix, or a marker that F-J is empty."""
    if not ow.required_rows(cfg) or "row_seats" not in s:
        return ""
    if s["row_seats"] <= 0:
        return "  ·  none in %s" % "".join(ow.required_rows(cfg))
    return "  ·  <b>%d in %s</b>" % (s["row_seats"], ",".join(s["row_labels"]))


def tg(token, method, **params):
    payload = {}
    for key, value in params.items():
        if value is None:
            continue
        payload[key] = (
            json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        )
    req = urllib.request.Request(
        API % (token, method), data=urllib.parse.urlencode(payload).encode()
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError("%s failed: %s %s" % (method, exc.code, detail))


def get_sessions(cfg, force=False):
    now = time.time()
    if force or _CACHE["sessions"] is None or now - _CACHE["at"] > CACHE_TTL:
        sessions, errors = ow.collect(cfg)
        if errors and not sessions:
            raise RuntimeError("Cineplex API unreachable: %s" % errors[0])
        _CACHE.update(at=now, sessions=sessions)
    return _CACHE["sessions"]


# --------------------------------------------------------------------------
# Screens - each returns (text, inline_keyboard)
# --------------------------------------------------------------------------


def esc(text):
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def screen_menu(cfg, sessions):
    total = len(sessions)
    avail = [s for s in sessions if ow.available(s)]
    tight = [s for s in avail if ow.seats_of(s) <= 10]
    soldout = total - len(avail)

    text = [
        "<b>%s — %s</b>" % (esc(cfg.get("film_name")), esc(cfg.get("experience_label"))),
        "",
        "%d showtimes in the booking window" % total,
        "%d with seats · %d sold out" % (len(avail), soldout),
    ]
    if tight:
        text.append("%d nearly gone (10 seats or fewer)" % len(tight))
    text += ["", "<i>Pick a theatre to see showtimes and seat counts.</i>"]

    want = ow.required_rows(cfg)
    rows = []
    if want:
        text.insert(4, "Filtering for rows <b>%s</b>" % ",".join(want))
        rows.append([{"text": "🎯 Seats in rows %s" % "".join(want),
                      "callback_data": "rows"}])
    rows += [[{"text": t["name"].replace("Cineplex Cinemas ", "🎬 "),
               "callback_data": "th:%s:0" % t["id"]}]
             for t in cfg["theatres"]]
    rows.append([{"text": "🔥 Almost gone", "callback_data": "low"},
                 {"text": "🎟 Sold out", "callback_data": "out"}])
    rows.append([{"text": "🔄 Refresh", "callback_data": "menu:r"}])
    return "\n".join(text), rows


def screen_rows(cfg, sessions):
    """The showtimes that actually matter: ones with a seat in the wanted rows."""
    want = ow.required_rows(cfg)
    live = [s for s in sessions if ow.available(s)]
    resolve(cfg, live)
    hits = [s for s in live if s.get("row_seats", 0) > 0]
    hits.sort(key=lambda s: s["start"] or "")

    text = ["<b>🎯 Seats in rows %s</b>" % ",".join(want), ""]
    rows = []
    if not hits:
        text += ["Nothing available in those rows right now.",
                 "",
                 "<i>%d showtimes have seats, but all of them are in other rows. "
                 "You'll be alerted the moment one opens in %s.</i>"
                 % (len(live), ",".join(want))]
    else:
        for s in hits:
            where = s["theatre"].replace("Cineplex Cinemas ", "")
            text.append("🎯 <b>%s</b> — %d in row %s\n     %s · %d free in the room"
                        % (esc(ow.pretty_time(s["start"])), s["row_seats"],
                           ",".join(s["row_labels"]), esc(where), ow.seats_of(s)))
            rows.append([{"text": "🎟 %s · %s (row %s)"
                          % (ow.pretty_time(s["start"]), where.replace(" Square One", ""),
                             ",".join(s["row_labels"])),
                          "url": ow.booking_link(s)}])
        text += ["", "<i>Tap to open the seat map and confirm.</i>"]

    rows = rows[:8]
    rows.append([{"text": "🏠 Menu", "callback_data": "menu"},
                 {"text": "🔄 Refresh", "callback_data": "rows:r"}])
    return "\n".join(text), rows


def days_with_shows(sessions, theatre_id=None):
    days = []
    for s in sessions:
        if theatre_id and s["theatre_id"] != theatre_id:
            continue
        day = (s["start"] or "")[:10]
        if day and day not in days:
            days.append(day)
    return sorted(days)


def screen_theatre(cfg, sessions, theatre_id, day_index):
    theatre = next(
        (t for t in cfg["theatres"] if str(t["id"]) == str(theatre_id)), None
    )
    name = theatre["name"] if theatre else str(theatre_id)
    days = days_with_shows(sessions, int(theatre_id))
    if not days:
        return "No showtimes found for %s." % esc(name), [[{"text": "← Back", "callback_data": "menu"}]]

    day_index = max(0, min(int(day_index), len(days) - 1))
    day = days[day_index]
    todays = [
        s for s in sessions
        if s["theatre_id"] == int(theatre_id) and (s["start"] or "").startswith(day)
    ]
    todays.sort(key=lambda s: s["start"] or "")

    heading = datetime.strptime(day, "%Y-%m-%d").strftime("%A, %B %-d")
    text = ["<b>%s</b>" % esc(name), "<b>%s</b>" % esc(heading), ""]

    # Only the handful on screen, so the row counts are read live on each tap.
    resolve(cfg, [s for s in todays if ow.available(s)])

    book_rows = []
    for s in todays:
        when = ow.pretty_time(s["start"]).split(", ")[-1]
        if not ow.available(s):
            text.append("🔴 <b>%s</b> — SOLD OUT" % esc(when))
            continue
        seats = ow.seats_of(s)
        dot = "🎯" if s.get("row_seats", 0) > 0 else ("🟠" if seats <= 10 else "🟢")
        text.append("%s <b>%s</b> — %d seat%s%s"
                    % (dot, esc(when), seats, "" if seats == 1 else "s", row_note(cfg, s)))
        label = "🎟 Book %s (%d left)" % (when, seats)
        if s.get("row_seats", 0) > 0:
            label = "🎯 Book %s (row %s)" % (when, ",".join(s["row_labels"]))
        book_rows.append([{"text": label, "url": ow.booking_link(s)}])

    text += ["", "<i>Tap Book to open Cineplex's seat map and check it yourself.</i>"]

    nav = []
    if day_index > 0:
        nav.append({"text": "← Prev day", "callback_data": "th:%s:%d" % (theatre_id, day_index - 1)})
    if day_index < len(days) - 1:
        nav.append({"text": "Next day →", "callback_data": "th:%s:%d" % (theatre_id, day_index + 1)})

    rows = book_rows[:6]  # keep the keyboard a sane height
    if nav:
        rows.append(nav)
    rows.append([{"text": "🏠 Menu", "callback_data": "menu"},
                 {"text": "🔄 Refresh", "callback_data": "th:%s:%d:r" % (theatre_id, day_index)}])
    return "\n".join(text), rows


def screen_filtered(cfg, sessions, mode):
    if mode == "low":
        picked = [s for s in sessions if ow.available(s) and ow.seats_of(s) <= 10]
        title = "🔥 Almost gone — 10 seats or fewer"
        empty = "Nothing is close to selling out right now."
    else:
        picked = [s for s in sessions if not ow.available(s)]
        title = "🎟 Sold out — watching for cancellations"
        empty = "Nothing is sold out right now. Every showtime has seats."

    picked.sort(key=lambda s: s["start"] or "")
    text = ["<b>%s</b>" % title, ""]
    rows = []
    if not picked:
        text.append(empty)
    else:
        for s in picked[:15]:
            where = s["theatre"].replace("Cineplex Cinemas ", "")
            when = ow.pretty_time(s["start"])
            if mode == "low":
                text.append("🟠 <b>%s</b> — %d left\n     %s" % (esc(when), ow.seats_of(s), esc(where)))
                # Include the theatre: the same slot exists at both, so a
                # time-only label would give two identical buttons.
                short = where.replace(" Square One", "")
                rows.append([{"text": "🎟 %s · %s (%d)" % (when, short, ow.seats_of(s)),
                              "url": ow.booking_link(s)}])
            else:
                text.append("🔴 <b>%s</b>\n     %s" % (esc(when), esc(where)))
        if len(picked) > 15:
            text.append("\n<i>…and %d more.</i>" % (len(picked) - 15))
        if mode == "out":
            text += ["", "<i>You'll get an alert the moment any of these frees up.</i>"]

    rows = rows[:6]
    rows.append([{"text": "🏠 Menu", "callback_data": "menu"},
                 {"text": "🔄 Refresh", "callback_data": "%s:r" % mode}])
    return "\n".join(text), rows


def render(cfg, data, force=False):
    """Map a callback_data string to a screen."""
    parts = data.split(":")
    force = force or parts[-1] == "r"
    sessions = get_sessions(cfg, force=force)
    if parts[0] == "th":
        return screen_theatre(cfg, sessions, parts[1], parts[2])
    if parts[0] == "rows":
        return screen_rows(cfg, sessions)
    if parts[0] in ("low", "out"):
        return screen_filtered(cfg, sessions, parts[0])
    return screen_menu(cfg, sessions)


# --------------------------------------------------------------------------
# Bot loop
# --------------------------------------------------------------------------


def allowed(cfg, chat_id):
    """Only answer chats we know about.

    The bot's username is public, so without this anyone who stumbles on it
    could drive Cineplex requests through it.
    """
    conf = (cfg.get("notify") or {}).get("telegram") or {}
    if conf.get("bot_allow_any"):
        return True
    return str(chat_id) in [str(c) for c in ow.env_list(conf.get("chat_ids"))]


def send_menu(token, cfg, chat_id):
    text, rows = render(cfg, "menu", force=True)
    return tg(token, "sendMessage", chat_id=chat_id, text=text,
              parse_mode="HTML", reply_markup={"inline_keyboard": rows},
              disable_web_page_preview=True)


def handle_update(token, cfg, update):
    msg = update.get("message")
    cb = update.get("callback_query")

    if msg:
        chat_id = (msg.get("chat") or {}).get("id")
        if not allowed(cfg, chat_id):
            return
        send_menu(token, cfg, chat_id)
        return

    if cb:
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        message_id = (cb.get("message") or {}).get("message_id")
        if not allowed(cfg, chat_id):
            tg(token, "answerCallbackQuery", callback_query_id=cb["id"], text="Not authorised")
            return
        data = cb.get("data") or "menu"
        # Answer before rendering: a full row scan reads many seat maps and can
        # take longer than Telegram is willing to spin for.
        note = "Checking seat maps…" if data.startswith("rows") else None
        try:
            tg(token, "answerCallbackQuery", callback_query_id=cb["id"], text=note)
        except Exception:
            pass
        try:
            text, rows = render(cfg, data)
        except Exception as exc:
            text, rows = "Couldn't reach Cineplex:\n%s" % esc(exc), [
                [{"text": "🔄 Try again", "callback_data": "menu:r"}]
            ]
        try:
            tg(token, "editMessageText", chat_id=chat_id, message_id=message_id,
               text=text, parse_mode="HTML",
               reply_markup={"inline_keyboard": rows}, disable_web_page_preview=True)
        except RuntimeError as exc:
            # "message is not modified" just means the screen didn't change.
            if "not modified" not in str(exc):
                raise


def run(token, cfg):
    tg(token, "setMyCommands", commands=[{"command": "start", "description": "Show the menu"}])
    me = tg(token, "getMe")["result"]
    ow.log("bot online as @%s" % me.get("username"))
    offset = None
    while True:
        try:
            resp = tg(token, "getUpdates", offset=offset, timeout=30,
                      allowed_updates=["message", "callback_query"])
        except Exception as exc:
            ow.log("bot: getUpdates failed - %s" % exc)
            time.sleep(5)
            continue
        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            try:
                handle_update(token, cfg, update)
            except Exception as exc:
                ow.log("bot: update failed - %s" % exc)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--menu", action="store_true",
                    help="push the menu to every configured chat, then exit")
    ap.add_argument("--config", metavar="PATH", help="config file")
    args = ap.parse_args()

    if args.config:
        ow.CONFIG_PATH = args.config
    cfg = ow.load_config()
    conf = (cfg.get("notify") or {}).get("telegram") or {}
    token = ow.env_or(conf.get("bot_token", ""))
    if not token:
        sys.exit("No Telegram token - set ODYSSEY_TELEGRAM_TOKEN")

    if args.menu:
        for chat_id in ow.env_list(conf.get("chat_ids")):
            send_menu(token, cfg, chat_id)
            ow.log("menu sent to %s" % chat_id)
        return

    run(token, cfg)


if __name__ == "__main__":
    main()
