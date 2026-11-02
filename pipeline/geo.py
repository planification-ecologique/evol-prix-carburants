"""Correspondances code postal -> département -> région (France métropolitaine)."""

REGIONS = {
    "11": ["75", "77", "78", "91", "92", "93", "94", "95"],
    "24": ["18", "28", "36", "37", "41", "45"],
    "27": ["21", "25", "39", "58", "70", "71", "89", "90"],
    "28": ["14", "27", "50", "61", "76"],
    "32": ["02", "59", "60", "62", "80"],
    "44": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
    "52": ["44", "49", "53", "72", "85"],
    "53": ["22", "29", "35", "56"],
    "75": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
    "76": ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"],
    "84": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
    "93": ["04", "05", "06", "13", "83", "84"],
    "94": ["2A", "2B"],
}

DEP_TO_REG = {d: r for r, deps in REGIONS.items() for d in deps}


def departement(cp: str) -> str | None:
    """Département (métropole) déduit du code postal, ou None."""
    cp = (cp or "").strip()
    if len(cp) == 4:
        cp = "0" + cp
    if len(cp) != 5 or not cp.isdigit():
        return None
    if cp.startswith("20"):
        return "2A" if int(cp) < 20200 else "2B"
    d = cp[:2]
    return d if d in DEP_TO_REG else None
