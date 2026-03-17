# ha-unifi-events

Fetches recent UniFi Protect AI detection thumbnails and stitches them into a single
mosaic image for display on a Home Assistant dashboard.

```yaml
type: custom:refreshable-picture-card
url: /local/unifi_events/recent.jpg
refresh_interval: 10
noMargin: true
```

---

## Running locally (testing)

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
python apps/recent_detections/recent_detections.py
```

Options:

| Flag                    | Default | Description                            |
| ----------------------- | ------- | -------------------------------------- |
| `--hours 4`             | `2`     | How far back to search for events      |
| `--limit 5`             | none    | Keep only the N most recent detections |
| `--types person animal` | all     | Restrict to specific detection types   |

Individual thumbnails are cached in `./output/` and the mosaic is written to `./output/recent.jpg`.
The directory is created automatically. Re-runs skip thumbnails that are already saved.

On Home Assistant, `output_dir` in `apps.yaml` controls the output location instead (default: `/homeassistant/www/unifi_events/`).

---

## Running on Home Assistant (AppDaemon via HACS)

### Prerequisites

- **HACS** installed ([instructions](https://www.hacs.xyz/docs/use/download/download/))
- **AppDaemon apps enabled in HACS**: Settings → Devices & Services → HACS → Configure → enable "AppDaemon apps discovery & tracking"
- **AppDaemon add-on** installed via Settings → Add-ons → Add-on Store → search "AppDaemon"
- **refreshable-picture-card** installed via HACS → Frontend

> **Troubleshooting:** If AppDaemon doesn't pick up the app after installing via HACS, check
> that `app_dir` in your `appdaemon.yaml` points to `/homeassistant/appdaemon/apps`. On a fresh
> AppDaemon + HACS install this should already be correct.

### Step 1 — Install Python dependencies

Go to **Settings → Add-ons → AppDaemon → Configuration** and add:

```yaml
python_packages:
  - uiprotect
  - aiofiles
  - Pillow
```

### Step 2 — Install this app via HACS

1. In HACS, click the three-dot menu (top right) → **Custom repositories**
2. Paste in this repo's GitHub URL, set category to **AppDaemon**, click **Add**
3. Find "UniFi Recent Detections" in HACS and click **Download**

HACS will place the app at `/homeassistant/appdaemon/apps/recent_detections/`.

### Step 3 — Add your credentials as secrets

In `/homeassistant/secrets.yaml` (via File Editor), add:

```yaml
unifi_protect_host: 192.168.1.1
unifi_protect_username: localadmin
unifi_protect_password: your_password_here
```

### Step 4 — Configure the app

Create (or open) `/homeassistant/appdaemon/apps/apps.yaml` in the File Editor and paste in the
`recent_detections:` block from this repo's [apps.yaml](apps.yaml). All credentials are already
referenced via `!secret` — no values to edit directly.

> If `apps.yaml` already exists with other apps in it, **merge** the `recent_detections:` block in
> rather than replacing the whole file.

### Step 5 — Restart AppDaemon

Settings → Add-ons → AppDaemon → Restart

### Step 6 — Verify

In Settings → Add-ons → AppDaemon → Log, you should see:

```
Starting apps: ['recent_detections', ...]
Connected. Fetching events from the last 2h...
Mosaic saved -> /homeassistant/www/unifi_events/recent.jpg
```

`/homeassistant/www/` is served by Home Assistant at `/local/` — the mosaic will be available at
`/local/unifi_events/recent.jpg`.

### Step 7 — Add the dashboard card

In your dashboard, add a Manual card:

```yaml
type: custom:refreshable-picture-card
url: /local/unifi_events/recent.jpg
refresh_interval: 10
noMargin: true
```

---

## Configuration reference (apps.yaml)

| Key               | Default                           | Description                                       |
| ----------------- | --------------------------------- | ------------------------------------------------- |
| `host`            | —                                 | Use `!secret unifi_protect_host`                  |
| `port`            | `443`                             | HTTPS port                                        |
| `username`        | —                                 | Use `!secret unifi_protect_username`              |
| `password`        | —                                 | Use `!secret unifi_protect_password`              |
| `verify_ssl`      | `false`                           | Set `true` if you have a valid cert               |
| `hours`           | `2`                               | How far back to search each run                   |
| `limit`           | none                              | Max detections to include in mosaic               |
| `types`           | all                               | List of: `person`, `animal`, `vehicle`, `package` |
| `interval`        | `300`                             | Seconds between runs                              |
| `output_dir`      | `/homeassistant/www/unifi_events` | Where to write thumbnails and mosaic              |
| `mosaic_filename` | `recent.jpg`                      | Filename for the combined image                   |
