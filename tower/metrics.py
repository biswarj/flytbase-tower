"""
Deterministic engine. ALL arithmetic lives here.

Rule enforced across the whole system: the language model never computes a
number. It reads prose and returns labels and quotes. Every percentage,
score, trend, forecast and ranking in TOWER is computed in this file, in
Python, from the raw series. That is not fussiness. It is the difference
between a system whose numbers are stable across runs and a demo that
silently reports a different decline percentage every time you refresh it.
"""

from __future__ import annotations

import re
import math
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# Relative date resolution.
# The corpus dates things as "4 months ago" / "6 weeks ago" / "last quarter".
# Timelines are meaningless until those are anchored to real dates.
# --------------------------------------------------------------------------

_REL = re.compile(
    r"\b(?:(?P<n>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple\s+of|few)\s+)?"
    r"(?P<unit>day|days|week|weeks|month|months|quarter|quarters|year|years)\s+ago\b",
    re.I,
)
_WORD_N = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
           "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
           "couple of": 2, "few": 3}
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30.44, "quarter": 91.31, "year": 365.25}


def resolve_relative(text: str, ref: date) -> date | None:
    """'6 weeks ago' + anchor -> a real date."""
    m = _REL.search(text or "")
    if not m:
        return None
    raw = (m.group("n") or "1").lower().strip()
    n = _WORD_N.get(raw, None)
    if n is None:
        try:
            n = int(raw)
        except ValueError:
            n = 1
    unit = m.group("unit").lower().rstrip("s")
    return ref - timedelta(days=round(n * _UNIT_DAYS[unit]))


_ABS_PATTERNS = [
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b"), lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b"),
     lambda m: _safe_date(int(m[3]), int(m[2]), int(m[1]))),
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(20\d{2})\b", re.I),
     lambda m: _safe_date(int(m[3]), _MON[m[1][:3].title()], int(m[2]))),
    (re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})\b", re.I),
     lambda m: _safe_date(int(m[2]), _MON[m[1][:3].title()], 1)),
]
_MON = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _safe_date(y, m, d) -> date | None:
    try:
        return date(y, m, min(d, 28) if m == 2 else d)
    except ValueError:
        return None


def resolve_any_date(text: str, ref: date) -> tuple[date | None, str]:
    """Return (date, how). Absolute wins over relative."""
    for rx, fn in _ABS_PATTERNS:
        m = rx.search(text or "")
        if m:
            d = fn(m)
            if d:
                return d, "absolute"
    d = resolve_relative(text, ref)
    return (d, "relative-resolved") if d else (None, "none")


# --------------------------------------------------------------------------
# Usage analytics
# --------------------------------------------------------------------------

def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _slope(ys: list[float]) -> float:
    """OLS slope over evenly spaced points."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx, my = _mean(xs), _mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def _pct(new: float, old: float) -> float | None:
    if old == 0:
        return None if new == 0 else 999.0
    return (new - old) / old * 100.0


def usage_profile(series: list[dict], metric: str = "flightHours") -> dict:
    """Everything we can honestly say about a flight-activity series.

    Deliberately distinguishes 'declined' from 'dipped and recovered'. A
    system that cannot tell those apart will scream about an account that
    already fixed its own problem, and burn the team's trust in week one.
    """
    vals = [float(r.get(metric) or 0) for r in series]
    months = [r.get("month") for r in series]
    n = len(vals)
    out = {
        "metric": metric, "months": months, "values": vals, "n": n,
        "trajectory": "no-data", "headline": "No usage history available.",
        "latest": None, "peak": None, "peak_month": None,
        "pct_3v3": None, "pct_last_vs_peak": None, "pct_mom": None,
        "slope_per_month": 0.0, "slope_pct_of_mean": 0.0,
        "volatility_cv": None, "dormant_months": 0, "recovering": False,
        "total_recent_3": 0.0, "total_prior_3": 0.0,
    }
    if n == 0:
        return out

    out["latest"] = vals[-1]
    out["peak"] = max(vals)
    out["peak_month"] = months[vals.index(max(vals))] if months else None
    mean = _mean(vals)
    out["slope_per_month"] = round(_slope(vals), 3)
    out["slope_pct_of_mean"] = round(_slope(vals) / mean * 100, 2) if mean else 0.0
    if mean > 0 and n > 1:
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
        out["volatility_cv"] = round(sd / mean, 3)

    # trailing zeros = dormant
    dormant = 0
    for v in reversed(vals):
        if v <= 0:
            dormant += 1
        else:
            break
    out["dormant_months"] = dormant

    if n >= 2:
        out["pct_mom"] = _round(_pct(vals[-1], vals[-2]))
    if out["peak"]:
        out["pct_last_vs_peak"] = _round(_pct(vals[-1], out["peak"]))

    if n >= 6:
        recent, prior = vals[-3:], vals[-6:-3]
        out["total_recent_3"], out["total_prior_3"] = sum(recent), sum(prior)
        out["pct_3v3"] = _round(_pct(_mean(recent), _mean(prior)))
    elif n >= 4:
        half = n // 2
        recent, prior = vals[half:], vals[:half]
        out["total_recent_3"], out["total_prior_3"] = sum(recent), sum(prior)
        out["pct_3v3"] = _round(_pct(_mean(recent), _mean(prior)))

    p = out["pct_3v3"]
    latest, peak = out["latest"], out["peak"]

    if dormant >= 2:
        out["trajectory"] = "dormant"
    elif p is None:
        out["trajectory"] = "insufficient-history"
    elif p <= -40:
        out["trajectory"] = "collapsing"
    elif p <= -15:
        out["trajectory"] = "declining"
    elif p < 15:
        out["trajectory"] = "flat"
    elif p < 50:
        out["trajectory"] = "growing"
    else:
        out["trajectory"] = "scaling"

    # Dip-and-recover: there was a real trough, but the latest month is back
    # at or above the historical peak band. Not the same thing as decline.
    if n >= 5:
        trough = min(vals[:-1])
        if trough > 0 and peak and latest >= 0.9 * peak and trough <= 0.7 * peak:
            out["recovering"] = True
            if out["trajectory"] in ("flat", "growing", "scaling"):
                out["trajectory"] = "recovered"

    out["headline"] = _usage_headline(out)
    return out


def _usage_headline(u: dict) -> str:
    t, p = u["trajectory"], u["pct_3v3"]
    lm = u["months"][-1] if u["months"] else "?"
    unit = "flight hours" if u["metric"] == "flightHours" else "missions"
    if t == "no-data":
        return "No usage history available."
    if t == "dormant":
        return f"No {unit} logged for {u['dormant_months']} consecutive months to {lm}."
    if t == "insufficient-history":
        return f"Only {u['n']} months of history; trend not yet reliable."
    if t == "recovered":
        return (f"Dipped to a trough then recovered: latest month {u['latest']:.0f} "
                f"{unit} against a peak of {u['peak']:.0f}. Dip appears resolved.")
    d = "up" if (p or 0) >= 0 else "down"
    return (f"Last 3 months {d} {abs(p):.0f}% versus the prior 3 "
            f"({u['total_recent_3']:.0f} vs {u['total_prior_3']:.0f} {unit}); "
            f"latest month {u['latest']:.0f}, peak {u['peak']:.0f} in {u['peak_month']}.")


def _round(v):
    return None if v is None else round(v, 1)


# --------------------------------------------------------------------------
# Health. Decomposable by construction: a score nobody can take apart is a
# score nobody should trust.
# --------------------------------------------------------------------------

BANDS = [(85, "Strong"), (70, "Healthy"), (55, "Watch"), (35, "At Risk"), (0, "Critical")]
BAND_ORDER = ["Critical", "At Risk", "Watch", "Healthy", "Strong"]


def band(score: float) -> str:
    for cut, label in BANDS:
        if score >= cut:
            return label
    return "Critical"


class HealthModel:
    """Weighted, additive, and fully explainable.

    Each factor returns (0..1 quality, weight, one-line reason, evidence ids).
    We publish the contribution of every factor so a CSM can argue with the
    score instead of ignoring it.
    """

    WEIGHTS = {
        "usage_trajectory": 30,
        "support_burden": 20,
        "relationship": 20,
        "sentiment": 15,
        "commercial": 15,
    }

    @staticmethod
    def score_usage(u: dict) -> tuple[float, str]:
        t = u["trajectory"]
        table = {
            "scaling": (1.00, "Usage scaling."),
            "growing": (0.90, "Usage growing."),
            "recovered": (0.82, "Usage dipped and has recovered to near peak."),
            "flat": (0.62, "Usage flat."),
            "insufficient-history": (0.60, "Too little history to judge usage."),
            "no-data": (0.50, "No usage telemetry (not necessarily bad: pre-sale or non-flying account)."),
            "declining": (0.28, "Usage declining."),
            "collapsing": (0.08, "Usage collapsing."),
            "dormant": (0.02, "Account has stopped flying."),
        }
        q, why = table.get(t, (0.5, "Usage trend unclear."))
        if u.get("pct_3v3") is not None and t in ("declining", "collapsing", "growing", "scaling"):
            why = f"{why} {u['pct_3v3']:+.0f}% over the last 3 months versus the prior 3."
        return q, why

    @staticmethod
    def score_support(open_tickets: int, critical_open: int, sla_breaches: int,
                      recurring: int) -> tuple[float, str]:
        q = 1.0
        bits = []
        if critical_open:
            q -= 0.42 * min(critical_open, 2)
            bits.append(f"{critical_open} critical ticket(s) open")
        if open_tickets:
            q -= 0.10 * min(open_tickets, 4)
            bits.append(f"{open_tickets} open ticket(s)")
        if sla_breaches:
            q -= 0.15 * min(sla_breaches, 3)
            bits.append(f"{sla_breaches} SLA breach(es)")
        if recurring:
            q -= 0.18 * min(recurring, 2)
            bits.append(f"{recurring} recurring issue(s)")
        q = max(0.0, min(1.0, q))
        return q, ("Clean support record." if not bits else "Support load: " + ", ".join(bits) + ".")

    @staticmethod
    def score_relationship(has_champion: bool, champion_departed: bool,
                           contacts_known: int, days_silent: int | None,
                           economic_buyer_known: bool) -> tuple[float, str]:
        q, bits = 0.5, []
        if has_champion and not champion_departed:
            q += 0.25; bits.append("active champion identified")
        if champion_departed:
            q -= 0.35; bits.append("champion has left or gone quiet")
        if economic_buyer_known:
            q += 0.15; bits.append("economic buyer identified")
        else:
            q -= 0.10; bits.append("no economic buyer identified")
        if contacts_known >= 3:
            q += 0.12; bits.append(f"multithreaded across {contacts_known} contacts")
        elif contacts_known <= 1:
            q -= 0.15; bits.append("single-threaded")
        if days_silent is not None:
            if days_silent > 90:
                q -= 0.30; bits.append(f"no contact in {days_silent} days")
            elif days_silent > 45:
                q -= 0.15; bits.append(f"{days_silent} days since last contact")
        q = max(0.0, min(1.0, q))
        return q, ("Relationship: " + ", ".join(bits) + ".") if bits else "Relationship unclear."

    @staticmethod
    def score_sentiment(label: str, trend: str) -> tuple[float, str]:
        base = {"very-negative": 0.05, "negative": 0.25, "mixed": 0.5,
                "neutral": 0.6, "positive": 0.85, "very-positive": 1.0}.get(
                    (label or "neutral").lower(), 0.6)
        adj = {"improving": 0.12, "stable": 0.0, "deteriorating": -0.20}.get(
            (trend or "stable").lower(), 0.0)
        q = max(0.0, min(1.0, base + adj))
        return q, f"Customer sentiment reads {label or 'neutral'} and {trend or 'stable'}."

    @staticmethod
    def score_commercial(days_to_renewal: int | None, commercial_friction: bool,
                         competitor_present: bool, unresolved_commercial: int) -> tuple[float, str]:
        q, bits = 0.8, []
        if days_to_renewal is not None:
            if days_to_renewal < 0:
                q -= 0.4; bits.append("renewal date has passed without a signed outcome")
            elif days_to_renewal <= 45:
                q -= 0.20; bits.append(f"renewal in {days_to_renewal} days")
            elif days_to_renewal <= 90:
                q -= 0.10; bits.append(f"renewal in {days_to_renewal} days")
        if commercial_friction:
            q -= 0.25; bits.append("active pricing or commercial friction")
        if competitor_present:
            q -= 0.25; bits.append("competitor in the account")
        if unresolved_commercial:
            q -= 0.10 * min(unresolved_commercial, 3)
            bits.append(f"{unresolved_commercial} unresolved commercial item(s)")
        q = max(0.0, min(1.0, q))
        return q, ("Commercial: " + ", ".join(bits) + ".") if bits else "No commercial friction detected."

    @classmethod
    def compute(cls, factors: dict[str, tuple[float, str]]) -> dict:
        total_w = sum(cls.WEIGHTS[k] for k in factors if k in cls.WEIGHTS)
        if total_w == 0:
            return {"score": 50, "band": "Watch", "factors": []}
        score = 0.0
        rows = []
        for k, (q, why) in factors.items():
            w = cls.WEIGHTS.get(k, 0)
            contrib = q * w
            score += contrib
            rows.append({
                "factor": k, "quality": round(q, 3), "weight": w,
                "contribution": round(contrib, 1),
                "points_lost": round(w - contrib, 1), "reason": why,
            })
        score = score / total_w * 100
        rows.sort(key=lambda r: -r["points_lost"])
        return {"score": round(score, 1), "band": band(score), "factors": rows,
                "biggest_drag": rows[0]["factor"] if rows else None}


# --------------------------------------------------------------------------
# Reality gap: stated label versus derived truth
# --------------------------------------------------------------------------

def reality_gap(crm_label: str, derived_band: str) -> dict:
    """Every one of the 14 CRM records says 'Healthy'. That field is noise.
    This measures how far the record is from what the behaviour says, and in
    which direction, so the team can see which labels are actively lying."""
    norm = (crm_label or "").strip().lower()
    mapping = {"healthy": "Healthy", "strong": "Strong", "at risk": "At Risk",
               "at-risk": "At Risk", "watch": "Watch", "critical": "Critical",
               "churned": "Critical", "green": "Healthy", "amber": "Watch",
               "yellow": "Watch", "red": "At Risk"}
    stated = mapping.get(norm)
    if stated is None or derived_band not in BAND_ORDER:
        return {"gap": 0, "direction": "unknown", "stated": crm_label or "—",
                "derived": derived_band, "severity": "unknown",
                "note": "CRM health label is blank or unrecognised, so it cannot be checked."}
    g = BAND_ORDER.index(stated) - BAND_ORDER.index(derived_band)
    sev = {0: "aligned", 1: "minor", 2: "material", 3: "severe", 4: "severe"}[min(abs(g), 4)]
    if g > 0:
        direction, note = "overstated", (
            f"CRM says {stated}. Behaviour says {derived_band}. "
            f"The record is {abs(g)} band(s) more optimistic than the evidence supports.")
    elif g < 0:
        direction, note = "understated", (
            f"CRM says {stated}. Behaviour says {derived_band}. "
            f"The account is doing better than its record suggests.")
    else:
        direction, note = "aligned", f"CRM label and derived health agree at {stated}."
    return {"gap": g, "direction": direction, "stated": stated,
            "derived": derived_band, "severity": sev, "note": note}


# --------------------------------------------------------------------------
# Renewal + prioritisation
# --------------------------------------------------------------------------

RENEWAL_BUCKETS = ["Secure", "Likely", "At Risk", "High Risk", "Lost", "Not Applicable"]


def renewal_probability(health_score: float, days_to_renewal: int | None,
                        lifecycle: str, competitor: bool, commercial_friction: bool,
                        usage_traj: str) -> tuple[float, str]:
    """Explicitly NOT a black box. Starts from health, applies named
    adjustments, and every adjustment is reported."""
    if lifecycle == "churned":
        return 0.0, "Already churned."
    if lifecycle == "pre-sale":
        return -1.0, "Pre-sale: no renewal to forecast, this is new business."

    p = 0.25 + (health_score / 100.0) * 0.65      # 0.25 .. 0.90
    notes = [f"base {p:.0%} from health {health_score:.0f}"]

    if days_to_renewal is not None:
        if days_to_renewal < 0:
            p -= 0.15; notes.append("renewal date already passed (-15pp)")
        elif days_to_renewal <= 30 and health_score < 60:
            p -= 0.12; notes.append("under 30 days with weak health (-12pp)")
        elif days_to_renewal > 180:
            p += 0.05; notes.append("long runway to renewal (+5pp)")
    if competitor:
        p -= 0.18; notes.append("competitor active (-18pp)")
    if commercial_friction:
        p -= 0.10; notes.append("open commercial friction (-10pp)")
    if usage_traj in ("collapsing", "dormant"):
        p -= 0.20; notes.append("usage collapsed or dormant (-20pp)")
    elif usage_traj in ("scaling", "growing", "recovered"):
        p += 0.08; notes.append("usage healthy or recovering (+8pp)")

    p = max(0.02, min(0.97, p))
    return p, "; ".join(notes)


def renewal_bucket(p: float, lifecycle: str) -> str:
    if lifecycle == "churned":
        return "Lost"
    if p < 0:
        return "Not Applicable"
    if p >= 0.80:
        return "Secure"
    if p >= 0.60:
        return "Likely"
    if p >= 0.35:
        return "At Risk"
    return "High Risk"


def urgency_multiplier(days: int | None) -> float:
    """Time pressure. Something 20 days out beats the same problem 200 days out."""
    if days is None:
        return 1.0
    if days < 0:
        return 2.2
    if days <= 30:
        return 2.0
    if days <= 60:
        return 1.6
    if days <= 90:
        return 1.3
    if days <= 180:
        return 1.1
    return 1.0


def action_value(arr: float, prob_loss: float, impact: float, effort: float,
                 days_to_renewal: int | None, expansion_value: float = 0.0) -> float:
    """Expected dollars moved per unit of human effort.

    prob_loss  probability we lose the ARR if nobody acts (0..1)
    impact     fraction of that outcome this action can realistically change
    effort     1 (an email) .. 5 (an exec escalation and a rebuild plan)
    """
    protect = arr * prob_loss * impact
    grow = expansion_value * impact * 0.5
    return round((protect + grow) * urgency_multiplier(days_to_renewal) / max(effort, 1), 1)


def days_between(a: date, b: date) -> int:
    return (b - a).days


def parse_month(m: str) -> date | None:
    try:
        return datetime.strptime(m[:7], "%Y-%m").date()
    except Exception:
        return None
