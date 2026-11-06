"""Compatibility shim: the Xsi-Events engine now lives in sipphone.presence."""
from .presence import manager as _m                                    # noqa: F401
from .presence.connection import ASYNC_PATH, EVENTS_PATH, EXPIRES, APP_ID, EventsClient   # noqa: F401
from .presence.event_processor import DocSplitter, parse_call           # noqa: F401
from .presence.manager import PresenceManager, STALE_AFTER              # noqa: F401
from .presence.models import status_of                                  # noqa: F401
from .presence.subscriptions import PACKAGES                            # noqa: F401


def __getattr__(name):
    if name == "HEARTBEAT":
        return _m.HEARTBEAT
    raise AttributeError(name)


def set_heartbeat(seconds: float) -> None:
    _m.HEARTBEAT = seconds
