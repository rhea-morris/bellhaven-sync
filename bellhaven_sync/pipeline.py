"""Daily pipeline: scrape -> fetch CRM -> match -> queue proposals. Never writes to the CRM."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter

from . import config
from .crm import CRMClient
from .matcher import build_proposals
from .scraper import Scraper
from .store import Store

log = logging.getLogger("bellhaven_sync")


def run(store: Store | None = None, crm: CRMClient | None = None, scraper: Scraper | None = None, dry_run: bool = False) -> dict:
    store = store or Store()
    crm = crm or CRMClient()
    scraper = scraper or Scraper()

    locations = [l.to_dict() for l in scraper.scrape()]
    if len(locations) < 5:
        # Guard: a broken scrape must never look like "every facility closed".
        raise RuntimeError(f"Scrape returned only {len(locations)} locations; aborting without proposing anything.")
    accounts = crm.list_accounts()
    contacts = crm.list_contacts()
    proposals, report = build_proposals(locations, accounts, contacts)

    if dry_run:
        for p in proposals:
            prev = store.existing(p["key"])
            print(f"[{'NEW' if not prev else prev['status'].upper():>10}] {p['kind']:<10} {p['title']}")
        return {"proposals": len(proposals)}

    run_id = store.start_run()
    new = skipped = 0
    for p in proposals:
        if store.add(run_id, p):
            new += 1
        else:
            skipped += 1
    superseded = store.supersede_missing({p["key"] for p in proposals})
    report["summary"] = {
        "website_locations": len(locations),
        "crm_accounts": len(accounts),
        "by_classification": dict(Counter(r["classification"] for r in report["locations"])),
        "unlisted": dict(Counter(r["classification"] for r in report["unlisted_accounts"])),
        "proposals_generated": len(proposals), "new": new, "already_decided_or_queued": skipped, "superseded": superseded,
    }
    store.finish_run(run_id, locations=len(locations), accounts=len(accounts), new_proposals=new,
                     skipped_decided=skipped, superseded=superseded, report=report)
    (config.DB_PATH.parent / "last_report.json").write_text(json.dumps(report, indent=2))
    (config.DB_PATH.parent / "last_locations.json").write_text(json.dumps(locations, indent=2))
    return report["summary"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bellhaven website -> CRM reconciliation (proposal generation only)")
    ap.add_argument("--dry-run", action="store_true", help="print proposals without saving them")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = run(dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
