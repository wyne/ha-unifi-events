# ha-unifi-events

Display recent [UniFi Protect](https://ui.com/camera-security) AI detection thumbnails on your Home Assistant dashboard — persons, vehicles, animals, and packages — with instant updates via sensor triggers.

![Card screenshot](card.png)

```yaml
type: custom:unifi-events-card
url: /local/unifi_events/recent.json
entity: sensor.unifi_detections_updated
count: 3
lightbox_count: 9
cols: 3
refresh_interval: 300
```

---

## Installation

### Prerequisites

- **HACS** installed ([instructions](https://www.hacs.xyz/docs/use/download/download/))
- **AppDaemon apps enabled in HACS**: Settings → Devices & Services → HACS → Configure → enable "AppDaemon apps discovery & tracking"
- **AppDaemon add-on** installed via Settings → Add-ons → Add-on Store → search "AppDaemon"

### Step 1 — Point AppDaemon at the HACS app directory (one-time)

By default AppDaemon stores apps in its own isolated config volume, separate from where HACS installs them. This one-time change aligns them. You only do this once, regardless of how many HACS AppDaemon apps you install.

From the Home Assistant CLI (e.g. the Proxmox console), type `login` to get a root bash shell:

```bash
vi /mnt/data/supervisor/addon_configs/a0d7b954_appdaemon/appdaemon.yaml
```

Find the `app_dir` line and change it to:

```yaml
app_dir: /homeassistant/appdaemon/apps
```

Save and exit. After this change, AppDaemon will look in the same directory that HACS uses, and you can manage `apps.yaml` via the File Editor.

### Step 2 — Install Python dependencies

Go to **Settings → Add-ons → AppDaemon → Configuration** and add:

```yaml
python_packages:
  - uiprotect
  - aiofiles
```

### Step 3 — Install this app via HACS

1. In HACS, click the three-dot menu (top right) → **Custom repositories**
2. Paste `https://github.com/wyne/ha-unifi-events`, set category to **AppDaemon**, click **Add**
3. Find "UniFi Recent Detections" in HACS and click **Download**

HACS will place the app at `/homeassistant/appdaemon/apps/recent_detections/`.

### Step 4 — Install the Lovelace card via HACS

1. In HACS, click the three-dot menu (top right) → **Custom repositories**
2. Paste `https://github.com/wyne/ha-unifi-events-card`, set category to **Frontend**, click **Add**
3. Find "UniFi Events Card" in HACS and click **Download**

HACS will install the card and register it as a Lovelace resource automatically.

### Step 5 — Add your credentials as secrets

In `/homeassistant/secrets.yaml` (via File Editor), add:

```yaml
unifi_protect_host: 192.168.1.1
unifi_protect_username: localadmin
unifi_protect_password: your_password_here
```

### Step 6 — Configure the app

Create (or open) `/homeassistant/appdaemon/apps/apps.yaml` in the File Editor and paste in the
`recent_detections:` block from this repo's [apps.yaml](apps.yaml). All credentials are already
referenced via `!secret` — no values to edit directly.

> If `apps.yaml` already exists with other apps in it, **merge** the `recent_detections:` block in
> rather than replacing the whole file.

### Step 7 — Restart AppDaemon

Settings → Add-ons → AppDaemon → Restart

### Step 8 — Verify

In Settings → Add-ons → AppDaemon → Log, you should see:

```
Starting apps: ['recent_detections', ...]
Connected. Fetching events from the last 2h...
Event feed saved -> /homeassistant/www/unifi_events/recent.json (6 entries)
```

`/homeassistant/www/` is served by Home Assistant at `/local/` — the event feed will be available at
`/local/unifi_events/recent.json`.

### Step 9 — Add the dashboard card

In your dashboard, add a **Manual card**:

```yaml
type: custom:unifi-events-card
url: /local/unifi_events/recent.json
entity: sensor.unifi_detections_updated
count: 3
lightbox_count: 6
cols: 3
refresh_interval: 300
```

| Key                | Default | Description                                                                                               |
| ------------------ | ------- | --------------------------------------------------------------------------------------------------------- |
| `url`              | —       | Path to `recent.json` (required)                                                                          |
| `entity`           | —       | HA entity ID updated by AppDaemon on new detections; triggers instant card refresh with zero idle polling |
| `count`            | `3`     | Thumbnails shown in the card grid                                                                         |
| `lightbox_count`   | `6`     | Thumbnails shown when the card is tapped                                                                  |
| `cols`             | `3`     | Columns per row in both the grid and lightbox                                                             |
| `refresh_interval` | `300`   | Fallback polling interval in seconds (only active if `entity` is not set or as a safety net)              |

---

## How updates reach the card

A detection goes through three stages, so something useful is on screen almost immediately:

1. **Icon (~1s)** — a trigger sensor flips `on` and a typed placeholder is written to the feed straight away.
2. **Live snapshot (~3s)** — while the event is still running, a forced camera snapshot stands in. Protect's own
   event thumbnail is not usable here: during an event it returns the camera's last cached frame, which can be a
   minute stale and show an empty scene.
3. **Final crop (~15s after the event ends)** — Protect only generates the real, object-centred thumbnail once the
   event is over. `thumbnails/<id>` returns 404 until then, which is exactly the readiness signal the app polls for.

While anything is unresolved the app re-checks every `poll_fast_interval` seconds instead of waiting for the next
scheduled run. Waiting on the trigger sensor to clear is not good enough: those sensors track motion, not the
Protect event, and can stay `on` for minutes after the thumbnail already exists. Each stage writes a different
filename, because `/local` is served with a month-long cache header and reusing a URL would pin the older image.

---

## Configuration reference (apps.yaml)

| Key                    | Default                           | Description                                                                                                   |
| ---------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `host`                 | —                                 | Use `!secret unifi_protect_host`                                                                              |
| `port`                 | `443`                             | HTTPS port                                                                                                    |
| `username`             | —                                 | Use `!secret unifi_protect_username`                                                                          |
| `password`             | —                                 | Use `!secret unifi_protect_password`                                                                          |
| `verify_ssl`           | `false`                           | Set `true` if you have a valid cert                                                                           |
| `hours`                | `2`                               | How far back to search each run                                                                               |
| `count`                | none (all)                        | Max thumbnails to include in the event feed                                                                   |
| `types`                | all                               | List of: `person`, `animal`, `vehicle`, `package`                                                             |
| `interval`             | `300`                             | Seconds between scheduled runs                                                                                |
| `followup_seconds`     | `600`                             | How long to keep re-checking while a detection is pending. Covers the gap between Protect finishing an event and the HA motion sensor dropping |
| `poll_fast_interval`   | `3`                               | Seconds between checks while a pending detection is younger than `poll_fast_window`                          |
| `poll_fast_window`     | `300`                             | How long the fast cadence lasts before dropping to `poll_slow_interval`                                      |
| `poll_slow_interval`   | `5`                               | Seconds between checks for the long tail                                                                     |
| `timing_log`           | none                              | Optional CSV path; one row per saved thumbnail with start/end/download deltas, for tuning the cadence         |
| `pending_ttl`          | `600`                             | How long a placeholder from a sensor trigger survives without a matching Protect event                        |
| `pending_match_window` | `180`                             | How far apart a placeholder and a Protect event can be and still be treated as the same detection             |
| `cleanup`              | `true`                            | Delete thumbnails that drop out of the feed                                                                   |
| `cleanup_grace`        | `600`                             | Seconds before an unreferenced thumbnail is deleted, so a card one feed behind never 404s                    |
| `output_dir`           | `/homeassistant/www/unifi_events` | Where to write thumbnails and the event feed                                                                  |
| `web_root`             | `/local/unifi_events`             | URL prefix for thumbnail paths in the event feed                                                              |
| `trigger_sensors`      | `[]`                              | HA binary sensors that mark a detection. Going `on` shows a placeholder at once; going `off` fetches the thumbnail. Entity IDs must match exactly — the app logs a warning at startup for any it can't find |

---

## Local testing

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Create your local config**

```bash
cp local_config.example.py local_config.py
```

Edit `local_config.py` with your UniFi Protect credentials. This file is gitignored and never copied to Home Assistant.

**3. Run**

```bash
cd apps/recent_detections
python3 recent_detections.py --count 6
```

| Flag                    | Default               | Description                                          |
| ----------------------- | --------------------- | ---------------------------------------------------- |
| `--hours 4`             | `2`                   | How far back to search for events                    |
| `--count 6`             | none (all)            | Max thumbnails to include in the event feed          |
| `--web-root /local/...` | `/local/unifi_events` | URL prefix for thumbnail paths written to the JSON   |
| `--types person animal` | all                   | Restrict to specific detection types                 |
| `--timing-log PATH`     | off                   | Append one CSV row per saved thumbnail, for tuning   |

Thumbnails are cached in `apps/recent_detections/output/` and the event feed is written to
`apps/recent_detections/output/recent.json` (both relative to where you run the script).
Re-runs skip thumbnails that are already saved.

**4. Preview in browser**

Use [ha-unifi-events-card](https://github.com/wyne/ha-unifi-events-card) — it includes `test_card.html` and reads from `output/recent.json`.
