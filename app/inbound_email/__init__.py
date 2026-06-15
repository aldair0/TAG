"""Always-on sale-notification email receiver.

Listens to the store inbox over IMAP IDLE for TCGPlayer "items have sold"
emails, parses each into a structured sale (see
``app.sync.tcgplayer.email_parser``), and flags the matching inventory
units as *sold online* — a soft, auto-expiring POS block that holds until
the authoritative TCGPlayer CSV sync decrements the stock for real.

Public API:
- :func:`flag_email_sale` — the inventory action (match + flag).
- :func:`expiry_for_flag` — shared flag-expiry math.
- :func:`start_email_receiver` / :func:`stop_email_receiver` — lifecycle.
"""

from __future__ import annotations

from app.inbound_email.flagger import (
    FlagSummary,
    ItemMatch,
    expiry_for_flag,
    flag_email_sale,
)
from app.inbound_email.receiver import (
    start_email_receiver,
    stop_email_receiver,
)

__all__ = [
    "FlagSummary",
    "ItemMatch",
    "expiry_for_flag",
    "flag_email_sale",
    "start_email_receiver",
    "stop_email_receiver",
]
