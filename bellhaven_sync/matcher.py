"""Match website locations to CRM accounts and turn the differences into proposals.

Principles
----------
* A facility is a building. The street address (number + normalised street,
  confirmed by zip or city/state) is the primary key; the name only confirms.
  Names change on every rebrand, addresses almost never do. This is what stops
  us linking "Amberly Manor, Hudson OH" to "Amberly Manor, Colorado Springs CO"
  or "Bellhaven of Carlisle, PA" to "Bellhaven of New Carlisle, OH".
* A name-only match is allowed only when the CRM address is unusable (blank or
  a PO box) and city/state agree. It is flagged as medium confidence.
* Several live accounts at one address = duplicates. One survivor is chosen by
  a transparent ranking; the rest are pointed at it and made Inactive.
* Moving an account to a new parent always goes through the CHOW SOP.
* Nothing here writes to the CRM. It only produces proposals.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

from . import config
from .normalize import (crm_care_types, is_po_box, name_similarity, street_key,
                        street_number, zip5)
from difflib import SequenceMatcher

NAME_OK = 0.85


def _key(*parts) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16] + ":" + str(parts[0])


def snap(a: dict) -> dict:
    keep = ("account_id", "name", "parent_id", "parent_name", "billing_street", "billing_city",
            "billing_state", "billing_zip", "care_type", "status", "phone", "lifetime_revenue",
            "outstanding_ar", "chow_current_account", "duplicate_of_account", "note")
    return {k: a.get(k) for k in keep}


def has_billing_lock(a: dict) -> bool:
    """CHOW SOP: revenue history AND outstanding AR > 0 means the account is frozen."""
    return float(a.get("lifetime_revenue") or 0) > 0 and float(a.get("outstanding_ar") or 0) > 0


@dataclass
class Ctx:
    accounts: list[dict]
    by_id: dict
    target: dict
    acquired_ids: set
    contacts_per_account: dict


def build_ctx(accounts: list[dict], contacts: list[dict]) -> Ctx:
    by_id = {a["account_id"]: a for a in accounts}
    target = next((a for a in accounts if a["name"] == config.TARGET_PARENT_NAME), None)
    if not target:
        raise RuntimeError(f"Parent account '{config.TARGET_PARENT_NAME}' not found in CRM")
    acquired = {a["account_id"] for a in accounts if a["name"] in config.ACQUIRED_PARENT_NAMES}
    cpa = defaultdict(int)
    for c in contacts:
        if c.get("is_active", True):
            cpa[c["account_id"]] += 1
    return Ctx(accounts, by_id, target, acquired, cpa)


def is_parent_account(a: dict, ctx: Ctx) -> bool:
    return (a["name"] or "").endswith("(Parent Account)") or (
        not a.get("billing_street") and any(x.get("parent_id") == a["account_id"] for x in ctx.accounts))


def live_candidates(ctx: Ctx) -> list[dict]:
    """Accounts that can represent a facility today."""
    return [a for a in ctx.accounts
            if not is_parent_account(a, ctx)
            and not a.get("duplicate_of_account")
            and not a.get("chow_current_account")]


def address_match(loc: dict, a: dict) -> bool:
    if not street_number(loc["street"]) or street_number(loc["street"]) != street_number(a.get("billing_street")):
        return False
    same_place = zip5(loc["zip"]) == zip5(a.get("billing_zip")) or (
        loc["city"].lower() == (a.get("billing_city") or "").lower() and loc["state"] == a.get("billing_state"))
    if not same_place:
        return False
    k1, k2 = street_key(loc["street"]), street_key(a.get("billing_street"))
    return k1 == k2 or SequenceMatcher(None, k1, k2).ratio() >= 0.9


def name_only_match(loc: dict, a: dict) -> bool:
    crm_street = a.get("billing_street") or ""
    unusable = not crm_street.strip() or is_po_box(crm_street)
    return (unusable
            and loc["city"].lower() == (a.get("billing_city") or "").lower()
            and loc["state"] == a.get("billing_state")
            and name_similarity(loc["name"], a["name"]) >= NAME_OK)


def parent_rank(a: dict, ctx: Ctx) -> int:
    p = a.get("parent_id") or ""
    if p == ctx.target["account_id"]:
        return 3
    if p in ctx.acquired_ids:
        return 2
    return 1 if not p else 0


def survivor_sort_key(a: dict, loc: dict, ctx: Ctx):
    return (
        float(a.get("lifetime_revenue") or 0) > 0,            # billing history wins
        parent_rank(a, ctx),                                  # already under Bellhaven
        a.get("status") == "Active",
        ctx.contacts_per_account.get(a["account_id"], 0),     # more relationships
        (a.get("billing_street") or "").strip().lower() == loc["street"].strip().lower(),
        name_similarity(loc["name"], a["name"]),
        # deterministic final tie-break: lowest id wins (negated via sort below)
    )


def choose_survivor(cands: list[dict], loc: dict, ctx: Ctx) -> tuple[dict, list[dict]]:
    ranked = sorted(cands, key=lambda a: a["account_id"])
    ranked = sorted(ranked, key=lambda a: survivor_sort_key(a, loc, ctx), reverse=True)
    return ranked[0], ranked[1:]


def _new_account_fields(loc: dict, ctx: Ctx, note: str) -> dict:
    care = crm_care_types(loc["care_offerings"])
    return {
        "name": loc["name"], "parent_id": ctx.target["account_id"],
        "billing_street": loc["street"], "billing_city": loc["city"],
        "billing_state": loc["state"], "billing_zip": loc["zip"],
        "care_type": care[0] if care else "", "phone": loc.get("phone", ""),
        "status": "Active", "note": note,
    }


def build_proposals(locations: list[dict], accounts: list[dict], contacts: list[dict]):
    """Return (proposals, report)."""
    ctx = build_ctx(accounts, contacts)
    tid, tname = ctx.target["account_id"], ctx.target["name"]
    cands = live_candidates(ctx)
    proposals: list[dict] = []
    report = {"locations": [], "unlisted_accounts": []}
    claimed: set[str] = set()

    for loc in sorted(locations, key=lambda l: l["name"]):
        web_ev = {k: loc[k] for k in ("name", "street", "city", "state", "zip", "care_offerings", "phone", "administrator", "url", "found_via")}
        matched = [a for a in cands if address_match(loc, a)]
        how = "address"
        if not matched:
            matched = [a for a in cands if name_only_match(loc, a)]
            how = "name+city (CRM address unusable)" if matched else None
        near_misses = [
            {"account_id": a["account_id"], "name": a["name"], "address": f"{a.get('billing_street')}, {a.get('billing_city')}, {a.get('billing_state')} {a.get('billing_zip')}",
             "parent": a.get("parent_name"), "name_similarity": name_similarity(loc["name"], a["name"])}
            for a in cands if a not in matched and (
                name_similarity(loc["name"], a["name"]) >= 0.6 and (a.get("billing_city") or "").lower() == loc["city"].lower()
                or name_similarity(loc["name"], a["name"]) >= 0.95)
        ]
        row = {"slug": loc["slug"], "name": loc["name"], "city": f"{loc['city']}, {loc['state']}", "match_basis": how,
               "matched": [a["account_id"] for a in matched], "issues": []}

        # ---------------------------------------------------------------- NEW
        if not matched:
            fields = _new_account_fields(loc, ctx, f"Created from Bellhaven website listing {loc['url']}")
            proposals.append({
                "key": _key("create", loc["slug"], street_key(loc["street"]), zip5(loc["zip"])),
                "kind": "create", "location_slug": loc["slug"], "confidence": "high" if not near_misses else "review",
                "title": f"Create account: {loc['name']} ({loc['city']}, {loc['state']})",
                "payload": {"ops": [{"op": "create", "ref": "new", "fields": fields,
                                     "guard": {"street": loc["street"], "zip": loc["zip"]}}]},
                "evidence": {"website": web_ev, "crm": [], "near_misses": near_misses,
                             "why": "No live CRM account at this street address"
                                    + ("; similarly named accounts exist but at a different address, so they are NOT treated as this facility." if near_misses else ".")},
            })
            row.update(classification="new", issues=["no CRM account"])
            report["locations"].append(row)
            continue

        survivor, losers = choose_survivor(matched, loc, ctx)
        claimed.update(a["account_id"] for a in matched)
        row["account_id"] = survivor["account_id"]
        conf = "high" if how == "address" else "medium"
        base_ev = {"website": web_ev, "match_basis": how,
                   "crm": [snap(a) | {"contacts": ctx.contacts_per_account.get(a["account_id"], 0)} for a in matched],
                   "survivor": survivor["account_id"]}

        # --------------------------------------------------------- DUPLICATES
        for loser in losers:
            fields = {"duplicate_of_account": survivor["account_id"], "status": "Inactive"}
            note = f"Duplicate of {survivor['account_id']} ({survivor['name']}): same facility at {loc['street']}, {loc['city']} {loc['state']}."
            proposals.append({
                "key": _key("duplicate", loser["account_id"], survivor["account_id"]),
                "kind": "duplicate", "account_id": loser["account_id"], "location_slug": loc["slug"], "confidence": conf,
                "title": f"Mark duplicate: {loser['name']} ({loser['account_id']}) → {survivor['name']} ({survivor['account_id']})",
                "payload": {"ops": [
                    {"op": "update", "account_id": loser["account_id"], "fields": fields, "note_append": note,
                     "expect": {"duplicate_of_account": loser.get("duplicate_of_account") or "", "status": loser.get("status")}},
                    {"op": "move_contacts", "from": loser["account_id"], "to": survivor["account_id"]},
                ]},
                "evidence": base_ev | {"why": "Several live CRM accounts share this facility's street address. Survivor chosen by: "
                                               "billing history > already under Bellhaven > acquired-family parent > Active > # contacts > exact address > name similarity.",
                                       "loser": snap(loser), "survivor_record": snap(survivor)},
            })
            row["issues"].append(f"duplicate {loser['account_id']}")

        # ------------------------------------------------------ PARENT / CHOW
        chow = False
        if survivor.get("parent_id") != tid:
            lock = has_billing_lock(survivor)
            sop = {"lifetime_revenue": survivor.get("lifetime_revenue"), "outstanding_ar": survivor.get("outstanding_ar"),
                   "rule": "revenue history AND outstanding AR > 0 → keep old account, create new + chow_current_account",
                   "result": "CHOW (billing lock)" if lock else "direct re-parent allowed"}
            if lock:
                chow = True
                fields = _new_account_fields(
                    loc, ctx, f"Created by change of ownership from {survivor['account_id']} "
                              f"({survivor.get('parent_name') or 'no parent'} → {tname}). Old account kept for billing.")
                proposals.append({
                    "key": _key("chow", survivor["account_id"], tid),
                    "kind": "chow", "account_id": survivor["account_id"], "location_slug": loc["slug"], "confidence": conf,
                    "title": f"CHOW: new {loc['name']} under Bellhaven; link old account",
                    "payload": {"ops": [
                        {"op": "create", "ref": "new", "fields": fields, "guard": {"street": loc["street"], "zip": loc["zip"]}},
                        {"op": "update", "account_id": survivor["account_id"], "fields": {"chow_current_account": "$new"},
                         "expect": {"chow_current_account": "", "parent_id": survivor.get("parent_id") or ""}},
                    ]},
                    "evidence": base_ev | {"sop": sop, "why": f"Listed on the Bellhaven site but parented to "
                                                             f"{survivor.get('parent_name') or 'nothing'}. Account has revenue AND open AR, "
                                                             "so per SOP the old account is left untouched and a new account is created."},
                })
                row["issues"].append("CHOW")
            else:
                proposals.append({
                    "key": _key("reparent", survivor["account_id"], tid),
                    "kind": "reparent", "account_id": survivor["account_id"], "location_slug": loc["slug"], "confidence": conf,
                    "title": f"Re-parent {survivor['name']}: {survivor.get('parent_name') or '(none)'} → {tname}",
                    "payload": {"ops": [{"op": "update", "account_id": survivor["account_id"], "fields": {"parent_id": tid},
                                         "note_append": f"Re-parented from {survivor.get('parent_name') or 'no parent'} to {tname} (listed on Bellhaven website).",
                                         "expect": {"parent_id": survivor.get("parent_id") or ""}, "sop_check": True}]},
                    "evidence": base_ev | {"sop": sop, "why": "Listed on the Bellhaven website but not linked to the Bellhaven parent."},
                })
                row["issues"].append("wrong parent")

        # ------------------------------------------------------ FIELD UPDATES
        if not chow:
            changes, expect, reasons = {}, {}, []
            if survivor["name"].strip() != loc["name"].strip():
                changes["name"] = loc["name"]
                sim = name_similarity(loc["name"], survivor["name"])
                reasons.append(("rebrand / outdated name" if sim < NAME_OK else "name formatting differs") + f" (similarity {sim})")
            if street_key(survivor.get("billing_street")) != street_key(loc["street"]):
                changes["billing_street"] = loc["street"]
                reasons.append("street differs")
            if zip5(survivor.get("billing_zip")) != zip5(loc["zip"]):
                changes["billing_zip"] = loc["zip"]
                reasons.append("zip differs")
            if (survivor.get("billing_city") or "").lower() != loc["city"].lower():
                changes["billing_city"] = loc["city"]
                reasons.append("city differs")
            if survivor.get("billing_state") != loc["state"]:
                changes["billing_state"] = loc["state"]
                reasons.append("state differs")
            care = crm_care_types(loc["care_offerings"])
            if care and survivor.get("care_type") not in care:
                changes["care_type"] = care[0]
                reasons.append("care type differs")
            if survivor.get("status") != "Active":
                changes["status"] = "Active"
                reasons.append(f"status is {survivor.get('status')} but facility is listed")
            if changes:
                for f in changes:
                    expect[f] = survivor.get(f) or ""
                proposals.append({
                    "key": _key("update", survivor["account_id"], changes),
                    "kind": "update", "account_id": survivor["account_id"], "location_slug": loc["slug"], "confidence": conf,
                    "title": f"Update {survivor['name']}: " + ", ".join(sorted(changes)),
                    "payload": {"ops": [{"op": "update", "account_id": survivor["account_id"], "fields": changes, "expect": expect}]},
                    "evidence": base_ev | {"why": "; ".join(reasons),
                                           "diff": {f: [survivor.get(f), v] for f, v in changes.items()}},
                })
                row["issues"].extend(reasons)

        row["classification"] = "confident" if not row["issues"] else ("chow" if chow else "needs_fix")
        report["locations"].append(row)

    # ------------------------------------------------ UNLISTED BELLHAVEN ACCOUNTS
    for a in sorted(cands, key=lambda a: a["name"]):
        if a.get("parent_id") != tid or a["account_id"] in claimed:
            continue
        # someone else now operating at the same address?
        successors = [o for o in cands if o is not a and o.get("parent_id") != tid
                      and o["account_id"] not in claimed and address_match(
                          {"street": a.get("billing_street") or "", "zip": a.get("billing_zip") or "",
                           "city": a.get("billing_city") or "", "state": a.get("billing_state") or ""}, o)]
        ev = {"crm": [snap(a)], "website": None,
              "checked": f"{len(locations)} website locations; no address or name match"}
        entry = {"account_id": a["account_id"], "name": a["name"], "city": f"{a.get('billing_city')}, {a.get('billing_state')}"}
        if successors:
            succ = successors[0]
            ev["successor"] = snap(succ)
            if has_billing_lock(a):
                proposals.append({
                    "key": _key("chow_link", a["account_id"], succ["account_id"]),
                    "kind": "chow_link", "account_id": a["account_id"], "confidence": "high",
                    "title": f"CHOW: {a['name']} → existing {succ['name']} ({succ.get('parent_name')})",
                    "payload": {"ops": [{"op": "update", "account_id": a["account_id"],
                                         "fields": {"chow_current_account": succ["account_id"]},
                                         "expect": {"chow_current_account": ""}}]},
                    "evidence": ev | {"sop": {"lifetime_revenue": a.get("lifetime_revenue"), "outstanding_ar": a.get("outstanding_ar"),
                                              "result": "CHOW (billing lock) — old account left as is"},
                                      "why": f"No longer on the Bellhaven site, and the same address now has a live account under "
                                             f"{succ.get('parent_name')}. Facility was sold. Account has revenue and open AR, so we don't "
                                             "re-parent; we point chow_current_account at the new owner's existing account."},
                })
                entry["classification"] = "sold (CHOW link)"
            else:
                proposals.append({
                    "key": _key("duplicate", a["account_id"], succ["account_id"]),
                    "kind": "duplicate", "account_id": a["account_id"], "confidence": "medium",
                    "title": f"Mark duplicate: {a['name']} → {succ['name']} (sold)",
                    "payload": {"ops": [{"op": "update", "account_id": a["account_id"],
                                         "fields": {"duplicate_of_account": succ["account_id"], "status": "Inactive"},
                                         "note_append": f"Facility sold; now {succ['name']} under {succ.get('parent_name')}.",
                                         "expect": {"duplicate_of_account": ""}}]},
                    "evidence": ev | {"why": "Not on the Bellhaven site; the new owner already has an account at this address and this one has no billing lock."},
                })
                entry["classification"] = "sold (duplicate)"
        else:
            if a.get("status") == "Needs Review":
                entry["classification"] = "unlisted (already flagged)"
                report["unlisted_accounts"].append(entry)
                continue
            proposals.append({
                "key": _key("unlisted", a["account_id"], tid),
                "kind": "unlisted", "account_id": a["account_id"], "confidence": "review",
                "title": f"Flag for review: {a['name']} is no longer on the Bellhaven website",
                "payload": {"ops": [{"op": "update", "account_id": a["account_id"], "fields": {"status": "Needs Review"},
                                     "note_append": "Not listed on the Bellhaven website; ownership unverified (closed, sold, or delisted). "
                                                    "Parent left unchanged pending sales/ops confirmation.",
                                     "expect": {"status": a.get("status")}}]},
                "evidence": ev | {"why": "Under the Bellhaven parent but not on the website, and no other operator has an account at its "
                                         "address. Absence from a website isn't proof of a sale, so we flag it rather than guess a new parent."},
            })
            entry["classification"] = "unlisted (flag)"
        report["unlisted_accounts"].append(entry)

    return proposals, report
