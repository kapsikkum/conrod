"""Race number -> driver / team / class keyword mapping.

The CSV needs a 'number' column. Every other column becomes a keyword, so the
same loader handles a two-column grid and a full entry list without changes.
"""

from __future__ import annotations

import csv
from pathlib import Path


class NumberMap:
    def __init__(self, rows: dict[str, dict[str, str]] | None = None):
        self.rows = rows or {}

    @classmethod
    def load(cls, path: Path) -> "NumberMap":
        rows: dict[str, dict[str, str]] = {}
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return cls(rows)
            number_field = _find_number_field(reader.fieldnames)
            if not number_field:
                raise ValueError(
                    f"{path} has no 'number' column (found: {reader.fieldnames})"
                )
            for row in reader:
                key = _canonical(row.get(number_field, ""))
                if not key:
                    continue
                rows[key] = {
                    k.strip(): (v or "").strip()
                    for k, v in row.items()
                    if k and k != number_field and (v or "").strip()
                }
        return cls(rows)

    def keywords_for(self, number: str, prefix: str = "") -> list[str]:
        """Keywords to write for one detected number."""
        key = _canonical(number)
        out = [f"{prefix}{number}", f"{prefix}#{number}", f"{prefix}Car {number}"]
        for value in self.rows.get(key, {}).values():
            # A cell may hold several values, e.g. "Repco;Castrol".
            for part in str(value).replace(";", ",").split(","):
                part = part.strip()
                if part:
                    out.append(f"{prefix}{part}")
        # Stable order, no duplicates.
        seen: set[str] = set()
        return [k for k in out if not (k in seen or seen.add(k))]

    def describe(self, number: str) -> str:
        row = self.rows.get(_canonical(number))
        return ", ".join(row.values()) if row else ""

    def __len__(self) -> int:
        return len(self.rows)


def _find_number_field(fields) -> str | None:
    for candidate in ("number", "no", "no.", "num", "car", "race number", "racenumber"):
        for field in fields:
            if field and field.strip().lower() == candidate:
                return field
    return None


def _canonical(value: str) -> str:
    """'#07 ' and '7' are the same competitor as far as lookup is concerned."""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits.lstrip("0") or digits
