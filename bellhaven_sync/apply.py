"""Execute an approved proposal against the CRM.

Safety rails:
* Preconditions (`expect`) are re-checked against a fresh GET. If someone
  changed the account since the proposal was generated, we refuse and ask for
  a re-run instead of overwriting their edit.
* Re-parent ops re-check the CHOW SOP at write time (AR can change overnight).
* Creates first look for an existing live account at the same address under the
  target parent, so a retried/duplicated approval can't create two accounts.
* Notes are appended, never overwritten.
"""
from __future__ import annotations

from datetime import date

from . import config
from .crm import CRMClient
from .matcher import has_billing_lock
from .normalize import street_key, zip5


class PreconditionFailed(RuntimeError):
    pass


class ApplyError(RuntimeError):
    """Carries partial progress so a retry resumes instead of redoing ops."""
    def __init__(self, msg: str, partial: dict):
        super().__init__(msg)
        self.partial = partial


def _append_note(existing: str, text: str) -> str:
    line = f"{config.NOTE_TAG} {date.today().isoformat()}: {text}"
    return f"{existing.rstrip()}\n{line}" if existing and existing.strip() else line


def _find_existing(crm: CRMClient, fields: dict) -> dict | None:
    for a in crm.list_accounts(zip=zip5(fields["billing_zip"])):
        if (a.get("parent_id") == fields["parent_id"] and not a.get("duplicate_of_account")
                and street_key(a.get("billing_street")) == street_key(fields["billing_street"])):
            return a
    return None


def apply_proposal(crm: CRMClient, proposal: dict, prior_result: dict | None = None) -> dict:
    refs: dict[str, str] = dict((prior_result or {}).get("refs", {}))
    log: list = list((prior_result or {}).get("log", []))
    done_ops = set((prior_result or {}).get("done_ops", []))

    try:
        _run_ops(crm, proposal, refs, log, done_ops)
    except Exception as e:  # noqa: BLE001
        raise ApplyError(str(e), {"refs": refs, "log": log, "done_ops": sorted(done_ops), "error": str(e)}) from e
    return {"refs": refs, "log": log, "done_ops": sorted(done_ops)}


def _run_ops(crm: CRMClient, proposal: dict, refs: dict, log: list, done_ops: set):
    for i, op in enumerate(proposal["payload"]["ops"]):
        if i in done_ops:           # resume after partial failure
            continue
        kind = op["op"]
        if kind == "create":
            existing = _find_existing(crm, op["fields"])
            if existing:
                refs[op["ref"]] = existing["account_id"]
                log.append({"op": i, "reused_existing": existing["account_id"]})
            else:
                created = crm.create_account(op["fields"])
                refs[op["ref"]] = created["account_id"]
                log.append({"op": i, "created": created["account_id"]})

        elif kind == "update":
            acct = crm.get_account(op["account_id"])
            for f, want in (op.get("expect") or {}).items():
                have = acct.get(f) or ""
                if str(have) != str(want or ""):
                    raise PreconditionFailed(
                        f"{op['account_id']}.{f} is now {have!r} (expected {want!r}). The CRM changed since this "
                        "proposal was made; re-run the pipeline.")
            if op.get("sop_check") and has_billing_lock(acct):
                raise PreconditionFailed(
                    f"{op['account_id']} now has revenue AND outstanding AR "
                    f"({acct.get('lifetime_revenue')}/{acct.get('outstanding_ar')}); CHOW SOP forbids re-parenting. Re-run the pipeline.")
            fields = {k: (refs[v[1:]] if isinstance(v, str) and v.startswith("$") else v) for k, v in op["fields"].items()}
            if op.get("note_append"):
                fields["note"] = _append_note(acct.get("note") or "", op["note_append"])
            after = crm.update_account(op["account_id"], fields)
            log.append({"op": i, "updated": op["account_id"], "fields": fields,
                        "after": {k: after.get(k) for k in fields} if isinstance(after, dict) else None})

        elif kind == "move_contacts":
            moved = []
            for c in crm.list_contacts(account_id=op["from"]):
                crm.update_contact(c["contact_id"], {"account_id": op["to"]})
                moved.append(c["contact_id"])
            log.append({"op": i, "moved_contacts": moved, "to": op["to"]})
        else:
            raise ValueError(f"unknown op {kind}")
        done_ops.add(i)
