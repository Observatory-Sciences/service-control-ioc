"""Render scenario evidence as a markdown table: a row per scenario, a column
per target, each cell ``pass (<time_to_final_s> s)`` or ``FAIL (...)``.

    PYTHONPATH=tests uv run python -m integration.summarise [EVIDENCE_DIR]

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


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else evidence_dir()
    sys.stdout.write(render(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
