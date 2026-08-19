# Odyssey Watch

Watches Cineplex for **The Odyssey in IMAX 70mm** at **Cineplex Cinemas Vaughan** and
**Cineplex Cinemas Mississauga Square One**, and pushes an alert to you and whoever else
you add the moment seats open up.

Stdlib-only Python, no dependencies.

## How it works

It calls the same public JSON API that `www.cineplex.com` itself calls:

```
GET https://apis.cineplex.com/prod/cpx/theatrical/api/v1/showtimes
      ?language=en&locationId=<theatreId>&filmId=<filmId>
Header: Ocp-Apim-Subscription-Key: <key embedded in Cineplex's own web bundle>
```

One request per theatre returns the **entire ~30-day bookable window**, including
`experienceTypes` (so `IMAX` + `70mm` can be matched exactly, not guessed from a label),
`seatsRemaining`, `isSoldOut`, and a direct ticketing URL for each showtime. So a poll is
2 HTTP requests totalling ~16 KB — cheap enough to run every minute without being rude.

Each poll is diffed against the previous one (`state.json`) and alerts fire on:

| Alert | Meaning |
|---|---|
| `REOPENED` | A sold-out showtime now has seats — someone cancelled. **This is the one you want.** |
| `NEW_SHOWTIME` | A showtime we've never seen — a new date rolled into the on-sale window. |
| `SEATS_UP` | Seats jumped by `seats_up_threshold`+ on a show that had fewer than `seats_up_only_below` left — a bulk return on a scarce showtime. |
| `LOW_SEATS` | Seats fell to `low_seats_threshold` or below — last chance. Off by default. |

### Row filtering

`require_rows` narrows everything to seats in specific rows. With
`["F","G","H","I","J"]` set, a showtime with 51 free seats all in rows A–E counts as
**zero** — no alert, and the bot marks it "none in FGHIJ". Every rule above then operates on
the row count rather than the room total.

This matters more than it sounds: at the time of writing, 224 showtimes had seats and only
**3** had one in F–J. Without the filter, essentially every alert would be a seat you don't
want.

The showtimes API only reports a room total, so row data comes from the two endpoints the
seat-map page itself calls — both plain reads, no cart, login or session:

```
GET /v1/theatre/{theatreId}/showtime/{showtimeId}/seat-layout
GET /v1/theatre/{theatreId}/showtime/{showtimeId}/seat-availability?preview=true
```

Layout gives rows (`A`…`J`) and seat types; availability gives `{seat_id: Available|Occupied}`.
Joining them was cross-checked against `seatsRemaining` and matched.

`seat_types` (default `["Standard"]`) excludes Wheelchair and Companion spaces, so an
accessible space opening never reads as "a seat freed up". Both IMAX rooms currently keep
those in row E only, but the filter means that isn't relied on.

**Request volume** is the cost, and three things keep it bounded:

- seat layouts are per *auditorium*, so they're fetched **twice, ever**, and cached in state
- availability is re-read only when a showtime's total seat count actually moved
- plus `seat_refresh_per_poll` (default 30) stalest showtimes each poll

That last one exists because a cancellation in row F offset by a booking in row A leaves the
total unchanged — the cheap signal would miss it. The rolling sweep means any such miss
self-heals within a full pass (~8 polls) instead of persisting. A cold start reads every seat
map once (~220 requests, ~15s); steady state is ~30.

The first run seeds the baseline silently (set `alert_on_first_run: true` to change that),
and a total API outage never wipes state, so you don't get an alert storm when it recovers.

## Current setup

Live and configured. Alerts fire on all four triggers, polling every 90s.

**ntfy — active.** The topic name is deliberately kept out of this repo (it doubles as a shared secret). Find it in your local `config.json` under `notify.ntfy.topic`, or in the `ODYSSEY_NTFY_TOPIC` GitHub secret.

Everyone who wants alerts (you included) does this once:

1. Install **ntfy** — [iOS](https://apps.apple.com/app/ntfy/id1625396347) /
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) — or just open
   <https://ntfy.sh/YOUR-TOPIC> in a browser.
2. Subscribe to that topic.

That's the whole thing — no account, no key. Anyone with the topic name receives the alerts,
which is exactly why the name is random: **treat it as a shared secret**, since anyone who
guesses it could also post to it. To rotate, change `notify.ntfy.topic` in `config.json` and
have everyone resubscribe.

**Telegram — built, needs a token to switch on:**

```bash
# 1. Message @BotFather on Telegram -> /newbot -> copy the token
export ODYSSEY_TELEGRAM_TOKEN='123456:ABC-your-token'

# 2. Have each person send the bot any message, then:
python3 odyssey_watch.py --telegram-chats     # prints their chat ids

# 3. Paste those ids into notify.telegram.chat_ids in config.json,
#    set "enabled": true, and put the token in the launchd plist's
#    EnvironmentVariables so the daemon can see it.
python3 odyssey_watch.py --test-notify
```

**macOS banners — active** as a local backstop while your Mac is awake.

## Using the bot (buttons)

`odyssey_bot.py` is the interactive half: the watcher pushes alerts at you, the bot
lets you pull. Open [@cineplex_ticket_finder_bot](https://t.me/cineplex_ticket_finder_bot)
and send anything (or `/start`) to get the menu.

```
The Odyssey — IMAX 70mm
226 showtimes in the booking window
224 with seats · 2 sold out
4 nearly gone (10 seats or fewer)

[ 🎬 Vaughan ]  [ 🎬 Mississauga Square One ]
[ 🔥 Almost gone ]  [ 🎟 Sold out ]
[ 🔄 Refresh ]
```

Tapping a theatre gives one day at a time — every showtime with a live seat count,
`Prev day` / `Next day` to move through the booking window, and a **Book** button per
showtime that opens Cineplex's seat map. That's the point: the seat map is the source of
truth, so you can tap through and confirm the bot's number against Cineplex itself.

- 🟢 plenty of seats · 🟠 10 or fewer · 🔴 sold out
- **Almost gone** — everything under 10 seats across both theatres, tightest first
- **Sold out** — exactly the showtimes being watched for cancellations
- **Refresh** re-queries Cineplex (results are cached 30s so rapid taps don't hammer it)

The bot only answers chat ids listed in `notify.telegram.chat_ids`; its username is public,
so without that anyone could drive Cineplex requests through it. Set `"bot_allow_any": true`
to lift that.

```bash
python3 odyssey_bot.py           # long-poll for taps (needs ODYSSEY_TELEGRAM_TOKEN)
python3 odyssey_bot.py --menu    # push the menu to every configured chat
```

### Alerts vs. buttons need different hosting

They're independent, and only one of them can live on GitHub Actions:

| | Alerts (`odyssey_watch.py`) | Buttons (`odyssey_bot.py`) |
|---|---|---|
| Needs | a cron | a process that's always listening |
| Runs on | GitHub Actions, 24/7 | a LaunchAgent on this Mac |
| If the Mac sleeps | unaffected | buttons stop responding until it wakes |

A button tap has to be answered within seconds, and a 5-minute cron can't do that — so the
bot can't move to Actions. It's installed as `com.moe.odyssey-bot` and restarts at login:

```bash
launchctl list | grep odyssey-bot
launchctl unload ~/Library/LaunchAgents/com.moe.odyssey-bot.plist   # stop
launchctl load   ~/Library/LaunchAgents/com.moe.odyssey-bot.plist   # start
tail -f bot.log
```

That plist holds the bot token, so it is gitignored — never commit it. For buttons that work
while the Mac is off, run `odyssey_bot.py` on any always-on box (Raspberry Pi, free-tier VM)
with `ODYSSEY_TELEGRAM_TOKEN` set. Only one process may long-poll a given bot token at a
time, so stop the local one first.

## Commands

```bash
python3 odyssey_watch.py --status         # current availability, sends nothing
python3 odyssey_watch.py --once           # one poll, alert on changes (cron mode)
python3 odyssey_watch.py --daemon         # run forever on poll_interval_seconds
python3 odyssey_watch.py --once --dry-run # detect + print, don't send or save
python3 odyssey_watch.py --test-notify    # send a test through every enabled notifier
python3 odyssey_watch.py --reset          # wipe state, next poll re-seeds
python3 odyssey_watch.py --find-film "dune"        # look up a film id
python3 odyssey_watch.py --find-theatre "vaughan"  # look up theatre ids
```

## Notifiers

Enable any combination in `config.json` under `notify`. Each one has `"enabled": true|false`,
and a failure in one never blocks the others. Any secret can be written as `"env:VAR_NAME"`
to read it from the environment instead of the file.

**`ntfy`** — best option for alerting a group. Pick an unguessable topic name, then everyone
installs the free [ntfy](https://ntfy.sh) app (iOS/Android) and subscribes to that topic. No
accounts, no API keys, and it arrives as a real push notification.

```json
"ntfy": { "enabled": true, "topic": "odyssey-70mm-8f3k2j9x", "priority": "urgent" }
```

**`email`** — SMTP to a list of recipients. For Gmail you need an
[App Password](https://myaccount.google.com/apppasswords) (regular password won't work),
which should go in `ODYSSEY_SMTP_PASSWORD` rather than in the file.

**`webhook`** — Discord or Slack incoming webhook. Set `"style": "discord"` or `"slack"`;
`mention` (e.g. `@here`) prefixes the message so the group actually gets pinged.

**`telegram`** — a bot token from @BotFather plus one or more chat ids.

**`macos`** — local desktop banner and sound. Useful as a backstop alongside a push notifier.

## Running it continuously

**Already installed and running** as a LaunchAgent — it polls every 90s, restarts if it
crashes, and starts again at login.

```bash
launchctl list | grep odyssey                                       # check it's alive
tail -f watch.log                                                   # follow it
launchctl unload ~/Library/LaunchAgents/com.moe.odyssey-watch.plist # stop it
launchctl load   ~/Library/LaunchAgents/com.moe.odyssey-watch.plist # start it again
```

To uninstall completely: `launchctl unload` as above, then
`rm ~/Library/LaunchAgents/com.moe.odyssey-watch.plist`.

The tracked file is `com.moe.odyssey-watch.plist.example`; copy it to
`com.moe.odyssey-watch.plist` (gitignored) before putting any real token in its
`EnvironmentVariables`, so a secret can never be committed by accident.

Note it reads `config.json` at startup, so **restart it (unload + load) after editing
config** — e.g. after switching Telegram on. Secrets go in the plist's
`EnvironmentVariables` block, since a LaunchAgent doesn't inherit your shell's env.

A laptop that sleeps stops polling — see the next section for 24/7 coverage.

**Don't run the LaunchAgent and GitHub Actions against the same alert channels at once.**
They keep independent state, so you'd get every alert twice. Pick one, or point the local
one at a different ntfy topic.

## Deploying to GitHub Actions (24/7, no server)

The laptop LaunchAgent stops polling when the Mac sleeps. `.github/workflows/watch.yml`
runs the same script on GitHub's runners instead, on a 5-minute cron, free.

### 1. Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name → copy the token
   (looks like `8123456789:AAH...`).
2. Decide who gets alerts:
   - **Individuals** — each person opens your bot and sends it any message (e.g. `/start`).
     Telegram won't let a bot message someone who hasn't opened the conversation first.
   - **A group** (easier for a few friends) — create a group, add the bot to it, send one
     message in it. Group chat ids are negative, e.g. `-1002345678901`.
3. Collect the ids:

   ```bash
   export ODYSSEY_TELEGRAM_TOKEN='8123456789:AAH...'
   python3 odyssey_watch.py --telegram-chats
   ```

### 2. Push the repo

```bash
cd /Users/moe/Projects/odyssey-watch
git remote add origin git@github.com:<you>/odyssey-watch.git
git push -u origin main
```

`config.json` and `state.json` are gitignored — the runner uses `config.ci.json`, which is
safe to commit because every secret in it indirects through an env var.

**Public vs private:** Actions minutes are unlimited on public repos but capped at 2,000/month
on free private ones. A 5-minute cron is ~8,640 runs/month, so **make the repo public** — no
secrets live in it — or accept that a private repo will exhaust its minutes.

### 3. Add the secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `ODYSSEY_TELEGRAM_TOKEN` | the BotFather token |
| `ODYSSEY_TELEGRAM_CHAT_IDS` | chat ids, comma separated: `12345678,-1002345678901` |
| `ODYSSEY_NTFY_TOPIC` | your ntfy topic (from local `config.json`) |

### 4. Turn it on

Actions tab → enable workflows → **Odyssey watch** → *Run workflow* → mode `test-notify`
to confirm alerts land, then `poll`. The cron takes over from there.

The manual *Run workflow* dropdown also offers `status` (print availability, send nothing)
and `reset` (re-seed the baseline without alerting).

### How state survives between runs

Each run is a fresh container, so the previous poll's seat counts come from
`actions/cache`: the run saves under `odyssey-state-<run_id>` and restores by the
`odyssey-state-` prefix, which returns the most recent entry. If the cache is ever evicted
the next run just re-seeds silently rather than alerting on all 226 showtimes. A
`concurrency` group prevents two runs overlapping and double-alerting.

### The honest tradeoff

| | Laptop LaunchAgent | GitHub Actions |
|---|---|---|
| Poll interval | 90s | 5 min floor, **often delayed 5–15 min** under load |
| Uptime | only while awake | 24/7 |

GitHub deprioritises scheduled workflows at busy times, so treat 5 minutes as a best case.
If you want both sub-minute polling *and* 24/7, run `--daemon` on an always-on box — a
Raspberry Pi, or a free-tier VM (Oracle Cloud, Fly.io) — using `config.ci.json` with the
same env vars. Nothing about the script changes.

## Retargeting it

The film and theatres are just config. `--find-film` and `--find-theatre` resolve new ids,
and `require_experience_types` accepts any combination Cineplex publishes
(`IMAX`, `70mm`, `VIP`, `UltraAVX`, `D-BOX`, `3D`, …), so the same script works for the next
sold-out release.

## Files

| File | |
|---|---|
| `odyssey_watch.py` | the whole watcher |
| `config.json` | your live config (holds recipients — not for sharing) |
| `config.example.json` | template |
| `state.json` | last poll's seat counts; delete to re-seed |
| `watch.log` | append-only poll + alert log |
