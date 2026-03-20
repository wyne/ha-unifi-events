import asyncio, sys
sys.path.insert(0, "apps/recent_detections")
from datetime import datetime, timezone, timedelta
import local_config as cfg
from uiprotect import ProtectApiClient
from uiprotect.data.types import EventType

async def debug():
    client = ProtectApiClient(
        host=cfg.HOST, port=cfg.PORT,
        username=cfg.USERNAME, password=cfg.PASSWORD,
        verify_ssl=cfg.VERIFY_SSL,
    )
    await client.update()
    now = datetime.now(tz=timezone.utc)
    events = await client.get_events(start=now - timedelta(hours=24), end=now)
    detections = [
        e for e in events
        if e.type == EventType.SMART_DETECT and e.end is not None
        and e.thumbnail_id is not None
    ]
    detections.sort(key=lambda e: e.start, reverse=True)
    for e in detections[:5]:
        tid = e.thumbnail_id.replace('e-', '')
        print(f"id={e.id}")
        print(f"  thumbnail_id={e.thumbnail_id}  score={e.score}")
        print(f"  start={e.start.astimezone()}  end={e.end.astimezone() if e.end else None}")
        for path in [
            f"events/{tid}/thumbnail",
            f"thumbnails/{e.thumbnail_id}",
            f"thumbnails/{tid}",
            f"events/{e.id}/snapshot",
        ]:
            raw = await client.api_request_raw(path, raise_exception=False)
            print(f"  {path}: {'OK ' + str(len(raw)) + 'B' if raw else 'EMPTY'}")
    await client.close_session()

asyncio.run(debug())
