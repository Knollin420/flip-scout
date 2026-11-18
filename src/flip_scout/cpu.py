"""Fixed rules: is a CPU good enough for an energy-saving laptop / mini PC?

The AI only reads the exact CPU name and its number of cores from the listing.
Whether that CPU is good enough is decided here, the same way every time.

Wanted: mobile CPUs (low idle power), at least 4 cores, at least Intel N100 level:
- Intel N100, N150, N200, N250, N300, N305 (also as "Core i3-N305")
- Intel Core i3 from the 11th generation (with at least 4 cores), i5/i7/i9 from the 8th
- Intel Core 3/5/7 and Core Ultra (2023 and later)
- AMD Ryzen 5/7/9 from the 3000 series, Ryzen 3 from the 4000 series
- Apple M1 and later
Never: MediaTek, Snapdragon and other ARM chips (except Apple), Celeron, Pentium, Atom,
Athlon, Core m / Y, AMD A-/E-series, desktop CPUs (mini PCs: T/K/no suffix).
"""

from __future__ import annotations

import re

UNKNOWN = "CPU nicht erkennbar"

_NEVER = (
    "mediatek",
    "kompanio",
    "snapdragon",
    "qualcomm",
    "exynos",
    "rockchip",
    "allwinner",
    "celeron",
    "pentium",
    "atom",
    "athlon",
    "sempron",
    "core m",
    "core 2",
    "core2",
    "duo",
    "amd a4",
    "amd a6",
    "amd a8",
    "amd a9",
    "amd a10",
    "amd a12",
    "amd e1",
    "amd e2",
)
_INTEL_N_OK = {100, 150, 200, 250, 300, 305}
# Mobile suffixes. Intel: U, H(Q/K/S/X), P, G1-G7, V; Y is mobile but too weak.
_INTEL_MOBILE = re.compile(r"^(u|h|hq|hk|hs|hx|p|g[1-7]|v)$")
_AMD_MOBILE = re.compile(r"^(u|h|hs|hx)$")


def _gen_from_words(text: str) -> int | None:
    """'8th gen', '11. generation', '12e génération' -> generation number."""
    m = re.search(r"\b(\d{1,2})\s*(?:th|st|nd|rd|\.|e|ª|°)?\s*gen", text)
    return int(m.group(1)) if m else None


def cpu_problem(
    cpu: str, cores: int | None = None, mobile_suffix_required: bool = False
) -> str | None:
    """None = good enough; UNKNOWN = not identifiable; otherwise the reason it is not.

    mobile_suffix_required: mini PCs often have desktop T-CPUs, so there the suffix must
    prove a mobile CPU. In laptops CPUs are mobile anyway."""
    c = " ".join((cpu or "").lower().replace("®", "").replace("™", "").replace("(r)", "").split())
    if not c or c in {"unbekannt", "unklar", "?", "-"}:
        return UNKNOWN
    if any(word in c for word in _NEVER):
        return f"CPU zu schwach: {cpu}"
    if cores is not None and 0 < cores < 4:
        return f"CPU hat nur {cores} Kerne: {cpu}"

    # Apple M1, M2 Pro, ...
    if re.search(r"\b(apple\s*)?m[1-9]\b", c) and ("apple" in c or re.match(r"^m[1-9]\b", c)):
        return None

    # Intel N series (Intel N100 / Intel Processor N150 / Core i3-N305)
    m = re.search(r"(?:^|[\s-])n(\d{2,4})\b", c)
    if m and ("intel" in c or "i3" in c or c.startswith("n")):
        n = int(m.group(1))
        return None if n in _INTEL_N_OK else f"CPU zu schwach: {cpu}"

    # Intel Core Ultra 5 125U / Core 5 120U / Core Ultra 7 258V (2023 and later)
    m = re.search(r"core\s*(?:ultra\s*)?[3579]\s*(\d{3})([a-z]*)\b", c)
    if m and not re.search(r"\bi[3579]\b", c):
        suffix = m.group(2)
        if mobile_suffix_required and not _INTEL_MOBILE.match(suffix or ""):
            return f"keine Mobil-CPU: {cpu}"
        return None

    # Intel Core i3/i5/i7/i9
    m = re.search(r"\bi([3579])(?:\s*-\s*|\s+)?(\d{4,5})([a-z]{0,2}\d?)\b", c)
    if m:
        tier, number, suffix = int(m.group(1)), m.group(2), m.group(3)
        # i5-8250U -> 8; i5-10210U, i7-1165G7, i5-1235U -> 10, 11, 12 (first two digits)
        gen = int(number[:2]) if len(number) == 5 or number[0] == "1" else int(number[0])
        if suffix == "y" or suffix.startswith("y"):
            return f"CPU zu schwach (Y-Serie): {cpu}"
        if mobile_suffix_required and not _INTEL_MOBILE.match(suffix):
            return f"keine Mobil-CPU: {cpu}"
        return _intel_core_problem(tier, gen, cores, cpu)
    m = re.search(r"\bi([3579])\b", c)
    if m:  # 'Intel Core i5 8th Gen' without model number
        gen = _gen_from_words(c)
        if gen is None or mobile_suffix_required:
            return UNKNOWN
        return _intel_core_problem(int(m.group(1)), gen, cores, cpu)

    # AMD Ryzen 5 3500U / Ryzen 7 PRO 4750U
    m = re.search(r"ryzen\s*([3579])\s*(?:pro\s*)?(\d{4})\s*([a-z]{0,2})\b", c)
    if m:
        tier, number, suffix = int(m.group(1)), m.group(2), m.group(3)
        gen = int(number[0])
        if not _AMD_MOBILE.match(suffix):
            if mobile_suffix_required or suffix:
                return f"keine Mobil-CPU: {cpu}"
        if tier == 3 and gen < 4:
            return f"CPU zu schwach: {cpu}"
        if gen < 3:
            return f"CPU zu alt: {cpu}"
        return None
    if "ryzen" in c:
        return UNKNOWN
    if re.search(r"\b(intel|core|amd)\b", c):
        return UNKNOWN  # family known, exact model not
    return f"CPU nicht in der Liste: {cpu}"


def _intel_core_problem(tier: int, gen: int, cores: int | None, cpu: str) -> str | None:
    if tier == 3:
        if gen < 11:
            return f"i3 vor der 11. Generation: {cpu}"
        if gen == 11 and not (cores and cores >= 4):
            return f"i3 11. Gen meist nur 2 Kerne: {cpu}"  # i3-1115G4; i3-1125G4 has 4
        return None
    if gen < 8:
        return f"CPU zu alt (vor 8. Generation): {cpu}"
    return None
