"""Run ONLY the IMAP IDLE receiver and log its activity at INFO.

Throwaway watcher for live verification — not part of the app. Logs every
sale email it parses and flags. Ctrl-C / kill to stop.
"""

from __future__ import annotations

import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from app.inbound_email.receiver import ImapIdleReceiver

r = ImapIdleReceiver()
r.start()
logging.getLogger("watch").info("watcher up — waiting for incoming mail (Ctrl-C to stop)")
try:
    while True:
        time.sleep(5)
except KeyboardInterrupt:
    r.stop()
