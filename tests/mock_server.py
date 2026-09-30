"""In-memory replica of the CRM API + website, seeded from real snapshots (tests/fixtures)."""
from __future__ import annotations

import copy
import html
import random
import string
from pathlib import Path

from flask import Flask, abort, jsonify, request

FIX = Path(__file__).parent / "fixtures"
MUT_ACC = ['name', 'parent_id', 'status', 'note', 'care_type', 'phone', 'billing_street', 'billing_city',
           'billing_state', 'billing_zip', 'chow_current_account', 'duplicate_of_account']
MUT_CON = ['name', 'title', 'email', 'phone', 'is_active', 'account_id']
TOKEN = "test-token"

CONTACTS = [  # the contacts that exist on accounts touched by the duplicate logic (from the real sandbox)
    ("00360FXBH3M9XX0SMR", "001CVBBCSDM7YHN220", "Angela Quinn"),
    ("003VEEW4P875R8G3U4", "001EGU7BMJ942ZTRE6", "Gloria Lambert"),
    ("003BT8QV7PS7S0XM0P", "001QU150PM4Z15UA71", "Tricia Lindqvist"),
    ("003212YKWRDXPEJCJG", "001U1750VLVJAGG1S5", "Elaine Vogel"),
    ("003KAV28HT3YJF0KW7", "001UELXDAKFRKB8932", "Angela McAllister"),
    ("0036PSWLLS0UXGQEYR", "001UELXDAKFRKB8932", "Brenda Whitfield"),
]


def load_accounts():
    rows = [l.split("|") for l in (FIX / "accounts.psv").read_text().splitlines() if l.strip()]
    accts = []
    for r in rows:
        accts.append(dict(account_id=r[0], name=r[1], parent_id=r[2], billing_street=r[3], billing_city=r[4],
                          billing_state=r[5], billing_zip=r[6], care_type=r[7], status=r[8],
                          lifetime_revenue=float(r[9]), outstanding_ar=float(r[10]), chow_current_account=r[11],
                          duplicate_of_account=r[12], note=r[13], phone="", created_by_candidate=False))
    return accts


def load_site():
    locs = []
    for l in (FIX / "site.psv").read_text().splitlines():
        hidden = l.startswith("*")
        s = l.lstrip("*").split("|")
        locs.append(dict(slug=s[0], name=s[1], street=s[2], city=s[3], state=s[4], zip=s[5], care=s[6].split(";"), hidden=hidden))
    return locs


def create_app():
    app = Flask(__name__)
    st = {"accounts": {a["account_id"]: a for a in load_accounts()},
          "contacts": {c[0]: dict(contact_id=c[0], account_id=c[1], name=c[2], title="", email="", phone="", is_active=True)
                       for c in CONTACTS},
          "site": load_site(), "writes": []}
    app.config["STATE"] = st

    def auth():
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            abort(401)

    def with_parent(a):
        a = dict(a)
        p = st["accounts"].get(a["parent_id"])
        a["parent_name"] = p["name"] if p else ""
        return a

    def page(rows):
        ps = int(request.args.get("page_size", 50)); pg = int(request.args.get("page", 1))
        return jsonify(data=rows[(pg - 1) * ps: pg * ps], page=pg, page_size=ps, total=len(rows))

    @app.get("/api/v1/accounts")
    def list_accounts():
        auth()
        rows = sorted(st["accounts"].values(), key=lambda a: a["name"])
        for f, col in (("zip", "billing_zip"), ("city", "billing_city"), ("state", "billing_state"), ("parent_id", "parent_id")):
            if request.args.get(f):
                rows = [a for a in rows if a[col] == request.args[f]]
        if request.args.get("q"):
            rows = [a for a in rows if request.args["q"].lower() in a["name"].lower()]
        return page([with_parent(a) for a in rows])

    @app.get("/api/v1/accounts/<aid>")
    def get_account(aid):
        auth()
        return jsonify(with_parent(st["accounts"].get(aid) or abort(404)))

    @app.patch("/api/v1/accounts/<aid>")
    def patch_account(aid):
        auth()
        a = st["accounts"].get(aid) or abort(404)
        body = {k: v for k, v in (request.get_json() or {}).items() if k in MUT_ACC}
        if not body:
            return jsonify(detail="No mutable fields provided"), 422
        if "status" in body and body["status"] not in ("Active", "Inactive", "Needs Review"):
            return jsonify(detail="bad status"), 422
        a.update(body); st["writes"].append(("PATCH", aid, body))
        return jsonify(with_parent(a))

    @app.post("/api/v1/accounts")
    def create_account():
        auth()
        body = request.get_json() or {}
        if not body.get("name"):
            return jsonify(detail="name is required"), 422
        aid = "001" + "".join(random.choices(string.ascii_uppercase + string.digits, k=15))
        a = dict(account_id=aid, name="", parent_id="", billing_street="", billing_city="", billing_state="", billing_zip="",
                 care_type="", status="Active", lifetime_revenue=0, outstanding_ar=0, chow_current_account="",
                 duplicate_of_account="", note="", phone="", created_by_candidate=True)
        a.update({k: v for k, v in body.items() if k in MUT_ACC})
        st["accounts"][aid] = a; st["writes"].append(("POST", aid, body))
        return jsonify(with_parent(a)), 201

    @app.get("/api/v1/contacts")
    def list_contacts():
        auth()
        rows = list(st["contacts"].values())
        if request.args.get("account_id"):
            rows = [c for c in rows if c["account_id"] == request.args["account_id"]]
        return page(rows)

    @app.patch("/api/v1/contacts/<cid>")
    def patch_contact(cid):
        auth()
        c = st["contacts"].get(cid) or abort(404)
        body = {k: v for k, v in (request.get_json() or {}).items() if k in MUT_CON}
        c.update(body); st["writes"].append(("PATCH-C", cid, body))
        return jsonify(c)

    # ---------------- website ----------------
    def shell(body):
        return f"<!doctype html><html><body><header><a href='/'>Home</a><a href='/communities'>Our Communities</a></header><div class='wrap'>{body}</div></body></html>"

    @app.get("/")
    def home():
        hidden = [l for l in st["site"] if l["hidden"]]
        extra = "".join(f"<p>New: <a href='/communities/{l['slug']}'>{l['name']}</a></p>" for l in hidden)
        return shell(f"<h1>Bellhaven</h1><a href='/communities'>Directory</a>{extra}")

    @app.get("/about")
    def about():
        return shell("<h1>About</h1>")

    @app.get("/communities")
    def directory():
        listed = [l for l in st["site"] if not l["hidden"]]
        pg = int(request.args.get("page", 1)); per = 16
        chunk = listed[(pg - 1) * per: pg * per]
        cards = "".join(f"<div class='card'><h3><a href='/communities/{l['slug']}'>{html.escape(l['name'])}</a></h3></div>" for l in chunk)
        nxt = f"<a href='/communities?page={pg + 1}'>Next</a>" if pg * per < len(listed) else ""
        return shell(f"<h1>Our Communities</h1><div class='cardgrid'>{cards}</div><div class='pager'>{nxt}</div>")

    @app.get("/communities/<slug>")
    def detail(slug):
        l = next((x for x in st["site"] if x["slug"] == slug), None) or abort(404)
        badges = "".join(f"<span class='badge'>{html.escape(c)}</span>" for c in l["care"])
        return shell(f"<h1>{html.escape(l['name'])}</h1><dl class='detail'><dt>Address</dt><dd>{html.escape(l['street'])}<br>"
                     f"{l['city']}, {l['state']} {l['zip']}</dd><dt>Care Offerings</dt><dd>{badges}</dd>"
                     f"<dt>Administrator</dt><dd>Pat Doe</dd><dt>Phone</dt><dd>(555) 555-0100</dd></dl>")

    return app
