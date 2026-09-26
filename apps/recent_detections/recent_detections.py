"""
recent_detections.py

Fetches the most recent UniFi Protect AI detection events and saves their
thumbnails to disk, then writes a JSON manifest for the custom HA card to render.

Can run in two modes:
  - AppDaemon app (event driven, runs inside Home Assistant)
  - CLI script (one-shot, for local testing)
"""

import argparse
import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
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

JPEG_MAGIC = b"\xff\xd8"
SENSOR     = "sensor.unifi_detections_updated"

# While anything in the feed is pending, poll on a steady fast cadence for the first
# stretch (thumbnails normally land well inside it), then slow down for the long tail.
# The HA sensor tracks motion, not the Protect event, so it can stay "on" minutes after
# the event ended and the thumbnail became available - waiting for it to drop is what
# made thumbnails show up late.
TIMING_HEADER = ("downloaded_at,kind,event_id,camera,type,watched,"
                 "event_start,event_end,start_to_download_s,end_to_download_s\n")


# ── AppDaemon app ──────────────────────────────────────────────────────────────

try:
    import appdaemon.plugins.hass.hassapi as hass

    class RecentDetections(hass.Hass):
        """Event driven: a detection starting shows a placeholder immediately, a
        detection ending fetches the thumbnail (which is when Protect generates it),
        and a periodic run reconciles anything the sensors missed."""

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
            self.cleanup     = bool(self.args.get("cleanup", True))

            # How long to keep chasing a pending thumbnail before giving up on it.
            self.followup_seconds = float(self.args.get("followup_seconds", 600))
            # Poll cadence while a detection is pending: steady and tight early, then
            # slower for the tail. Each poll is a single filtered query (~40 ms).
            self.poll_fast        = float(self.args.get("poll_fast_interval", 3))
            self.poll_fast_window = float(self.args.get("poll_fast_window", 90))
            self.poll_slow        = float(self.args.get("poll_slow_interval", 15))
            # Optional CSV of real thumbnail timings, for tuning the cadence.
            self.timing_log = Path(self.args["timing_log"]) if self.args.get("timing_log") else None
            # How long a sensor placeholder survives without a matching Protect event.
            self.pending_ttl    = float(self.args.get("pending_ttl", 600))
            self.match_window   = float(self.args.get("pending_match_window", 180))
            self.cleanup_grace  = float(self.args.get("cleanup_grace", 600))

            raw_types = self.args.get("types")
            self.watch_types = (
                {TYPE_MAP[t] for t in raw_types if t in TYPE_MAP}
                if raw_types else ALL_WATCH_TYPES
            )

            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.feed_path = self.output_dir / "recent.json"

            self._client       = None
            self._fetch_lock   = asyncio.Lock()   # one network fetch at a time
            self._write_lock   = asyncio.Lock()   # one writer for recent.json
            self._coalesce     = False
            self._refresh      = False            # re-download the bootstrap next fetch
            self._entries      = []               # last Protect-derived feed
            self._pending      = []               # sensor placeholders awaiting a Protect event
            self._published    = None             # last payload written, to skip no-op updates
            self._followup     = None             # handle for the scheduled pending re-check
            self._recent_names = deque(maxlen=3)  # filenames recently published, spared by cleanup

            for sensor in self.args.get("trigger_sensors", []) or []:
                state = await self.get_state(sensor)
                if state is None:
                    self.log(f"Trigger sensor {sensor} is unknown to Home Assistant - it will "
                             f"never fire. Check the entity id.", level="WARNING")
                else:
                    self.log(f"Watching {sensor} (currently {state})")
                await self.listen_state(self._on_detection_start, sensor, new="on")
                await self.listen_state(self._on_detection_end, sensor, old="on", new="off")

            await self._run_fetch("startup")
            await self.run_every(self._on_timer, f"now+{self.interval}", self.interval)

        # ── triggers ───────────────────────────────────────────────────────────

        async def _on_detection_start(self, entity, attribute, old, new, kwargs=None, **extra):
            """Show a typed icon right away, then fetch: Protect creates the event when
            the detection starts, so the real entry replaces this within a second."""
            detection_type = next((t for t in TYPE_MAP if t in entity), "person")
            attrs = await self.get_state(entity, attribute="all") or {}
            camera_id = (attrs.get("attributes") or {}).get("camera_id")
            self._pending.append({
                "ts":        _now().isoformat(),
                "camera":    "unknown",
                "camera_id": camera_id,
                "type":      detection_type,
                "url":       None,
                "pending":   True,
            })
            self.log(f"Detection started: {entity} ({detection_type})")
            await self._publish("detection started")
            await self._run_fetch(f"{detection_type} started")

        async def _on_detection_end(self, entity, attribute, old, new, kwargs=None, **extra):
            """The detection ended, which is when Protect generates the thumbnail."""
            self.log(f"Detection ended: {entity}")
            await self._run_fetch(f"{entity} ended")

        async def _on_timer(self, kwargs=None, **extra):
            self._refresh = True
            await self._run_fetch("timer")

        # ── fetching ───────────────────────────────────────────────────────────

        async def _run_fetch(self, reason):
            """Only one fetch runs at a time. Requests that arrive during one are
            coalesced into a single re-run instead of stacking up parallel chains."""
            if self._fetch_lock.locked():
                self._coalesce = True
                self.log(f"Fetch already running, coalescing: {reason}")
                return
            async with self._fetch_lock:
                while True:
                    await self._fetch_once(reason)
                    if not self._coalesce:
                        return
                    self._coalesce = False
                    reason = "coalesced request"

        async def _fetch_once(self, reason):
            self.log(f"Fetching ({reason})")
            try:
                client = await self._get_client()
                entries = await _fetch(
                    client=client,
                    hours=self.hours,
                    watch_types=self.watch_types,
                    count=self.count,
                    output_dir=self.output_dir,
                    web_root=self.web_root,
                    log=self.log,
                    timing_log=self.timing_log,
                    # Events we were already polling for; rows without this flag are
                    # backfills (e.g. the first run after a restart) and would skew stats.
                    watched_ids={t.get("id") for t in self._entries
                                 if t.get("pending") or t.get("provisional")},
                )
            except Exception as err:
                self.log(f"Fetch failed: {err}", level="WARNING")
                await self._close_client()   # reconnect on the next run
                return
            await self._publish(reason, entries)

        # ── publishing ─────────────────────────────────────────────────────────

        async def _publish(self, reason, entries=None):
            """Writes recent.json and pokes the card's sensor when the feed actually
            changed, and re-arms the pending follow-up either way."""
            async with self._write_lock:
                if entries is not None:
                    self._entries = entries
                feed = self._merge_pending(self._entries)
                if self.count:
                    feed = feed[:self.count]
                pending = sum(1 for t in feed if t.get("pending"))
                provisional = sum(1 for t in feed if t.get("provisional"))

                payload = json.dumps(feed, sort_keys=True)
                if payload != self._published:
                    self._published = payload
                    _write_json(self.feed_path, {"updated": _now().isoformat(), "thumbnails": feed})
                    self._recent_names.append({Path(t["url"]).name for t in feed if t.get("url")})
                    self.log(f"Feed saved -> {self.feed_path} ({len(feed)} entries, "
                             f"{pending} pending, {provisional} provisional) [{reason}]")

                    await self.set_state(SENSOR, state=_now().isoformat(), attributes={
                        "friendly_name": "UniFi detections updated",
                        "entries": len(feed),
                        "pending": pending,
                        "provisional": provisional,
                        "latest":  feed[0]["ts"] if feed else None,
                    })

                # Outside the "changed" branch on purpose: a superseded provisional
                # image only becomes deletable once its grace period passes, which is
                # usually after the feed has stopped changing.
                if self.cleanup and entries is not None and self._recent_names:
                    _cleanup(self.output_dir, set().union(*self._recent_names),
                             self.log, self.cleanup_grace)

                await self._schedule_followup(feed)

        async def _schedule_followup(self, feed):
            """Protect generates a thumbnail when the event ends, which is often well
            before the HA motion sensor drops. So while anything is pending, keep
            re-checking on a backoff instead of waiting for the next scheduled run."""
            if self._followup is not None:
                try:
                    await self.cancel_timer(self._followup)
                except Exception:
                    pass
                self._followup = None

            ages = [(_now() - _parse_ts(t["ts"])).total_seconds()
                    for t in feed if t.get("pending") or t.get("provisional")]
            if not ages or min(ages) > self.followup_seconds:
                return

            delay = self.poll_fast if min(ages) < self.poll_fast_window else self.poll_slow
            self._followup = await self.run_in(self._on_followup, delay)

        async def _on_followup(self, kwargs=None, **extra):
            self._followup = None
            await self._run_fetch("pending follow-up")

        def _merge_pending(self, entries):
            """Sensor placeholders stay in the feed until Protect reports the matching
            event (or they age out), so the icon never blinks out mid-detection."""
            now  = _now()
            kept = []
            for placeholder in self._pending:
                if (now - _parse_ts(placeholder["ts"])).total_seconds() > self.pending_ttl:
                    continue
                if any(self._covers(entry, placeholder) for entry in entries):
                    continue
                kept.append(placeholder)
            self._pending = kept
            return sorted(entries + kept, key=lambda t: t["ts"], reverse=True)

        def _covers(self, entry, placeholder):
            """True when a Protect event is the one a sensor placeholder stood in for."""
            if entry.get("type") != placeholder.get("type"):
                return False
            if placeholder.get("camera_id") and entry.get("camera_id") != placeholder.get("camera_id"):
                return False
            drift = abs((_parse_ts(entry["ts"]) - _parse_ts(placeholder["ts"])).total_seconds())
            return drift <= self.match_window

        # ── connection ─────────────────────────────────────────────────────────

        async def _get_client(self):
            """One long-lived client: logging in and downloading the bootstrap on every
            poll was most of the per-fetch cost."""
            if self._client is None:
                self.log(f"Connecting to {self.host}:{self.port}...")
                self._client = ProtectApiClient(
                    host=self.host, port=self.port,
                    username=self.username, password=self.password,
                    verify_ssl=self.verify_ssl,
                )
                await self._client.update()
                self._refresh = False
                self.log(f"Connected ({len(self._client.bootstrap.cameras)} cameras)")
            elif self._refresh:
                await self._client.update()   # picks up camera renames
                self._refresh = False
            return self._client

        async def _close_client(self):
            if self._client is not None:
                try:
                    await self._client.close_session()
                except Exception:
                    pass
                self._client = None

        async def terminate(self):
            await self._close_client()

except ImportError:
    pass  # Not running under AppDaemon — CLI mode only


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _now():
    return datetime.now(tz=timezone.utc)


def _parse_ts(value):
    return datetime.fromisoformat(value)


def _safe_name(name):
    """Keeps a camera name usable in a filename and a URL ('#', '?' or '%' would 404)."""
    cleaned = "".join(
        c if (c.isalnum() and c.isascii()) or c in "-_" else "_"
        for c in name.strip().lower().replace(" ", "_")
    )
    return cleaned.strip("_") or "unknown"


def _write_json(path, payload):
    """Writes via temp file + rename so the card never reads a half-written feed."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


async def _write_bytes(path, data):
    """Same for thumbnails: a partial .jpg gets cached by the browser for a month."""
    tmp = path.with_name(f".{path.name}.tmp")
    async with aiofiles.open(tmp, "wb") as f:
        await f.write(data)
    os.replace(tmp, path)


def _append_timing(path, event, camera_name, primary, downloaded_at, watched, kind="final"):
    """Appends one row per saved thumbnail so the poll cadence can be tuned against real
    numbers. `watched` marks events we were actively polling for."""
    if path is None:
        return
    try:
        header_needed = not path.exists()
        end = event.end
        start_delta = f"{(downloaded_at - event.start).total_seconds():.1f}"
        end_delta = f"{(downloaded_at - end).total_seconds():.1f}" if end else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            if header_needed:
                f.write(TIMING_HEADER)
            f.write(f"{downloaded_at.isoformat()},{kind},{event.id},{camera_name},{primary},"
                    f"{int(bool(watched))},{event.start.isoformat()},"
                    f"{end.isoformat() if end else ''},{start_delta},{end_delta}\n")
    except Exception:
        pass   # timing data is a nicety; never let it break a fetch


def _cleanup(output_dir, keep, log, grace_seconds):
    """Deletes thumbnails that dropped out of the feed. Files newer than the grace
    period are spared, so a card still showing the previous feed never 404s."""
    cutoff = time.time() - grace_seconds
    for f in output_dir.glob("*.jpg"):
        if f.name in keep:
            continue
        try:
            if f.stat().st_mtime > cutoff:
                continue
            f.unlink()
        except FileNotFoundError:
            continue
        log(f"Cleaned up old snapshot: {f.name}")


# ── Shared fetch logic ─────────────────────────────────────────────────────────

async def _get_jpeg(client, path, log):
    """Returns JPEG bytes, or None if Protect hasn't got that image yet (it 404s)."""
    try:
        data = await client.api_request_raw(path, raise_exception=False)
    except Exception as err:
        log(f"    -> Error on {path}: {err}")
        return None
    return data if data and data[:2] == JPEG_MAGIC else None


async def _fetch(*, client, hours, watch_types, count, output_dir, web_root, log,
                 timing_log=None, watched_ids=()):
    """Fetches recent smart detections and downloads any thumbnail we don't have yet.

    Returns the feed, newest first. Events still in progress, and ended events whose
    thumbnail Protect hasn't generated yet, are included as pending placeholders so the
    card shows a typed icon in the right slot instead of the entry vanishing.
    """
    now   = _now()
    since = now - timedelta(hours=hours)

    # Passing `types` matters: without it uiprotect has to page through every event
    # type in the window, 100 at a time, because of a Protect API bug.
    query = {"types": [EventType.SMART_DETECT]}
    if watch_types != ALL_WATCH_TYPES:
        query["smart_detect_types"] = list(watch_types)
    if count:
        query["limit"] = max(count * 3, count + 10)

    events = await client.get_events(start=since, end=now, sorting="desc", **query)

    detections = [
        e for e in events
        if e.type == EventType.SMART_DETECT
        and any(t in watch_types for t in (e.smart_detect_types or []))
    ]
    detections.sort(key=lambda e: e.start, reverse=True)
    if count:
        detections = detections[:count]

    log(f"Found {len(detections)} matching detection(s) (out of {len(events)} smart detect events)")

    entries = []
    for event in detections:
        types       = [t.value for t in event.smart_detect_types if t in watch_types]
        primary     = types[0] if types else "person"
        camera      = client.bootstrap.cameras.get(event.camera_id) if event.camera_id else None
        camera_name = _safe_name(camera.name) if camera else "unknown"

        entry = {
            "id":        event.id,
            "ts":        event.start.isoformat(),
            "camera":    camera_name,
            "camera_id": event.camera_id,
            "type":      primary,
        }

        stamp       = event.start.astimezone().strftime('%Y%m%d_%H%M%S')
        final_name  = f"{stamp}_{camera_name}_{primary}.jpg"
        prelim_name = f"{stamp}_{camera_name}_{primary}_live.jpg"
        final_path  = output_dir / final_name
        prelim_path = output_dir / prelim_name

        if final_path.exists() and final_path.stat().st_size > 0:
            entries.append({**entry, "url": f"{web_root}/{final_name}"})
            continue

        # `thumbnails/<id>` only answers once Protect has generated the final image, so
        # its 404 doubles as a "not ready yet" signal. The events list is no help here:
        # it can still report the event as running ~20s after it actually ended.
        final = await _get_jpeg(client, f"thumbnails/{event.thumbnail_id}", log) \
            if event.thumbnail_id else None
        if final:
            await _write_bytes(final_path, final)
            got_at = _now()
            since_end = f", {(got_at - event.end).total_seconds():.1f}s after it ended" if event.end else ""
            log(f"    -> {final_path} ({len(final)/1024:.1f} KB{since_end})")
            _append_timing(timing_log, event, camera_name, primary, got_at,
                           event.id in watched_ids)
            entries.append({**entry, "url": f"{web_root}/{final_name}"})
            continue

        # No final yet. While the event is still running, grab one forced live snapshot
        # to show instead of an icon. Protect's own event thumbnail at this stage is just
        # the camera's last cached frame, which can be a minute stale and show an empty
        # scene - worse than an icon. This gets its own filename, because reusing the
        # final's URL would pin the provisional in the browser cache for a month.
        if event.end is None and not (prelim_path.exists() and prelim_path.stat().st_size > 0):
            prelim = None
            if event.camera_id:
                try:
                    prelim = await client.get_camera_snapshot(event.camera_id)
                except Exception as err:
                    log(f"    -> Error fetching live snapshot: {err}")
                if prelim and prelim[:2] != JPEG_MAGIC:
                    prelim = None
            if prelim:
                await _write_bytes(prelim_path, prelim)
                got_at = _now()
                log(f"  {primary} on '{camera_name}' at "
                    f"{event.start.astimezone().strftime('%H:%M:%S')}: live snapshot "
                    f"({len(prelim)/1024:.1f} KB, {(got_at - event.start).total_seconds():.1f}s in)")
                _append_timing(timing_log, event, camera_name, primary, got_at,
                               event.id in watched_ids, kind="prelim")

        if prelim_path.exists() and prelim_path.stat().st_size > 0:
            # Real image on screen, but keep checking for the final one.
            entries.append({**entry, "url": f"{web_root}/{prelim_name}", "provisional": True})
        else:
            entries.append({**entry, "url": None, "pending": True})

    return entries


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
    parser.add_argument("--timing-log", default=None, metavar="PATH",
                        help="Append thumbnail timing rows to this CSV (for tuning the poll cadence)")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="Keep old snapshot files that are no longer in the event feed")
    args = parser.parse_args()

    watch = {TYPE_MAP[t] for t in args.types} if args.types else ALL_WATCH_TYPES
    output_dir = Path(cfg.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    async def _main():
        client = ProtectApiClient(
            host=cfg.HOST, port=cfg.PORT,
            username=cfg.USERNAME, password=cfg.PASSWORD,
            verify_ssl=cfg.VERIFY_SSL,
        )
        try:
            _log.info(f"Connecting to {cfg.HOST}:{cfg.PORT}...")
            await client.update()
            entries = await _fetch(
                client=client,
                hours=args.hours,
                watch_types=watch,
                count=args.count,
                output_dir=output_dir,
                web_root=args.web_root,
                log=_log.info,
                timing_log=Path(args.timing_log) if args.timing_log else None,
            )
            feed_path = output_dir / "recent.json"
            _write_json(feed_path, {"updated": _now().isoformat(), "thumbnails": entries})
            pending = sum(1 for t in entries if t.get("pending"))
            _log.info(f"Event feed saved -> {feed_path} ({len(entries)} entries, {pending} pending)")
            if not args.no_cleanup:
                keep = {Path(t["url"]).name for t in entries if t.get("url")}
                _cleanup(output_dir, keep, _log.info, 600)
        finally:
            await client.close_session()

    asyncio.run(_main())
