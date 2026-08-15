# TOWER

**An always-on GTM control tower for the FlytBase Book of Business.**

Not a dashboard with a database behind it. A system that watches a source of
truth it does not control, notices when that source changes, re-reasons only
over what moved, and can show you the exact sentence behind every claim it
makes.

---

## The one thing that shaped every design decision

The first useful fact about this dataset is not in any transcript. It is this:

```
ashford-construction     Healthy
ridgemont-polymers       —
swiftline-logistics      —
camborne-constabulary    Healthy
amber-ridge-processing   Healthy
meridian-energy          Healthy
vantage-protective       Healthy
walcross-materials       Healthy
northline-grid           Healthy
coastline-transit        Healthy
whitecliff-vineyard      Healthy
pinnacle-venue-group     Healthy
...
```

**Every account the CRM has an opinion about is labelled "Healthy."** Including
the ones that are visibly not. The health field carries zero information.

So the system is not allowed to surface it as an answer. It has to derive
health from behaviour, and then it has to do something more useful than that:
show you where the record and the reality disagree, and by how much. That is
the Reality Gap view, and it is the closest thing this repo has to a thesis.

---

## What it does

Seven questions, from the brief, answered across the whole portfolio:

| Question | Where it lives |
|---|---|
| What does each account actually look like, with evidence | `Accounts` → any row → every claim expands to its source quote |
| What should the team do next, ranked by what matters | `Triage` → expected dollars moved per unit of effort |
| Renewal and revenue picture, honestly | `Renewals` → weighted forecast against the naive CRM number |
| Real expansion versus traps | `Expansion & Traps` → each trap names its disqualifier |
| Are the churned accounts worth winning back | `Win-back` → scored, not guessed |
| Who is flying, and does it match the label | `Flight Activity` → the last column is literally "agrees?" |
| Does the system update itself when data changes | `Change Feed` → and the commit history of this repo |

---

## Architecture

```
   Book of Business (read-only MCP, 9 tools)
                  |
                  v
   +----------------------------------+
   |  SENTINEL   full inventory every  |   <-- the part that matters
   |  cycle, content-hash every object |
   |  ADDED / MODIFIED / WITHDRAWN     |
   +----------------------------------+
                  |
                  v
   +----------------------------------+
   |  EVIDENCE STORE                   |
   |  atomic, citable, tombstoned      |
   +----------------------------------+
                  |
        +---------+---------+
        v                   v
   +---------+       +--------------+
   | BRAIN   |       | METRICS      |
   | reads   |       | computes     |
   | prose   |       | every number |
   +---------+       +--------------+
        |                   |
        +---------+---------+
                  v
   +----------------------------------+
   |  ACCOUNT STATE  versioned, never  |
   |  overwritten, always diffable     |
   +----------------------------------+
                  |
                  v
   +----------------------------------+
   |  PORTFOLIO  ranking, forecast,    |
   |  traps, win-back, contradictions  |
   +----------------------------------+
                  |
                  v
        static JSON  ->  dashboard
```

### The three decisions worth defending

**1. The sentinel takes a complete inventory every cycle. It never asks
"what changed."**

The brief says: *"At least one document that's currently available stops being
available."*

Every incremental sync design fails that sentence. If you ask a source what is
new, it will never tell you about the thing that quietly disappeared. So every
60 seconds TOWER pulls the entire inventory, content-hashes every object, and
reconciles the full key set against what it holds. Anything held-but-not-seen
is **withdrawn**: tombstoned, dropped out of active reasoning, kept for audit.

That is what produces the sentence we actually want on the change feed:
*we no longer believe X, because the only evidence for X was withdrawn.*

See `tower/sentinel.py`.

**2. The model reads. Python computes. No exceptions.**

Every percentage, trend, health score, renewal probability and ranking in this
system is calculated in `tower/metrics.py`, in ordinary Python, from the raw
series. The language model is never asked for a number and is explicitly
instructed not to produce one.

This is not fussiness. It is the difference between a system whose numbers are
identical on every run and a demo that quietly reports a different decline
percentage each time you refresh it. It also means the scoring is arguable:
you can read `HealthModel.WEIGHTS` and disagree with it, which you cannot do
with a number a model made up.

**3. Every claim carries a quote, or it does not exist.**

The extraction schemas in `tower/brain.py` make `quote` a required field on
every person, signal, commitment and commercial fact. A finding without a
verbatim quote from the source document is not returned. In the dashboard,
those quotes are what you see when you expand a risk or an action.

---

## Things that are easy to get wrong, and how this handles them

**Dipped is not the same as declining.** Northline Grid drops from 55 to 24
flight hours over two months and then climbs to 64, above its previous peak.
A naive three-month comparison calls that account healthy. A naive
"look at the dip" calls it at risk. TOWER classifies it as **recovered** and
says the dip appears resolved. An alerting system that screams about an
account which already fixed itself loses the team's trust in the first week.

**The corpus dates things relatively.** Support tickets say "opened 4 months
ago", emails say "6 weeks ago". None of that is comparable until it is
anchored. `metrics.resolve_any_date` converts relative phrasing to real dates
against a reference date, and the dashboard shows which basis was used, so you
can see when a date is inferred rather than stated.

**Sources contradict each other.** They are supposed to. Rather than silently
picking a winner, TOWER records both claims and resolves them with a fixed
source-authority rule (`config.DOC_AUTHORITY`): what a customer said on a
dated call outranks an internal note, which outranks the CRM record. Both
sides stay visible in the Contradiction Ledger.

**An expansion signal is not a pipeline.** `portfolio._qualify` runs explicit
disqualifiers: no economic buyer, departed champion, open critical ticket,
collapsing usage, commercial friction, competitor present, base health at
risk. Anything that fails is filed as a **trap** with the reason and the
unblock condition stated, not quietly counted as upside.

**Billing problems must not stop the watching.** If the model API is down, out
of credit or rate limited, the reading layer degrades and the dashboard says
so at the top. Ingestion, change detection, usage analytics, health scoring
and ranking keep running. A payment failure is never allowed to stop the
sentinel noticing that a document vanished.

---

## The always-on layer

`.github/workflows/tower.yml` runs a long-lived job that polls every 60
seconds and commits whenever its understanding changes. `watchdog.yml` runs on
a cron, restarts the daemon if it died, and independently runs a one-shot sync
of its own so a single failure mode cannot lose a change.

The audit trail is the commit history of this repository. Each commit is
written by the runner, not by a human, and names exactly what moved:

```
sync 41 @ 2026-08-15 11:01:22Z: +3 added, -1 WITHDRAWN; 4 accounts re-reasoned

Withdrawn from source:
  document:coastline-transit/07_later_email_thread.md
```

Nobody can fake that after the fact, which is the point.

---

## Running it

```bash
pip install -r requirements.txt

export MCP_ENDPOINT="https://.../api/mcp"
export MCP_TOKEN="..."
export ANTHROPIC_API_KEY="sk-ant-..."     # or OPENAI_API_KEY

python -m tower.preflight     # verify connectivity, fail loudly if not
python -m tower.run once      # one cycle
python -m tower.run daemon    # poll forever
python -m tower.run export    # rebuild public/ from the database
```

The reading layer is provider agnostic. Anything speaking the OpenAI wire
format works by setting `OPENAI_BASE_URL`, including Google Gemini, Groq and
OpenRouter. No code changes.

## Layout

| File | What it is |
|---|---|
| `tower/mcp_client.py` | Minimal MCP Streamable-HTTP client. No framework. |
| `tower/source.py` | Adapter over the 9 Book of Business tools, including the SE dataset most people ignore |
| `tower/store.py` | Versioned truth store. Nothing is ever deleted, only tombstoned |
| `tower/sentinel.py` | Full-inventory reconciliation. The withdrawal detector |
| `tower/metrics.py` | All arithmetic. Usage trends, health, reality gap, forecasting |
| `tower/brain.py` | The reading layer. Schema-forced extraction, quotes required |
| `tower/pipeline.py` | One cycle: crawl, reconcile, re-read what moved, diff beliefs |
| `tower/portfolio.py` | Ranking, forecast, traps, win-back, contradictions |
| `tower/run.py` | Entry points and the git publishing that forms the audit trail |
| `web/index.html` | The dashboard. One file, no build step, cannot break in a demo |
