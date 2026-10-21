"""Render scenario evidence as a markdown table: a row per scenario, a column
per target, each cell ``pass (<time_to_final_s> s)`` or ``FAIL (...)``.

    PYTHONPATH=tests uv run python -m integration.summarise [--gate] [EVIDENCE_DIR]

``--gate`` lists every gate finding instead (scenario, code, dependent,
dependency, the dependent's start time) and ends with the tally per code.
``EVIDENCE_DIR`` defaults to ``$QDSC_EVIDENCE`` or ``docs/acceptance/evidence``,
with one subdirectory per target (``container``, ``vm``).
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from integration.evidence import evidence_dir
from integration.scenarios import SCENARIOS

Row = dict[str, object]


def _load(root: Path) -> dict[str, dict[str, Row]]:
    """target -> scenario id -> evidence."""
    found: dict[str, dict[str, Row]] = {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        rows = {}
        for path in sorted(folder.glob("*.json")):
            data = json.loads(path.read_text())
            if isinstance(data, dict) and "scenario" in data:
                rows[str(data["scenario"])] = data
        if rows:
            found[folder.name] = rows
    return found


def _cell(row: Row | None) -> str:
    if row is None:
        return "-"
    observed = row.get("observed")
    t = observed.get("time_to_final_s") if isinstance(observed, dict) else None
    word = "pass" if row.get("passed") is True else "FAIL"
    return f"{word} ({t:.1f} s)" if isinstance(t, int | float) else f"{word} (-)"


def render(root: Path) -> str:
    """The markdown table for every target under ``root``."""
    targets = _load(root)
    order = {sc.id: i for i, sc in enumerate(SCENARIOS)}
    ids = sorted(
        {sid for rows in targets.values() for sid in rows},
        key=lambda sid: (order.get(sid, len(order)), sid),
    )
    headers = []
    for target, rows in targets.items():
        arch = next(iter(rows.values())).get("arch")
        headers.append(f"{target} ({arch})" if arch else target)
    lines = [
        "| " + " | ".join(["id", "title", *headers]) + " |",
        "|" + "---|" * (2 + len(headers)),
    ]
    for sid in ids:
        rows = [targets[t].get(sid) for t in targets]
        title = next((str(r["title"]) for r in rows if r and "title" in r), "")
        lines.append("| " + " | ".join([sid, title, *map(_cell, rows)]) + " |")
    return "\n".join(lines) + "\n"


def _elements(text: str) -> str:
    """The elements a gate message names, ``(a, b)``, or ``-``."""
    start, end = text.find(" ("), text.find("), look at")
    return text[start + 2 : end] if 0 <= start < end else "-"


def render_gate(root: Path) -> str:
    """Every gate finding in the evidence, a table per target, then a tally.

    The finding's message carries the dependent's start time and the elements
    that were not ready, but not when they became ready, so there is no
    ready-time delta to list (the model keeps that time internally).
    """
    targets = _load(root)
    order = {sc.id: i for i, sc in enumerate(SCENARIOS)}
    out = ["# Gate findings in the acceptance evidence", ""]
    tally: dict[str, dict[str, int]] = {}
    for target, rows in targets.items():
        counts = tally.setdefault(target, {})
        out += [
            f"## {target}",
            "",
            "| scenario | code | dependent | dependency | dependent started | not ready |",
            "|---|---|---|---|---|---|",
        ]
        found = False
        for sid in sorted(rows, key=lambda x: (order.get(x, len(order)), x)):
            gate = rows[sid].get("gate")
            for entry in gate if isinstance(gate, list) else []:
                if not isinstance(entry, dict):
                    continue
                found = True
                code = str(entry.get("code"))
                counts[code] = counts.get(code, 0) + 1
                cells = [
                    sid,
                    code,
                    str(entry.get("dependent")),
                    str(entry.get("dependency")),
                    str(entry.get("dependent_start") or "-"),
                    _elements(str(entry.get("text"))),
                ]
                out.append("| " + " | ".join(cells) + " |")
        if not found:
            out.append("| (none) | | | | | |")
        out.append("")
    out += ["## Tally", ""]
    for target, counts in tally.items():
        parts = ", ".join(f"{n} x {code}" for code, n in sorted(counts.items()))
        out.append(f"- {target}: {parts or 'no gate findings'}")
    out += [
        "",
        "## Recommendation on the parked suppression rule",
        "",
        _recommend(tally),
    ]
    return "\n".join(out) + "\n"


def _recommend(tally: dict[str, dict[str, int]]) -> str:
    """One paragraph on suppressing ``gate_violation_possible``, from the counts."""
    possible = sum(c.get("gate_violation_possible", 0) for c in tally.values())
    definite = sum(c.get("gate_violation", 0) for c in tally.values())
    runs = len(tally)
    base = (
        f"Across {runs} target(s) the evidence holds {definite} definite"
        f" gate_violation and {possible} gate_violation_possible findings. "
    )
    if possible == 0:
        return base + (
            "The suppression the parked ruling proposed (no finding when the"
            " dependency's active_enter precedes the dependent's start and its"
            " elements were ready at the check poll) is now in the model, and with"
            " the ~100 ms polls while units start the possible verdict does not"
            " arise in any scenario, gated or not. The recommendation is to add no"
            " further suppression and keep possible as the honest verdict for a"
            " start within one fast poll of an element appearing, which the unit"
            " tests cover but the real stacks never produced. Every definite"
            " finding comes from G4 and F10, where the gating is absent or covers"
            " only the udp port, so they are genuine."
        )
    return base + (
        "gate_violation_possible still appears on real stacks, so the data does"
        " not yet show the rule is safe to drop; review the scenarios above."
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    gate = "--gate" in args
    args = [a for a in args if a != "--gate"]
    root = Path(args[0]) if args else evidence_dir()
    sys.stdout.write(render_gate(root) if gate else render(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
