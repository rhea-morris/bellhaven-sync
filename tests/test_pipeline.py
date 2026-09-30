"""End-to-end: scrape -> propose -> approve in the review app -> verify CRM -> re-run is a no-op."""
from bellhaven_sync import pipeline
from bellhaven_sync.app import create_app
from bellhaven_sync.store import Store

BH = "0015QAPLGS3FVYEEEM"


def _acc(state, aid):
    return state["accounts"][aid]


def _by_addr(state, street_prefix, parent=None):
    return [a for a in state["accounts"].values() if a["billing_street"].startswith(street_prefix)
            and (parent is None or a["parent_id"] == parent)]


def test_first_run_classification(env):
    summary = pipeline.run()
    assert summary["website_locations"] == 35          # includes Findlay (home-page only)
    c = summary["by_classification"]
    assert c["confident"] == 12 and c["new"] == 4 and c["chow"] == 2
    kinds = {}
    for p in Store().list("pending"):
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    assert kinds == {"chow": 2, "chow_link": 1, "create": 4, "duplicate": 7, "reparent": 4, "unlisted": 2, "update": 11}


def test_nothing_writes_without_approval(env):
    pipeline.run()
    assert env["state"]["writes"] == []


def test_approve_all_then_end_state_and_rerun_is_noop(env):
    pipeline.run()
    store = Store()
    client = create_app(store).test_client()
    ids = [str(p["id"]) for p in store.list("pending")]
    r = client.post("/bulk", data={"ids": ids, "action": "approve", "comment": "test"})
    assert r.status_code == 302
    assert store.counts() == {"applied": len(ids)}, [p["result"] for p in store.list("failed")]
    st = env["state"]

    # CHOW: old Tiffin untouched except the pointer; new account under Bellhaven
    tiffin = _acc(st, "001U6RW32TY0WSXZZB")
    assert tiffin["parent_id"] == "001FWSQ30SFW6S7604" and tiffin["name"] == "Bellhaven of Tiffin" and tiffin["note"] == ""
    new = _acc(st, tiffin["chow_current_account"])
    assert new["parent_id"] == BH and new["billing_street"] == "45 St Lawrence Dr"
    # Sold facility: Sandusky -> existing Millstone account, parent unchanged
    sand = _acc(st, "001SXSF4ELF0Z2LGDM")
    assert sand["chow_current_account"] == "0017JP8Z1UQ763BVK3" and sand["parent_id"] == BH
    # Direct re-parent (revenue but AR = 0)
    assert _acc(st, "001LGFPBJY4N9MB6KL")["parent_id"] == BH
    assert _acc(st, "001UKEFGADQ8YCZ4YM")["parent_id"] == BH
    # Duplicates
    for loser, surv in [("001JD2MWRA74LTSN24", "001UELXDAKFRKB8932"), ("001QU150PM4Z15UA71", "001EGU7BMJ942ZTRE6"),
                        ("0016KTS1UAWBRXS09J", "001WR41PYNWXCAE2X4"), ("001B7XZAA3AFALS9GP", "001WR41PYNWXCAE2X4")]:
        a = _acc(st, loser)
        assert a["duplicate_of_account"] == surv and a["status"] == "Inactive"
    assert st["contacts"]["003BT8QV7PS7S0XM0P"]["account_id"] == "001EGU7BMJ942ZTRE6"  # contact moved to survivor
    k = _acc(st, "001WR41PYNWXCAE2X4")
    assert k["name"] == "Bellhaven of Kettering" and k["parent_id"] == BH
    # Traps: different facilities with similar names are untouched; new accounts created instead
    assert _acc(st, "0015D74ZLRY810RGY5")["billing_city"] == "Colorado Springs"
    assert _acc(st, "001GNU41AVXZRLLJ9P")["parent_id"] == "001DAAUWV2J3SHQJ34"
    for street in ("4390 Darrow", "118 Union Square", "640 Walnut Bottom", "2000 Hospital"):
        assert len(_by_addr(st, street, BH)) == 1
    # Field fixes
    assert _acc(st, "001NXP9X46CWEPSLSV")["billing_street"] == "3156 W Prospect Rd"
    assert _acc(st, "001CF3LDWVRGL09P4F")["billing_zip"] == "45662"
    # Unlisted flagged, not re-parented
    assert _acc(st, "00116ETS45BL7DTQP7")["status"] == "Needs Review" and _acc(st, "00116ETS45BL7DTQP7")["parent_id"] == BH

    writes_before = len(st["writes"])
    summary = pipeline.run()
    assert summary["new"] == 0, [p["title"] for p in store.list("pending")]
    assert summary["by_classification"] == {"confident": 35}
    assert len(st["writes"]) == writes_before


def test_rejected_items_are_not_reproposed(env):
    pipeline.run()
    store = Store()
    client = create_app(store).test_client()
    p = next(p for p in store.list("pending") if p["kind"] == "unlisted")
    client.post(f"/proposal/{p['id']}", data={"action": "reject"})
    summary = pipeline.run()
    assert summary["new"] == 0
    assert store.get(p["id"])["status"] == "rejected"


def test_sop_rechecked_at_write_time(env):
    pipeline.run()
    store = Store()
    p = next(p for p in store.list("pending") if p["kind"] == "reparent" and p["account_id"] == "001LGFPBJY4N9MB6KL")
    env["state"]["accounts"]["001LGFPBJY4N9MB6KL"]["outstanding_ar"] = 500.0   # AR appears overnight
    create_app(store).test_client().post(f"/proposal/{p['id']}", data={"action": "approve"})
    assert store.get(p["id"])["status"] == "failed"
    assert env["state"]["accounts"]["001LGFPBJY4N9MB6KL"]["parent_id"] == "001FJZYHR7MLFMNPLL"


def test_drift_blocks_write(env):
    pipeline.run()
    store = Store()
    p = next(p for p in store.list("pending") if p["kind"] == "update" and p["account_id"] == "001CF3LDWVRGL09P4F")
    env["state"]["accounts"]["001CF3LDWVRGL09P4F"]["billing_zip"] = "45699"    # a rep edited it meanwhile
    create_app(store).test_client().post(f"/proposal/{p['id']}", data={"action": "approve"})
    assert store.get(p["id"])["status"] == "failed"
    assert env["state"]["accounts"]["001CF3LDWVRGL09P4F"]["billing_zip"] == "45699"


def test_broken_scrape_aborts(env):
    env["state"]["site"].clear()
    import pytest
    with pytest.raises(RuntimeError):
        pipeline.run()
