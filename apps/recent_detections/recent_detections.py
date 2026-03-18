"""
recent_detections.py

Fetches the most recent UniFi Protect AI detection events and stitches their
thumbnails into a single wide mosaic image.

Can run in two modes:
  - AppDaemon app (scheduled, runs inside Home Assistant)
  - CLI script (one-shot, for local testing)
"""

import asyncio
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiofiles
from PIL import Image, ImageDraw, ImageFont
from uiprotect import ProtectApiClient
from uiprotect.data.types import SmartDetectObjectType, EventType

TYPE_MAP = {
    "person":  SmartDetectObjectType.PERSON,
    "animal":  SmartDetectObjectType.ANIMAL,
    "vehicle": SmartDetectObjectType.VEHICLE,
    "package": SmartDetectObjectType.PACKAGE,
}

ALL_WATCH_TYPES = set(TYPE_MAP.values())


def _label_from_path(path: Path) -> str:
    """Parse filename into a fuzzy relative time string."""
    parts = path.stem.split("_")
    dt = datetime.strptime(f"{parts[0]}{parts[1]}", "%Y%m%d%H%M%S")
    seconds = int((datetime.now() - dt).total_seconds())
    minutes = seconds // 60
    hours   = minutes // 60
    days    = seconds // 86400
    if seconds < 60:
        return "now"
    elif minutes < 60:
        return f"{minutes}m"
    elif hours < 24:
        return f"{hours}h"
    elif days < 7:
        return f"{days}d"
    else:
        return f"{days // 7}w"


def _add_overlay(img: Image.Image, label: str) -> Image.Image:
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.load_default(size=40)
    except TypeError:
        font = ImageFont.load_default()
    margin = 10
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x, y = margin, img.height - text_h - margin * 2
    draw.rectangle([x - margin, y, x + text_w + 12, y + text_h + 20], fill=(0, 0, 0, 160))
    draw.text((x, y), label, font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(img, overlay).convert("RGB")


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
            self.limit       = self.args.get("limit")
            self.interval    = int(self.args.get("interval", 300))
            self.output_dir  = Path(self.args.get("output_dir", "/homeassistant/www/unifi_events"))
            self.mosaic_path = self.output_dir / self.args.get("mosaic_filename", "recent.jpg")

            raw_types = self.args.get("types")
            self.watch_types = (
                {TYPE_MAP[t] for t in raw_types if t in TYPE_MAP}
                if raw_types else ALL_WATCH_TYPES
            )

            self.output_dir.mkdir(parents=True, exist_ok=True)

            trigger_sensors = self.args.get("trigger_sensors", [])
            for sensor in trigger_sensors:
                self.listen_state(self.fetch_detections, sensor, new="on")

            await self.fetch_detections(trigger="startup")
            self.run_every(self.fetch_detections, f"now+{self.interval}", self.interval)

        async def fetch_detections(self, entity=None, attribute=None, old=None, new=None, trigger=None, **kwargs):
            if entity:
                self.log(f"Triggered by state change: {entity}")
            elif trigger == "startup":
                self.log("Triggered by startup")
            else:
                self.log("Triggered by timer")
            await _fetch(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                verify_ssl=self.verify_ssl,
                hours=self.hours,
                watch_types=self.watch_types,
                limit=self.limit,
                output_dir=self.output_dir,
                mosaic_path=self.mosaic_path,
                log=self.log,
            )

except ImportError:
    pass  # Not running under AppDaemon — CLI mode only


# ── Shared fetch logic ─────────────────────────────────────────────────────────

async def _fetch(*, host, port, username, password, verify_ssl,
                 hours, watch_types, limit, output_dir, mosaic_path, log):
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
        if limit is not None:
            detections = detections[:int(limit)]

        log(f"Found {len(detections)} matching detection(s) (out of {len(events)} total events)")

        saved_paths = []
        for event in detections:
            types       = [t.value for t in event.smart_detect_types if t in watch_types]
            primary     = types[0]
            camera_name = "unknown"
            if event.camera_id in client.bootstrap.cameras:
                camera_name = client.bootstrap.cameras[event.camera_id].name.lower().replace(" ", "_")

            event_ts = event.start.astimezone().strftime("%Y%m%d_%H%M%S")
            filename = f"{event_ts}_{camera_name}_{primary}.jpg"
            out_path = output_dir / filename

            if out_path.exists():
                log(f"  Skipping (already saved): {filename}")
                saved_paths.append(out_path)
                continue

            log(f"  Fetching: {primary} on '{camera_name}' at {event_ts} (score={event.score})")
            try:
                thumb = await client.api_request_raw(f"thumbnails/{event.thumbnail_id}", raise_exception=False)
                if thumb:
                    async with aiofiles.open(out_path, "wb") as f:
                        await f.write(thumb)
                    log(f"    -> {out_path} ({len(thumb)/1024:.1f} KB)")
                    saved_paths.append(out_path)
                else:
                    log(f"    -> Empty response for event {event.id}")
            except Exception as e:
                log(f"    -> Error: {e}")

        if saved_paths:
            images   = [Image.open(p) for p in saved_paths]
            target_h = min(img.height for img in images)
            resized  = [
                img.resize((int(img.width * target_h / img.height), target_h))
                for img in images
            ]
            labeled  = [_add_overlay(img, _label_from_path(p)) for img, p in zip(resized, saved_paths)]
            panel_w  = labeled[0].width
            n_panels = int(limit) if limit is not None else len(labeled)
            mosaic   = Image.new("RGB", (panel_w * n_panels, target_h))
            for i, img in enumerate(labeled):
                x = i * panel_w + (panel_w - img.width) // 2
                mosaic.paste(img, (x, 0))
            mosaic.save(mosaic_path, quality=85)
            log(f"Mosaic saved -> {mosaic_path} ({mosaic.width}x{mosaic.height})")

    except Exception as e:
        log(f"fetch failed: {e}")
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
    parser.add_argument("--hours",  type=float, default=2.0,  help="Hours back to look (default: 2)")
    parser.add_argument("--limit",  type=int,   default=None, help="Max number of events to fetch")
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
        limit=args.limit,
        output_dir=output_dir,
        mosaic_path=output_dir / "recent.jpg",
        log=lambda msg: _log.info(msg),
    ))
