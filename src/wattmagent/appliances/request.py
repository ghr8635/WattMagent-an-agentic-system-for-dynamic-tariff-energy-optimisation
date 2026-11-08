"""Reading an appliance request such as "run the dishwasher before 6pm".

Deterministic, like the default charging parser: it finds which household appliances the text
names, and for each a deadline ("before", "by", "until", "done by") and/or an earliest start
("after", "not before", "from"). Times reuse the charging parser's rules, so spoken forms
("half past five", "6 in the evening") work the same way. Anything the text does not say is
``None``; ``agent.py`` decides what that means.

A time with no cue word ("at 6pm") is read as a deadline, exactly as for charging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time

from wattmagent.appliances.household import CycleEntry, Household
from wattmagent.graph.request import (
    find_clock_times,
    is_earliest_cue,
    normalise_text,
    split_clauses,
)

# Everyday names for the catalogue's appliances. The catalogue key and the household's own
# name for an appliance always count too, so a new catalogue entry works without editing this.
_EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "washing_machine": ("washer", "laundry", "washing"),
    "dishwasher": ("dish washer", "dishes"),
    "tumble_dryer": ("dryer", "drier", "tumble drier"),
}
_EV_WORDS = re.compile(r"\b(car|ev|vehicle|charge|charging|charged|battery)\b|%|percent", re.I)


@dataclass(frozen=True, slots=True)
class ApplianceAsk:
    entry: CycleEntry
    deadline: time | None = None
    earliest: time | None = None


@dataclass(frozen=True, slots=True)
class ParsedApplianceRequest:
    asks: tuple[ApplianceAsk, ...] = ()
    ambiguous: tuple[str, ...] = ()  # words that matched more than one household appliance
    mentions_ev: bool = False
    notes: list[str] = field(default_factory=list)


def _phrases(entry: CycleEntry) -> set[str]:
    key = entry.model.key
    names = {key.replace("_", " "), entry.name.replace("_", " ")}
    for prefix, extra in _EXTRA_ALIASES.items():
        if key.startswith(prefix):
            names.update(extra)
            names.add(prefix.replace("_", " "))
    return {n.lower() for n in names if n}


def _alias_table(household: Household) -> dict[str, list[CycleEntry]]:
    table: dict[str, list[CycleEntry]] = {}
    for entry in household.cycles:
        for phrase in _phrases(entry):
            table.setdefault(phrase, []).append(entry)
    return table


def _find_entries(
    clause: str, table: dict[str, list[CycleEntry]]
) -> tuple[list[CycleEntry], list[str]]:
    """Entries named in ``clause`` (longest phrase wins at a position) and ambiguous phrases."""
    found: list[CycleEntry] = []
    ambiguous: list[str] = []
    taken: list[tuple[int, int]] = []
    for phrase in sorted(table, key=len, reverse=True):
        for m in re.finditer(rf"\b{re.escape(phrase)}\b", clause):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            entries = table[phrase]
            if len(entries) > 1:
                ambiguous.append(phrase)
            else:
                found.append(entries[0])
    return found, ambiguous


def mentions_appliance(text: str, household: Household) -> bool:
    low = normalise_text(text)
    found, ambiguous = _find_entries(low, _alias_table(household))
    return bool(found or ambiguous)


def parse_appliance_request(text: str, household: Household) -> ParsedApplianceRequest:
    low = normalise_text(text)
    table = _alias_table(household)
    asks: dict[str, ApplianceAsk] = {}
    ambiguous: list[str] = []
    pending: list[CycleEntry] = []
    recent: list[CycleEntry] = []  # appliances of the last clause that named some

    def apply(entries: list[CycleEntry], deadline: time | None, earliest: time | None) -> None:
        for e in entries:
            old = asks.get(e.name)
            asks[e.name] = ApplianceAsk(
                e,
                deadline or (old.deadline if old else None),
                earliest or (old.earliest if old else None),
            )

    for clause in split_clauses(low):
        entries, amb = _find_entries(clause, table)
        ambiguous.extend(amb)
        deadline = earliest = None
        for t, prefix in find_clock_times(clause):
            if is_earliest_cue(prefix):
                earliest = earliest or t
            else:
                deadline = deadline or t
        if entries and (deadline or earliest):
            recent = pending + entries
            apply(recent, deadline, earliest)
            pending = []
        elif entries:
            pending.extend(entries)  # "dishwasher and washer before 6pm": the time comes next
            recent = []
        elif deadline or earliest:
            # "dryer after 9am, done by 10pm": a clause with only a time belongs to the
            # appliances before it
            target = pending or recent
            apply(target, deadline, earliest)
            if pending:
                recent, pending = pending, []
    apply(pending, None, None)  # named, with no time at all
    return ParsedApplianceRequest(
        asks=tuple(asks.values()),
        ambiguous=tuple(dict.fromkeys(ambiguous)),
        mentions_ev=bool(_EV_WORDS.search(low)),
    )
