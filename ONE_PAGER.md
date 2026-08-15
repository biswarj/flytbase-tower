# TOWER — one-pager

**An always-on GTM control tower for the FlytBase Book of Business.**
Repo: `github.com/biswarj/flytbase-tower` · Dashboard: GitHub Pages · Runtime: GitHub Actions

---

## What I found before I built anything

Every account in the CRM that carries a health label carries the same one: **Healthy**. All of them. Including accounts whose usage has collapsed and whose champion has gone quiet.

That single fact decided the architecture. A system that surfaces the CRM's own fields has built nothing. The job is to derive health from behaviour, and then do the more useful thing: quantify how far the record has drifted from reality, and show the evidence for that gap.

## What I built

A pipeline that runs itself every 60 seconds:

**Sentinel → Evidence store → Reading layer + Metrics engine → Versioned beliefs → Portfolio intelligence → Static dashboard**

It answers all seven questions in the brief across the whole portfolio, and every answer expands to the verbatim source sentence behind it.

## Three decisions I would defend in a design review

**1. The sentinel takes a full inventory every cycle. It never asks "what changed."**

The brief says at least one document stops being available. Every incremental-sync design fails that sentence, because a source will never volunteer that something disappeared. So TOWER pulls the complete inventory each cycle, content-hashes every object, and reconciles the full key set. Held-but-not-seen means **withdrawn**: tombstoned, removed from active reasoning, retained for audit.

That is what lets the change feed say the thing that actually matters: *we no longer believe X, because the only evidence for X was withdrawn.*

**2. The model reads. Python computes. No exceptions.**

Every percentage, trend, health score, renewal probability and ranking is calculated in ordinary Python from raw series. The model is explicitly instructed never to produce a number. This is why the figures are identical on every run, and why the scoring is arguable rather than opaque: you can read the weights and disagree with them.

**3. Every claim carries a quote, or it does not exist.**

`quote` is a required field on every extracted person, signal, commitment and commercial fact. No quote, no finding. Those quotes are what you see when you expand any risk or action in the dashboard.

## Judgment calls the data rewards

- **Dipped is not declining.** Northline Grid falls 55 → 24 flight hours, then recovers to 64, above its prior peak. Naive trend maths gets this wrong in both directions. TOWER classifies it **recovered**. An alerting system that screams about an account which already fixed itself loses the team in week one.
- **Relative dates.** The corpus says "4 months ago", not a date. Nothing is comparable until it is anchored, so relative phrasing is resolved to real dates and the dashboard shows which basis was used.
- **Contradictions are recorded, not resolved silently.** A fixed source-authority rule decides which claim wins (a dated customer statement outranks an internal note, which outranks the CRM), and both sides stay visible.
- **Expansion signals are qualified, not counted.** Explicit disqualifiers turn a stated intention into a **trap** with the blocker and the unblock condition named.
- **The SE dataset.** Five tools most candidates will skip expose a second system of record: issues, feature requests, tasks, meeting notes. Feature requests cross-referenced against churn reasons is what makes the win-back question answerable instead of a guess.

## How the 4:30 requirement is met

A long-lived GitHub Actions job polls every 60 seconds and commits whenever its understanding changes. A separate cron watchdog restarts it if it dies and independently runs its own one-shot sync, so no single failure mode can lose a change.

The proof is the commit history of the repository: written by the runner, timestamped in UTC, naming exactly what moved. Nobody can fabricate that after the fact, which is the point.

## What I deliberately did not do

I did not build a prettier CRM. The dashboard is one static HTML file reading committed JSON, with no server to cold-start and nothing to fall over mid-demo. All the effort went into the layer underneath it: whether the system notices things, whether it can prove what it says, and whether it keeps working when something breaks.

On that last point: if the model API is down or out of credit, the reading layer degrades and the dashboard says so plainly at the top. Ingestion, change detection, usage analytics, health scoring and ranking keep running. A billing failure is never allowed to stop the sentinel noticing that a document vanished.
