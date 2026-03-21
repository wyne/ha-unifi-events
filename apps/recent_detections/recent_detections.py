"""
recent_detections.py

Fetches the most recent UniFi Protect AI detection events and saves their
thumbnails to disk, then writes a JSON manifest for the custom HA card to render.

Can run in two modes:
  - AppDaemon app (scheduled, runs inside Home Assistant)
  - CLI script (one-shot, for local testing)
"""

import asyncio
import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiofiles
from uiprotect import ProtectApiClient
from uiprotect.data.types import SmartDetectObjectType, EventType

TYPE_MAP = {
    "person":  SmartDetectObjectType.PERSON,
    "animal":  SmartDetectObjectType.ANIMAL,
    "vehicle": SmartDetectObjectType.VEHICLE,
    "package": SmartDetectObjectType.PACKAGE,
}

ALL_WATCH_TYPES = set(TYPE_MAP.values())


# ── AppDaemon app ──────────────────────────────────────────────────────────────

try:
    import appdaemon.plugins.hass.hassapi as hass

    class RecentDetections(hass.Hass):

        async def initialize(self):
            self.host        = self.args["host"]
            self.port        = int(self.args.get("port", 443))
            self.username    = self.args["username"]
            self.password    = self.args["password"]
            self.verify_ssl  = bool(self.args.get("verify_ssl", False))
            self.hours       = float(self.args.get("hours", 2.0))
            self.count       = int(self.args["count"]) if "count" in self.args else None
            self.interval    = int(self.args.get("interval", 300))
            self.output_dir  = Path(self.args.get("output_dir", "/homeassistant/www/unifi_events"))
            self.web_root    = self.args.get("web_root", "/local/unifi_events")

            raw_types = self.args.get("types")
            self.watch_types = (
                {TYPE_MAP[t] for t in raw_types if t in TYPE_MAP}
                if raw_types else ALL_WATCH_TYPES
            )

            self.output_dir.mkdir(parents=True, exist_ok=True)

            self.trigger_delay            = int(self.args.get("trigger_delay", 120))
            self.trigger_poll_interval    = int(self.args.get("trigger_poll_interval", 5))   # seconds between post-trigger polls
            self.trigger_poll_count       = int(self.args.get("trigger_poll_count", 12))     # max polls before giving up
            self._trigger_polls_remaining = 0  # counts down after a sensor trigger; drives fast polling until a new thumbnail is found

            trigger_sensors = self.args.get("trigger_sensors", [])
            for sensor in trigger_sensors:
                self.listen_state(self.on_sensor_trigger, sensor, new="on")

            await self._do_fetch(trigger="startup")
            self.run_every(self._do_fetch, f"now+{self.interval}", self.interval)

        def on_sensor_trigger(self, entity, attribute, old, new, kwargs):
            """Fired by a HA binary sensor (e.g. person/vehicle detected). Injects a placeholder
            into the feed immediately so the card shows something, then arms fast polling and
            schedules the first real fetch after trigger_delay. Sync required by AppDaemon."""
            self._trigger_polls_remaining = self.trigger_poll_count
            detection_type = next(
                (t for t in ["person", "vehicle", "animal", "package"] if t in entity),
                "person",
            )
            self.log(f"Triggered by state change: {entity} — injecting placeholder, fetching in {self.trigger_delay}s")
            self._inject_placeholder(detection_type)
            # Signal the card immediately so it shows the placeholder icon now,
            # before the thumbnail is ready.
            self.set_state(
                "sensor.unifi_detections_updated",
                state=f"pending_{datetime.now(tz=timezone.utc).isoformat()}",
                attributes={"pending": True, "type": detection_type},
            )
            self.run_in(self._do_fetch, self.trigger_delay)

        def _inject_placeholder(self, detection_type):
            """Prepends a null-URL entry to recent.json so the card shows a typed icon
            immediately while the real thumbnail is still being fetched."""
            feed_path = self.output_dir / "recent.json"
            try:
                event_feed = json.loads(feed_path.read_text()) if feed_path.exists() else {"thumbnails": []}
            except Exception:
                event_feed = {"thumbnails": []}
            event_feed["thumbnails"].insert(0, {
                "url": None,
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "camera": "unknown",
                "type": detection_type,
                "pending": True,
            })
            if self.count:
                event_feed["thumbnails"] = event_feed["thumbnails"][:self.count]
            event_feed["updated"] = datetime.now(tz=timezone.utc).isoformat()
            feed_path.write_text(json.dumps(event_feed))

        async def _do_fetch(self, kwargs=None, trigger=None):
            """Runs a full fetch, signals the card, and continues fast polling if a sensor
            trigger is active and no new thumbnail has been found yet."""
            if trigger == "startup":
                self.log("Triggered by startup")
            elif kwargs:
                self.log("Triggered by timer")
            else:
                self.log("Fetching after trigger delay")
            found_new = await _fetch(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                verify_ssl=self.verify_ssl,
                hours=self.hours,
                watch_types=self.watch_types,
                count=self.count,
                output_dir=self.output_dir,
                web_root=self.web_root,
                log=self.log,
            )
            self.set_state(
                "sensor.unifi_detections_updated",
                state=datetime.now(tz=timezone.utc).isoformat(),
            )
            if self._trigger_polls_remaining > 0:
                if found_new:
                    self.log("Post-trigger poll complete: new thumbnail found")
                    self._trigger_polls_remaining = 0
                else:
                    self._trigger_polls_remaining -= 1
                    self.log(f"Post-trigger poll, {self._trigger_polls_remaining} remaining")
                    self.run_in(self._do_fetch, self.trigger_poll_interval)

except ImportError:
    pass  # Not running under AppDaemon — CLI mode only


# ── Shared fetch logic ─────────────────────────────────────────────────────────

async def _fetch(*, host, port, username, password, verify_ssl,
                 hours, watch_types, count, output_dir, web_root, log):
    """Connects to UniFi Protect, fetches completed smart detect events from the last `hours`,
    downloads any missing thumbnails, and writes recent.json. Returns True if at least one
    new thumbnail was saved (used to stop post-trigger polling early)."""
    now   = datetime.now(tz=timezone.utc)
    since = now - timedelta(hours=hours)

    log(f"Connecting to {host}:{port}...")
    client = ProtectApiClient(
        host=host, port=port,
        username=username, password=password,
        verify_ssl=verify_ssl,
    )

    try:
        await client.update()
        log(f"Connected. Fetching events from the last {hours}h...")

        events = await client.get_events(start=since, end=now)

        detections = [
            e for e in events
            if e.type == EventType.SMART_DETECT
            and e.end is not None
            and e.thumbnail_id is not None
            and any(t in watch_types for t in (e.smart_detect_types or []))
        ]

        detections.sort(key=lambda e: e.start, reverse=True)
        if count is not None:
            detections = detections[:count]

        log(f"Found {len(detections)} matching detection(s) (out of {len(events)} total events)")

        feed_entries = []
        found_new    = False  # set to True when a thumbnail is newly downloaded; stops post-trigger polling
        for event in detections:
            types       = [t.value for t in event.smart_detect_types if t in watch_types]
            primary     = types[0]
            camera_name = "unknown"
            if event.camera_id in client.bootstrap.cameras:
                camera_name = client.bootstrap.cameras[event.camera_id].name.lower().replace(" ", "_")

            event_ts   = event.start.astimezone().strftime("%Y%m%d_%H%M%S")
            filename   = f"{event_ts}_{camera_name}_{primary}.jpg"
            out_path   = output_dir / filename
            feed_entry = {
                "url":    f"{web_root}/{filename}",
                "ts":     event.start.isoformat(),
                "camera": camera_name,
                "type":   primary,
            }

            if out_path.exists():
                feed_entries.append(feed_entry)
                continue

            log(f"  Fetching: {primary} on '{camera_name}' at {event_ts} (score={event.score})")
            try:
                thumb = await client.api_request_raw(f"thumbnails/{event.thumbnail_id}", raise_exception=False)
                if thumb:
                    async with aiofiles.open(out_path, "wb") as f:
                        await f.write(thumb)
                    log(f"    -> {out_path} ({len(thumb)/1024:.1f} KB)")
                    feed_entries.append(feed_entry)
                    found_new = True
                else:
                    log(f"    -> No thumbnail available for event {event.id}")
            except Exception as e:
                log(f"    -> Error: {e}")

        if feed_entries:
            feed_path = output_dir / "recent.json"
            feed_path.write_text(json.dumps({"updated": now.isoformat(), "thumbnails": feed_entries}))
            log(f"Event feed saved -> {feed_path} ({len(feed_entries)} entries)")

        return found_new

    except Exception as e:
        log(f"fetch failed: {e}")
        return False
    finally:
        await client.close_session()


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        import local_config as cfg
    except ImportError:
        print("ERROR: local_config.py not found.")
        print("Copy local_config.example.py to local_config.py and fill in your credentials.")
        raise SystemExit(1)

    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    _log = logging.getLogger(__name__)

    parser = argparse.ArgumentParser()
    parser.add_argument("--hours",     type=float, default=2.0,  help="Hours back to look (default: 2)")
    parser.add_argument("--count",     type=int,   default=None, metavar="N",
                        help="Max thumbnails to include in the event feed. "
                             "Should be >= the card's lightbox_count (default: all)")
    parser.add_argument("--web-root",  default="/local/unifi_events",
                        help="URL prefix for thumbnail paths in the event feed (default: /local/unifi_events)")
    parser.add_argument("--types",  nargs="+",  default=None,
                        choices=["person", "animal", "vehicle", "package"],
                        help="Detection types to fetch (default: all)")
    args = parser.parse_args()

    watch = {TYPE_MAP[t] for t in args.types} if args.types else ALL_WATCH_TYPES
    output_dir = Path(cfg.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    asyncio.run(_fetch(
        host=cfg.HOST,
        port=cfg.PORT,
        username=cfg.USERNAME,
        password=cfg.PASSWORD,
        verify_ssl=cfg.VERIFY_SSL,
        hours=args.hours,
        watch_types=watch,
        count=args.count,
        output_dir=output_dir,
        web_root=args.web_root,
        log=lambda msg: _log.info(msg),
    ))
