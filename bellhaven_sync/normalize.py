"""Normalisation helpers for addresses, names and care types."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

SUFFIX = {
    "street": "st", "str": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "boulevard": "blvd", "blv": "blvd", "lane": "ln", "pk": "pike", "pke": "pike", "court": "ct",
    "place": "pl", "parkway": "pkwy", "highway": "hwy", "circle": "cir", "terrace": "ter",
    "trail": "trl", "square": "sq", "suite": "ste",
}
DIRECTION = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
PO_BOX_RE = re.compile(r"\b(p\.?\s*o\.?\s*box|post office box)\b", re.I)


def _tokens(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().replace("&", " and ")).split()


def street_key(street: str) -> str:
    """'4850 Northwest Sylvania Avenue' -> '4850 nw sylvania ave'."""
    out = []
    for t in _tokens(street):
        t = DIRECTION.get(t, t)
        t = SUFFIX.get(t, t)
        out.append(t)
    return " ".join(out)


def street_number(street: str) -> str:
    m = re.match(r"\s*(\d+)", street or "")
    return m.group(1) if m else ""


def is_po_box(street: str) -> bool:
    return bool(PO_BOX_RE.search(street or ""))


def zip5(z: str) -> str:
    return (z or "").strip()[:5]


NAME_SYNONYM = {
    "rehab": "rehabilitation", "centre": "center", "ctr": "center", "and": "and",
    "healthcare": "health care", "hc": "health care", "snf": "skilled nursing",
}


def name_key(name: str) -> str:
    toks = []
    for t in _tokens(name):
        if t == "the":
            continue
        toks.extend(NAME_SYNONYM.get(t, t).split())
    return " ".join(toks)


def name_similarity(a: str, b: str) -> float:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return 0.0
    seq = SequenceMatcher(None, " ".join(sorted(ka.split())), " ".join(sorted(kb.split()))).ratio()
    sa, sb = set(ka.split()), set(kb.split())
    jac = len(sa & sb) / len(sa | sb)
    return round(max(seq, jac), 3)


# Website care offering -> CRM care_type picklist value.
CARE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short-term rehabilitation and nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}


def crm_care_types(offerings: list[str]) -> list[str]:
    out = []
    for o in offerings:
        v = CARE_MAP.get(o.strip().lower())
        if v and v not in out:
            out.append(v)
    return out
