"""Ground truth: one group per line, the eval's only source of truth (spec 01 §5)."""

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from crossfoot.generate.records import Event, NoiseRecord
from crossfoot.models.decision import MatchType

SCHEMA_VERSION = 1


def generator_sha() -> str:
    """The commit the dataset was generated from. Never a timestamp: bytes must be stable."""
    override = os.environ.get("CROSSFOOT_GENERATOR_SHA")
    if override:
        return override
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def event_group(event: Event) -> dict[str, Any]:
    group: dict[str, Any] = {
        "event_id": event.event_id,
        "match_type": event.match_type,
        "source_ids": sorted(
            [invoice.record_id for invoice in event.invoices]
            + [note.record_id for note in event.credit_notes]
        ),
        # Only the canonical payment records. A duplicated copy is a real record but its own
        # group, or every duplicate event would be an unmatchable two-target group (§4.9).
        "target_ids": sorted(leg.record_id for leg in event.legs),
        "evidence_ids": [] if event.remittance is None else [event.remittance.id],
        "payment_method": event.payment_method,
        "paid_on": event.paid_on.isoformat(),
        "decoys": sorted(event.decoys),
        "notes": event.notes,
    }
    if event.reasoning_subtype:
        group["reasoning_subtype"] = event.reasoning_subtype
    if event.no_match_kind:
        group["kind"] = event.no_match_kind
    return group


def duplicate_groups(event: Event) -> list[dict[str, Any]]:
    """One singleton group per re-exported copy (spec 01 §4.9, §5.1).

    An empty source side is deliberate: a copy corresponds to no commercial fact, so the right
    answer is "this matches nothing, and here is what it duplicates".
    """
    canonical = event.legs[0].record_id if event.legs else ""
    return [
        {
            "event_id": None,
            "match_type": "duplicate",
            "duplicate_of": canonical,
            "source_ids": [],
            "target_ids": [leg.record_id],
            "evidence_ids": [],
            "notes": None,
        }
        for leg in event.duplicate_legs
    ]


def noise_group(noise: NoiseRecord) -> dict[str, Any]:
    return {
        "event_id": None,
        "match_type": "no_match",
        "kind": noise.kind,
        "source_ids": [],
        "target_ids": [noise.leg.record_id],
        "evidence_ids": [],
        "notes": noise.notes,
    }


def build_groups(events: Sequence[Event], noise: Sequence[NoiseRecord]) -> list[dict[str, Any]]:
    groups = [event_group(event) for event in events]
    for event in events:
        groups += duplicate_groups(event)
    groups += [noise_group(item) for item in noise]
    for group in groups:
        missing = [record_id for record_id in group["target_ids"] if not record_id]
        if missing or any(not record_id for record_id in group["source_ids"]):
            raise AssertionError(f"group {group.get('event_id')} has an unassigned record id")
    groups.sort(key=lambda item: (item["event_id"] or "~", item["source_ids"], item["target_ids"]))
    return groups


def build_header(
    *,
    seed: int,
    kind: str,
    event_count: int,
    type_counts: dict[MatchType, int],
    events: Sequence[Event],
    noise: Sequence[NoiseRecord],
) -> dict[str, Any]:
    # All four `no_match` kinds in one dict (spec 01 §5.2): the two event orphans, which count
    # toward the 1% quota, plus the two noise kinds, which sit outside it.
    kinds: dict[str, int] = {}
    for event in events:
        if event.no_match_kind:
            kinds[event.no_match_kind] = kinds.get(event.no_match_kind, 0) + 1
    for item in noise:
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "seed": seed,
        "generator_sha": generator_sha(),
        "kind": kind,
        "event_count": event_count,
        "type_counts": dict(sorted(type_counts.items())),
        "no_match_kinds": dict(sorted(kinds.items())),
    }


def write(out_dir: Path, header: dict[str, Any], groups: Sequence[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
    lines += [json.dumps(group, sort_keys=True, separators=(",", ":")) for group in groups]
    (out_dir / "manifest.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
