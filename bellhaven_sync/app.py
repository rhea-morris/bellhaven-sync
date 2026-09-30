"""Review app. The ONLY code path that writes to the CRM is an explicit approval here.

Run:  python -m bellhaven_sync.app   (then open http://127.0.0.1:5000)
"""
from __future__ import annotations

import json
import os
from collections import defaultdict

from flask import Flask, abort, redirect, render_template_string, request, url_for

from . import config
from .apply import ApplyError, apply_proposal
from .crm import CRMClient
from .store import Store

KIND_ORDER = ["chow", "chow_link", "reparent", "duplicate", "create", "update", "unlisted"]
KIND_LABEL = {
    "chow": "Change of ownership (new account + CHOW link)",
    "chow_link": "Sold facility (CHOW link to new owner's account)",
    "reparent": "Re-parent to Bellhaven",
    "duplicate": "Duplicate accounts",
    "create": "New locations (no CRM account)",
    "update": "Field fixes (name / address / care type)",
    "unlisted": "Under Bellhaven but not on website",
}

BASE = """<!doctype html><html><head><meta charset="utf-8"><title>Bellhaven Sync · Review</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mut:#667085;--line:#e4e7ec;--ok:#12805c;--bad:#b42318;--warn:#b54708;--acc:#2f5bd3}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{background:#1d2330;color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:center;flex-wrap:wrap}
header a{color:#cfd6e4;text-decoration:none}header a.on{color:#fff;font-weight:600}header b{font-size:16px;margin-right:12px}
main{max-width:1180px;margin:0 auto;padding:20px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
h2{margin:26px 0 6px;font-size:17px}h3{margin:0 0 6px;font-size:15px}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);color:var(--mut);margin-left:6px}
.pill.high{color:var(--ok);border-color:#a6e3cb}.pill.medium,.pill.review{color:var(--warn);border-color:#fcd9a8}
.pill.applied{color:var(--ok)}.pill.failed,.pill.rejected{color:var(--bad)}
table{border-collapse:collapse;width:100%;margin:6px 0}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top;font-size:13px}
th{color:var(--mut);font-weight:500}.why{color:#344054;margin:4px 0 8px}.mut{color:var(--mut)}
.old{color:var(--bad);text-decoration:line-through}.new{color:var(--ok);font-weight:600}
.sop{background:#fff8eb;border:1px solid #fcd9a8;padding:6px 10px;border-radius:8px;margin:6px 0}
form.inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
input[type=text]{padding:6px 8px;border:1px solid var(--line);border-radius:6px;min-width:260px}
button{padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:#fff;cursor:pointer;font-weight:600}
button.ap{background:var(--ok);color:#fff;border-color:var(--ok)}button.rj{color:var(--bad)}button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}
.stats{display:flex;gap:10px;flex-wrap:wrap}.stat{background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 14px}.stat b{font-size:20px;display:block}
details summary{cursor:pointer;color:var(--acc)}pre{white-space:pre-wrap;font-size:12px;background:#f2f4f7;padding:8px;border-radius:6px;overflow:auto}
.flash{padding:10px 14px;border-radius:8px;margin:8px 0}.flash.ok{background:#ecfdf3;border:1px solid #a6e3cb}.flash.err{background:#fef3f2;border:1px solid #fecdca}
</style></head><body>
<header><b>Bellhaven Sync</b>
<a href="{{url_for('queue')}}" class="{{'on' if tab=='queue'}}">Review queue ({{counts.get('pending',0)}})</a>
<a href="{{url_for('locations')}}" class="{{'on' if tab=='loc'}}">Locations</a>
<a href="{{url_for('history')}}" class="{{'on' if tab=='hist'}}">History</a>
<span class="mut" style="margin-left:auto">reviewer: {{reviewer}}</span></header><main>
{% for kind,msg in flashes %}<div class="flash {{kind}}">{{msg}}</div>{% endfor %}
{{body|safe}}</main></body></html>"""

CARD = """
<div class="card" id="p{{p.id}}">
  <h3>{% if selectable %}<input type="checkbox" name="ids" value="{{p.id}}" form="bulk"> {% endif %}{{p.title}}
    <span class="pill {{p.confidence}}">{{p.confidence}}</span><span class="pill {{p.status}}">{{p.status}}</span><span class="pill">#{{p.id}}</span></h3>
  <div class="why">{{ev.why}}</div>
  {% if ev.sop %}<div class="sop"><b>CHOW SOP check</b> · lifetime_revenue = {{ev.sop.lifetime_revenue}} · outstanding_ar = {{ev.sop.outstanding_ar}} → <b>{{ev.sop.result}}</b></div>{% endif %}
  {% if ev.diff %}<table><tr><th>Field</th><th>CRM now</th><th>Proposed</th></tr>
    {% for f,v in ev.diff.items() %}<tr><td>{{f}}</td><td class="old">{{v[0]}}</td><td class="new">{{v[1]}}</td></tr>{% endfor %}</table>{% endif %}
  <table><tr><th>Operation</th><th>Target</th><th>Fields written</th></tr>
  {% for op in p.payload.ops %}<tr><td>{{op.op}}</td><td>{{op.account_id or op.get('from') or 'new account'}}</td>
    <td>{% if op.fields %}{% for k,v in op.fields.items() %}<div><b>{{k}}</b>: {{v}}</div>{% endfor %}{% endif %}
        {% if op.note_append %}<div><b>note +=</b> {{op.note_append}}</div>{% endif %}
        {% if op.op=='move_contacts' %}move active contacts → {{op.to}}{% endif %}</td></tr>{% endfor %}</table>
  <details><summary>Evidence: website vs CRM</summary>
   {% if ev.website %}<table><tr><th colspan=2>Website ({{ev.website.url}})</th></tr>
     <tr><td>Name</td><td>{{ev.website.name}}</td></tr><tr><td>Address</td><td>{{ev.website.street}}, {{ev.website.city}}, {{ev.website.state}} {{ev.website.zip}}</td></tr>
     <tr><td>Care</td><td>{{ev.website.care_offerings|join(', ')}}</td></tr><tr><td>Found via</td><td>{{ev.website.found_via|join(', ')}}</td></tr></table>{% endif %}
   {% if ev.match_basis %}<div class="mut">Matched on: {{ev.match_basis}}{% if ev.survivor %} · survivor {{ev.survivor}}{% endif %}</div>{% endif %}
   {% if ev.crm %}<table><tr><th>CRM id</th><th>Name</th><th>Parent</th><th>Address</th><th>Status</th><th>Rev / AR</th><th>Contacts</th></tr>
     {% for a in ev.crm %}<tr><td>{{a.account_id}}</td><td>{{a.name}}</td><td>{{a.parent_name or '—'}}</td><td>{{a.billing_street}}, {{a.billing_city}} {{a.billing_state}} {{a.billing_zip}}</td><td>{{a.status}}</td><td>{{a.lifetime_revenue}} / {{a.outstanding_ar}}</td><td>{{a.contacts if a.contacts is defined else ''}}</td></tr>{% endfor %}</table>{% endif %}
   {% if ev.successor %}<div>New operator at this address: <b>{{ev.successor.name}}</b> ({{ev.successor.account_id}}, {{ev.successor.parent_name}})</div>{% endif %}
   {% if ev.near_misses %}<div class="mut">Similar names NOT linked (different address):</div><table>{% for n in ev.near_misses %}<tr><td>{{n.account_id}}</td><td>{{n.name}}</td><td>{{n.address}}</td><td>{{n.parent}}</td><td>sim {{n.name_similarity}}</td></tr>{% endfor %}</table>{% endif %}
  </details>
  {% if p.result %}<details><summary>API result</summary><pre>{{p.result|tojson(indent=1)}}</pre></details>{% endif %}
  {% if p.decision_comment %}<div class="mut">Comment: {{p.decision_comment}}</div>{% endif %}
  {% if p.status in ('pending','failed') %}
  <form class="inline" method="post" action="{{url_for('decide', pid=p.id)}}">
    <input type="text" name="comment" placeholder="optional comment">
    <button class="ap" name="action" value="approve">{{'Retry' if p.status=='failed' else 'Approve & write'}}</button>
    <button class="rj" name="action" value="reject">Reject</button>
  </form>{% endif %}
</div>"""


def create_app(store: Store | None = None, crm_factory=None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("BH_SECRET", "dev")
    st = store or Store()
    get_crm = crm_factory or (lambda: CRMClient())
    reviewer = os.environ.get("BH_REVIEWER", os.environ.get("USER", os.environ.get("USERNAME", "reviewer")))
    flashes: list = []

    def page(tab, body):
        out = render_template_string(BASE, tab=tab, body=body, counts=st.counts(), reviewer=reviewer, flashes=list(flashes))
        flashes.clear()
        return out

    def card(p, selectable=False):
        return render_template_string(CARD, p=p, ev=p["evidence"], selectable=selectable)

    def do_approve(p, comment):
        prior = p["result"] if p["status"] == "failed" else None
        st.set_status(p["id"], "approved", by=reviewer, comment=comment)
        try:
            res = apply_proposal(get_crm(), p, prior)
            st.set_status(p["id"], "applied", result=res)
            return True, f"#{p['id']} applied: {p['title']}"
        except ApplyError as e:
            st.set_status(p["id"], "failed", result=e.partial)
            return False, f"#{p['id']} failed: {e}"

    @app.get("/")
    def queue():
        pend = st.list("pending") + st.list("failed")
        groups = defaultdict(list)
        for p in pend:
            groups[p["kind"]].append(p)
        run = st.last_run()
        html = ""
        if run:
            s = (run["report"] or {}).get("summary", {})
            html += "<div class='stats'>" + "".join(
                f"<div class='stat'><b>{v}</b>{k.replace('_', ' ')}</div>" for k, v in s.items() if not isinstance(v, dict)) + "</div>"
            html += f"<p class='mut'>Last pipeline run #{run['id']} finished {run['finished_at']} UTC · classification: {s.get('by_classification')}</p>"
        else:
            html += "<p>No pipeline run yet. Run <code>python -m bellhaven_sync.pipeline</code>.</p>"
        if not pend:
            html += "<div class='card'>Nothing waiting for review.</div>" if run else ""
        else:
            html += "<form id='bulk' method='post' action='" + url_for("bulk") + "' class='inline'>" \
                    "<b>Selected:</b><input type='text' name='comment' placeholder='optional comment'>" \
                    "<button class='ap' name='action' value='approve'>Approve selected</button>" \
                    "<button class='rj' name='action' value='reject'>Reject selected</button></form>"
        for k in KIND_ORDER + sorted(set(groups) - set(KIND_ORDER)):
            if groups.get(k):
                html += f"<h2>{KIND_LABEL.get(k, k)} <span class='pill'>{len(groups[k])}</span></h2>"
                html += "".join(card(p, selectable=True) for p in groups[k])
        return page("queue", html)

    @app.post("/proposal/<int:pid>")
    def decide(pid):
        p = st.get(pid) or abort(404)
        if p["status"] not in ("pending", "failed"):
            flashes.append(("err", f"#{pid} is already {p['status']}"))
            return redirect(url_for("queue"))
        comment = request.form.get("comment") or None
        if request.form.get("action") == "approve":
            ok, msg = do_approve(p, comment)
            flashes.append(("ok" if ok else "err", msg))
        else:
            st.set_status(pid, "rejected", by=reviewer, comment=comment)
            flashes.append(("ok", f"#{pid} rejected (won't be proposed again unless the evidence changes)"))
        return redirect(url_for("queue"))

    @app.post("/bulk")
    def bulk():
        ids = [int(i) for i in request.form.getlist("ids")]
        comment = request.form.get("comment") or None
        for pid in ids:
            p = st.get(pid)
            if not p or p["status"] not in ("pending", "failed"):
                continue
            if request.form.get("action") == "approve":
                ok, msg = do_approve(p, comment)
                flashes.append(("ok" if ok else "err", msg))
            else:
                st.set_status(pid, "rejected", by=reviewer, comment=comment)
                flashes.append(("ok", f"#{pid} rejected"))
        return redirect(url_for("queue"))

    @app.get("/history")
    def history():
        rows = [p for p in st.list() if p["status"] not in ("pending",)]
        rows.sort(key=lambda p: p.get("decided_at") or "", reverse=True)
        return page("hist", "<h2>Decisions</h2>" + ("".join(card(p) for p in rows) or "<p>None yet.</p>"))

    @app.get("/locations")
    def locations():
        run = st.last_run()
        if not run:
            return page("loc", "<p>No run yet.</p>")
        rep = run["report"]
        props = st.list()
        by_slug = defaultdict(list)
        for p in props:
            if p.get("location_slug"):
                by_slug[p["location_slug"]].append(p)
        html = f"<h2>Website locations ({len(rep['locations'])})</h2><table><tr><th>Location</th><th>City</th><th>Class</th><th>CRM account</th><th>Match basis</th><th>Proposals</th></tr>"
        for r in rep["locations"]:
            ps = "".join(f"<div><span class='pill {p['status']}'>{p['status']}</span> {p['title']}</div>" for p in by_slug.get(r["slug"], []))
            html += f"<tr><td>{r['name']}</td><td>{r['city']}</td><td>{r['classification']}</td><td>{r.get('account_id', '—')}</td><td>{r.get('match_basis') or '—'}</td><td>{ps or '<span class=mut>none</span>'}</td></tr>"
        html += "</table><h2>Bellhaven-parented accounts not on the website</h2><table><tr><th>Account</th><th>City</th><th>Class</th></tr>"
        for r in rep["unlisted_accounts"]:
            html += f"<tr><td>{r['name']} <span class=mut>{r['account_id']}</span></td><td>{r['city']}</td><td>{r['classification']}</td></tr>"
        return page("loc", html + "</table>")

    return app


def main():
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
