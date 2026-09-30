# bellhaven-sync

Keeps the CRM's picture of **who owns which Bellhaven facility** in line with Bellhaven's public website.
Every day it scrapes the website, matches each community to a CRM account, and queues proposed fixes.
A reviewer approves or rejects each one in a small web app. **Only an approval writes to the CRM.**

```
website ──scrape──┐
                  ├─ matcher ─→ proposals (SQLite, idempotent keys) ─→ review app ─approve─→ CRM API
CRM API ──read────┘                                                       └─reject─→ remembered, never re-proposed
```

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env             # put your BH_CRM_TOKEN in it
python -m bellhaven_sync.pipeline            # scrape + match + queue (read-only). --dry-run prints without saving
python -m bellhaven_sync.app                 # http://127.0.0.1:5000 : review, approve, reject
python -m pytest -q                          # end-to-end tests against a mock CRM seeded from the real sandbox
```

Scheduling: `deploy/crontab` (daily 06:15 CT) or `.github/workflows/daily-sync.yml` (same schedule; it carries the
SQLite state between runs on a `sync-state` branch). The scheduled job only generates proposals. It can't write.

## Layout

| file | role |
|---|---|
| `bellhaven_sync/scraper.py` | Crawls the paginated directory **and** any community links on the home and About pages |
| `bellhaven_sync/normalize.py` | Address normalisation (`Northwest Sylvania Avenue` = `NW Sylvania Ave`, `Pk` = `Pike`), name keys, care-type mapping |
| `bellhaven_sync/matcher.py` | Matching, classification, survivor selection, CHOW SOP, and proposal generation (pure function, no I/O) |
| `bellhaven_sync/store.py` | SQLite runs and proposals. Deterministic proposal keys make re-runs idempotent |
| `bellhaven_sync/apply.py` | Executes an approved proposal, with drift and SOP re-checks at write time |
| `bellhaven_sync/app.py` | Flask review UI: queue grouped by type, evidence, bulk approve, history, per-location view |
| `tests/` | Mock CRM and website built from the real sandbox snapshot, plus 7 end-to-end tests |

## Writeup

**Matching approach.** A facility is a building, so the street address is the primary key and the name only
confirms it. Names change with every rebrand; addresses almost never do. An account matches a website location when
the street number matches, the normalised street matches (fuzzy at 0.9 or above), and the zip or city/state matches.
A name-only match is allowed in one case: the CRM address is unusable (blank or a PO box) and the city/state agree.
Those matches are flagged as medium confidence. Address-first matching gets the rebrands right (*Sunny Acres Retirement Home* → *Bellhaven
Willow Creek*, *Riverbend Manor* → *Bellhaven of Chagrin Falls*). It also avoids the traps. *Amberly Manor* (Hudson, OH) is
not the *Amberly Manor* in Colorado Springs. *Bellhaven of Carlisle, PA* is not *New Carlisle, OH*. *Bellhaven at Union
Square* (118 Union Square Dr) is not Juniper Point's *Union Square Senior Living* (240 Market St). Each of these
gets a new account, and the UI shows the lookalike as a "near miss, not linked" so the reviewer can see the choice.

**Classification.** Every location ends up as one of four things:

* confident (no changes)
* needs fix (name, address, zip, care type, or wrong parent)
* CHOW
* new

Every Bellhaven-parented account missing from the website is checked for a successor. If another operator has a live
account at the same address, the facility was sold. For *Sandusky* that operator is Millstone. Because Sandusky has
revenue and open AR, we set `chow_current_account` to Millstone's account and leave everything else alone. If there is
no successor, the account goes to **Needs Review** with a note, and its parent is left alone. Missing from a website
is not proof of a sale, so we don't guess an owner.

**Duplicates.** When several live accounts sit at one address, one survivor is chosen by a fixed order:

1. Has billing history
2. Already under Bellhaven
3. Under an acquired-family parent (Harborview or Cedar Trail, from the About page)
4. Active
5. Most contacts
6. Exact address string match
7. Name similarity
8. Lowest id

Each loser gets `duplicate_of_account`, status Inactive, and a note. Its contacts are moved to the survivor, since
that is what a merge would have done.

**CHOW SOP.** This is checked twice: when the proposal is built, and again at write time, because AR can change
overnight. If the account has revenue AND AR > 0, the old account keeps its parent and fields. We create a new account
under Bellhaven and point the old one's `chow_current_account` at it (*Tiffin*, *Marietta*). Otherwise we re-parent
directly (*Lima*: $47k revenue but $0 AR; *Findlay*: no parent at all; *Kettering*; *Zanesville*).

**Safety and idempotency.** A proposal's key is a hash of action, target and intended values. Anything already
decided is never re-proposed. If the evidence changes, the key changes and a new proposal appears. Pending items that
a later run no longer produces are marked superseded. At write time the app re-reads the account and refuses if
someone edited the fields since the proposal was made. Creates check for an existing account at the same address
first. Notes are appended, never overwritten. A scrape that returns fewer than 5 locations aborts, so a broken
website can't look like mass closures. Tests cover all of this: the second run after approving everything proposes
nothing and makes zero writes.

**Things I noticed in the data.** The home page says 35 communities, but the directory lists 34. *Bellhaven Meadows
of Findlay* is linked only from the home page, which is why the scraper also follows links on the home and About pages.
Several acquired communities still exist twice: once under Harborview or Cedar Trail, and once under Bellhaven.

**How I used AI tools.** I used Claude to read the site and API, list the traps in the data, and draft the matcher, app
and tests. I checked its work in two ways. First, I did an independent manual walk-through of all 35 locations and 121
accounts, and the pipeline's output had to agree with it. Second, the end-to-end tests assert the specific outcomes
(Tiffin untouched except the CHOW pointer, the lookalikes not linked, and so on), not just "it runs."

**What I'd build next.**

* Move the state to Postgres shared by the job and the app.
* Add reviewer auth and an audit export.
* Run a phone and administrator comparison. Phone numbers differ on almost every account, which is too noisy to
  propose blindly.
* Add geocoding for the rare building with two addresses.
* Alert when the website structure changes.
* Suggest a successor owner for "unlisted" facilities using licensing or state-registry data.
