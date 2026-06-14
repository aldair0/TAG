"""Bin the 'Number' column by shape so we can see what we actually have
and whether any look date-corrupted."""

from __future__ import annotations

import csv
import re
from collections import Counter

SRC = "data/csv/tcgplayer_pricing.csv"

# Patterns that look like Excel-coerced dates (the failure mode the user
# asked about). Each card number that hits these is suspicious.
_DATE_LIKE = [
    re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$"),        # 2026-01-02
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),      # 1/2/2026, 01/02/26
    re.compile(r"^\d{1,2}-\d{1,2}-\d{2,4}$"),      # 1-2-2026
    re.compile(r"^\d{1,2}-[A-Za-z]{3,}$", re.I),   # 2-Jan, 12-Feb
    re.compile(r"^[A-Za-z]{3,}-\d{1,2}$", re.I),   # Jan-2, Feb-12
    re.compile(r"^4[0-9]{4,}$"),                   # Excel serial (post-2009 dates ~ 40000-50000+)
]


def shape_of(n: str) -> str:
    if not n:
        return "(empty)"
    for pat in _DATE_LIKE:
        if pat.match(n):
            return "DATE-LIKE (suspicious)"
    if "/" in n:
        return "X/Y (fraction)"
    if "-" in n:
        return "X-Y (hyphen)"
    if n.isdigit():
        return "all-digits"
    if any(c.isalpha() for c in n) and any(c.isdigit() for c in n):
        return "mixed alpha+digit"
    return "other"


def main() -> None:
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    suspicious: list[tuple[str, str, str]] = []  # (id, name, raw number)

    with open(SRC, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            n = (row.get("Number") or "").strip()
            shape = shape_of(n)
            counts[shape] += 1
            samples.setdefault(shape, [])
            if len(samples[shape]) < 5:
                samples[shape].append(n)
            if shape == "DATE-LIKE (suspicious)" and len(suspicious) < 20:
                suspicious.append(
                    (row.get("TCGplayer Id", ""), row.get("Product Name", ""), n)
                )

    print("=== Number column shape distribution ===")
    for shape, n in counts.most_common():
        print(f"  {shape:30s} {n:>8,}   e.g. {samples[shape][:5]}")

    print()
    print(f"Suspicious (date-like) Number values: {counts.get('DATE-LIKE (suspicious)', 0):,}")
    if suspicious:
        print("First 20 suspect rows (TCGplayer Id, Product Name, Number):")
        for r in suspicious:
            print(f"  {r}")


if __name__ == "__main__":
    main()
