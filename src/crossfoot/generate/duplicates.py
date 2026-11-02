"""Deciding which copy of a duplicated payment record is canonical (spec 01 §4.9).

"The earlier id is canonical." For a bank line the id is `bank:<value date>:<row>`, and the row
number comes from the deterministic shuffle `emit/bank.py` applies within the day -- so which of
the two copies is earlier is not knowable while the event is being built. This pass runs after
the emitters have assigned ids and swaps the pair if the shuffle put the copy first.

Settlement ids are content-derived hex with no ordering meaning, so for those "earlier" is just
the lexicographic tie-break; it is stable, which is what the manifest needs.
"""

from collections.abc import Sequence

from crossfoot.generate.records import Event


def resolve(events: Sequence[Event]) -> None:
    """Order each duplicate pair so `legs[0]` is canonical and `duplicate_legs[0]` the copy."""
    for event in events:
        if not event.duplicate_legs:
            continue
        if len(event.legs) != 1 or len(event.duplicate_legs) != 1:
            raise AssertionError(
                f"{event.event_id}: a duplicate event must hold exactly one pair, "
                f"got {len(event.legs)} and {len(event.duplicate_legs)}"
            )
        canonical, copy = event.legs[0], event.duplicate_legs[0]
        if not canonical.record_id or not copy.record_id:
            raise AssertionError(f"{event.event_id}: duplicate pair has an unassigned record id")
        if canonical.record_id == copy.record_id:
            raise AssertionError(f"{event.event_id}: duplicate pair shares one record id")
        if copy.record_id < canonical.record_id:
            event.legs[0], event.duplicate_legs[0] = copy, canonical
