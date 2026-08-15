"""
The reading layer.

Division of labour that the whole design rests on:

  The model READS.   It returns labels, roles, quotes, claims.
  Python COMPUTES.   Every number, score, trend, rank and forecast.

So nothing here returns a percentage or a health score. It returns what a
sharp analyst would underline in the margin, always with the verbatim quote
attached, because a claim without a quote is an opinion and this system is
not allowed to have opinions.

Extraction is cached against the document's content hash. An unchanged
document is never re-read, which is what makes a 60 second poll loop cheap
enough to actually run all day.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from . import config

try:
    from anthropic import Anthropic
except Exception:  # library absent: system still runs, deterministically
    Anthropic = None  # type: ignore

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore


# --------------------------------------------------------------------------
# Schemas. Enforced as tools so the model cannot free-text its way out.
# --------------------------------------------------------------------------

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_date_text": {
            "type": "string",
            "description": "The date this document carries, copied verbatim, e.g. '6 weeks ago' or '2026-03-14'. Empty string if none.",
        },
        "summary": {
            "type": "string",
            "description": "Two sentences maximum. What actually happened, not what the document is about.",
        },
        "people": {
            "type": "array",
            "description": "Every named human in this document, on either side.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "title": {"type": "string", "description": "Job title if stated or clearly implied, else empty."},
                    "org_side": {"type": "string", "enum": ["customer", "flytbase", "third-party", "unknown"]},
                    "role": {
                        "type": "string",
                        "enum": ["economic-buyer", "champion", "technical-evaluator",
                                 "day-to-day-user", "blocker", "procurement",
                                 "executive-sponsor", "influencer", "unknown"],
                        "description": "Inferred from behaviour in the document, not from title alone. Someone who controls budget is economic-buyer even if their title says Director.",
                    },
                    "role_reason": {"type": "string", "description": "One clause on why that role."},
                    "sentiment": {"type": "string", "enum": ["very-negative", "negative", "mixed", "neutral", "positive", "very-positive", "unknown"]},
                    "status_signal": {
                        "type": "string",
                        "enum": ["active", "departed", "changed-role", "gone-quiet", "newly-introduced", "none"],
                        "description": "Use 'departed' only if the document says they left. Use 'gone-quiet' if unanswered contact is described.",
                    },
                    "quote": {"type": "string", "description": "Verbatim sentence from the document supporting this. Required."},
                },
                "required": ["name", "org_side", "role", "sentiment", "quote"],
            },
        },
        "signals": {
            "type": "array",
            "description": "Anything a good CSM would flag. Be specific and quote it.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["risk", "expansion-signal", "product-gap", "competitor",
                                 "commercial-friction", "positive-proof", "champion-risk",
                                 "adoption-blocker", "compliance-or-legal", "escalation"],
                    },
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "claim": {"type": "string", "description": "One sentence, concrete."},
                    "quote": {"type": "string", "description": "Verbatim supporting sentence. Required."},
                    "who": {"type": "string", "description": "Person it came from, if identifiable."},
                    "dollar_hint": {"type": "string", "description": "Any money or volume figure mentioned verbatim, else empty."},
                },
                "required": ["kind", "severity", "claim", "quote"],
            },
        },
        "commitments": {
            "type": "array",
            "description": "Promises made by either side. These are the open loops that quietly kill accounts.",
            "items": {
                "type": "object",
                "properties": {
                    "owner_side": {"type": "string", "enum": ["flytbase", "customer", "unknown"]},
                    "owner_name": {"type": "string"},
                    "promise": {"type": "string"},
                    "due_text": {"type": "string", "description": "Verbatim due date or timeframe if stated, else empty."},
                    "appears_closed": {"type": "boolean", "description": "True only if this document itself shows it was completed."},
                    "quote": {"type": "string"},
                },
                "required": ["owner_side", "promise", "appears_closed", "quote"],
            },
        },
        "commercial_facts": {
            "type": "array",
            "description": "Hard commercial values stated anywhere: renewal dates, contract values, seat/dock counts, term lengths, discounts, POC end dates.",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "e.g. renewal_date, contract_value, dock_count, term_months, poc_end_date, discount_pct, arr"},
                    "value": {"type": "string", "description": "Verbatim as written."},
                    "quote": {"type": "string"},
                },
                "required": ["field", "value", "quote"],
            },
        },
        "support_state": {
            "type": "object",
            "description": "Only meaningful for ticket documents. Zeros elsewhere.",
            "properties": {
                "open_tickets": {"type": "integer"},
                "critical_open": {"type": "integer"},
                "sla_breaches": {"type": "integer"},
                "recurring_issues": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["open_tickets", "critical_open", "sla_breaches", "recurring_issues"],
        },
        "overall_sentiment": {"type": "string", "enum": ["very-negative", "negative", "mixed", "neutral", "positive", "very-positive"]},
    },
    "required": ["summary", "people", "signals", "commitments", "commercial_facts",
                 "support_state", "overall_sentiment", "doc_date_text"],
}


DOC_SYSTEM = """You are the reading layer of a GTM control tower for FlytBase, an enterprise drone-autonomy platform (drone-in-a-box, docks, autonomous missions, AI detection agents, integrations into VMS and asset-management systems).

You are reading ONE raw source document from a customer account. Extract what a very good customer success lead plus solutions engineer would underline together.

Hard rules:
1. Every person, signal, commitment and commercial fact you return MUST carry a verbatim quote from this document. If you cannot quote it, do not return it.
2. Never infer beyond the text. If a title is not stated, leave it empty rather than guessing.
3. Roles come from behaviour, not job titles. The person who says "I'll need to take this to finance" is not the economic buyer. The person who says "I'll approve it" is.
4. Do not compute or estimate any percentage, score or trend. Numbers are handled elsewhere. Copy figures verbatim into dollar_hint or commercial_facts and stop there.
5. Read silence as data. Unanswered emails, someone who stopped attending calls, a champion whose name disappears: these are signals, quote the line that shows it.
6. Be sceptical of enthusiasm that is not backed by action, and of calm that is not backed by usage. Flag both.
7. Distinguish a problem that was raised and fixed from one that is still open. Only the still-open one is a risk."""


SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "one_line": {"type": "string", "description": "The single sentence a CSM should read first about this account. Blunt, specific, no hedging."},
        "situation": {"type": "string", "description": "3 to 5 sentences. What is actually going on, in plain language, referencing concrete facts."},
        "sentiment_label": {"type": "string", "enum": ["very-negative", "negative", "mixed", "neutral", "positive", "very-positive"]},
        "sentiment_trend": {"type": "string", "enum": ["improving", "stable", "deteriorating"]},
        "sentiment_reason": {"type": "string"},
        "champion_status": {"type": "string", "enum": ["strong", "present", "weak", "departed", "none-identified"]},
        "economic_buyer_identified": {"type": "boolean"},
        "competitor_present": {"type": "boolean"},
        "competitor_names": {"type": "array", "items": {"type": "string"}},
        "commercial_friction": {"type": "boolean"},
        "unresolved_commercial_items": {"type": "integer"},
        "top_risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "risk": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "why_it_matters": {"type": "string"},
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["risk", "severity", "why_it_matters", "evidence_quotes"],
            },
        },
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "opportunity": {"type": "string"},
                    "estimated_value_basis": {"type": "string", "description": "Quote the volume or money figure the estimate rests on, or say 'no figure stated'."},
                    "confidence": {"type": "string", "enum": ["speculative", "possible", "probable", "committed"]},
                    "is_trap": {"type": "boolean", "description": "True when this looks like an opportunity but qualification fails."},
                    "trap_reason": {"type": "string", "description": "If is_trap, exactly what disqualifies it right now. Else empty."},
                    "unblock_condition": {"type": "string", "description": "What must be true before pursuing this."},
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["opportunity", "confidence", "is_trap", "evidence_quotes"],
            },
        },
        "contradictions": {
            "type": "array",
            "description": "Places where two sources disagree. This is explicitly wanted. Look hard for them.",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "claim_a": {"type": "string"},
                    "source_a": {"type": "string", "description": "Document name or type."},
                    "claim_b": {"type": "string"},
                    "source_b": {"type": "string"},
                    "resolution": {"type": "string", "description": "Which one to believe and why, in one sentence."},
                    "matters_because": {"type": "string"},
                },
                "required": ["topic", "claim_a", "source_a", "claim_b", "source_b", "resolution"],
            },
        },
        "churn_analysis": {
            "type": "object",
            "description": "Populate ONLY for churned accounts. Otherwise leave fields empty/false.",
            "properties": {
                "root_cause": {"type": "string"},
                "cause_category": {"type": "string", "enum": ["product-gap", "reliability", "price", "champion-loss", "sponsor-or-budget-change", "programme-cancelled", "competitor-displacement", "service-failure", "not-applicable"]},
                "reversible": {"type": "boolean", "description": "Is the cause something FlytBase can now actually change?"},
                "what_changed_since": {"type": "string", "description": "Anything in the evidence suggesting the blocker no longer applies."},
                "residual_relationship": {"type": "string", "description": "Who would still take our call, and the quote showing it."},
                "winback_verdict": {"type": "string", "enum": ["pursue-now", "pursue-later", "do-not-pursue", "not-applicable"]},
                "winback_reasoning": {"type": "string"},
                "first_move": {"type": "string", "description": "The specific opening move, naming the person."},
            },
            "required": ["cause_category", "reversible", "winback_verdict"],
        },
        "next_actions": {
            "type": "array",
            "description": "2 to 4 actions. Each must name a person, a channel and a concrete ask. No 'schedule a check-in'.",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "owner_function": {"type": "string", "enum": ["CS", "SE", "CS+SE", "Sales", "Exec"]},
                    "target_person": {"type": "string"},
                    "why_now": {"type": "string"},
                    "effort": {"type": "integer", "description": "1 an email, 5 an exec escalation plus a rebuild plan."},
                    "impact": {"type": "number", "description": "0 to 1. Fraction of the at-stake outcome this action can realistically shift."},
                    "prob_loss_if_ignored": {"type": "number", "description": "0 to 1. Probability the ARR is lost if nobody does anything."},
                    "expansion_value_usd": {"type": "number", "description": "0 unless this action opens revenue, and only if a figure is quotable."},
                    "evidence_quotes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action", "owner_function", "why_now", "effort", "impact",
                             "prob_loss_if_ignored", "evidence_quotes"],
            },
        },
    },
    "required": ["one_line", "situation", "sentiment_label", "sentiment_trend",
                 "champion_status", "economic_buyer_identified", "competitor_present",
                 "commercial_friction", "top_risks", "opportunities", "contradictions",
                 "next_actions"],
}


SYNTH_SYSTEM = """You are the senior judgment layer of a GTM control tower for FlytBase (enterprise drone autonomy: docks, autonomous missions, AI detection, VMS and asset-management integrations).

You are given, for ONE account: the raw CRM record, the pre-computed usage analytics, and structured extractions from every source document with verbatim quotes. Produce the account's working truth.

Hard rules:
1. Do not compute numbers. Health scores, percentages, trends and rankings are already computed and given to you. Quote them, never recalculate them.
2. Every risk, opportunity and action must rest on quotes drawn from the supplied evidence. Do not invent a quote.
3. The CRM health field in this dataset is unreliable. Every account is labelled the same way. Never use it as support for a conclusion. If it disagrees with the evidence, that disagreement is itself a contradiction worth recording.
4. Hunt for contradictions between sources deliberately. A renewal table that disagrees with what the customer said on a call, an internal note that disagrees with a transcript, a health label that disagrees with usage. These are the highest-value findings in the dataset.
5. Traps matter as much as opportunities. An expansion signal is a trap when qualification fails: no economic buyer, unresolved critical support issues, a departed champion, a frozen budget, a stated intent nobody has funded, or growth talk from someone with no authority. Mark it, explain the disqualifier, and state the unblock condition.
6. Actions must be executable by a named human tomorrow morning. Name the person, the channel, and the specific ask. "Schedule a check-in" is a failure. "Get Dana Whitfield on a 30-minute call to walk through the two open dock-connectivity tickets before the 12 Sept renewal" is correct.
7. Where the evidence is thin, say so plainly rather than padding. Confident vagueness is the failure mode you must avoid.
8. Write in plain sentences. No em dashes."""


class Brain:
    """The reading layer, provider agnostic.

    Anthropic is preferred. OpenAI is a drop-in alternative. If neither key
    is present the whole class degrades to keyword heuristics and says so.
    Both providers are driven through forced tool calling against the same
    JSON schemas above, so the output shape is identical either way and
    nothing downstream needs to know which one ran.
    """

    def __init__(self, api_key: str | None = None):
        self.provider = "none"
        self.client = None
        self.api_key = api_key or config.ANTHROPIC_API_KEY

        # Explicit timeouts, and retries disabled at the SDK layer.
        #
        # Both SDKs default to a 600 second timeout with their own retries on
        # top. One hung request would therefore stall a cycle for up to half
        # an hour with no log line explaining why. A poll loop that promises
        # to react within a minute cannot own a thirty minute failure mode, so
        # we cap it hard and do our own backoff where it is visible.
        if self.api_key and Anthropic is not None:
            self.provider = "anthropic"
            self.client = Anthropic(api_key=self.api_key,
                                    timeout=config.MODEL_TIMEOUT_SECONDS,
                                    max_retries=0)
        elif config.OPENAI_API_KEY and OpenAI is not None:
            # "openai" here means the OpenAI wire format, not necessarily
            # OpenAI the company. Google, Groq and OpenRouter all speak it.
            self.provider = "openai"
            self.api_key = config.OPENAI_API_KEY
            self.client = OpenAI(api_key=self.api_key,
                                 base_url=config.OPENAI_BASE_URL,
                                 timeout=config.MODEL_TIMEOUT_SECONDS,
                                 max_retries=0)

        self.enabled = self.provider != "none"
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.failures = 0
        self.last_error = ""

    # ------------------------------------------------------------------
    def _tool_call(self, *, system: str, user: str, schema: dict,
                   tool_name: str, model: str, max_tokens: int = 8000,
                   retries: int = 4) -> dict | None:
        """Returns None on failure rather than raising.

        This matters more than it looks. If the model API is down, out of
        credit, or rate limited, the reading layer must degrade quietly while
        ingestion and change detection keep running. A billing problem must
        never be able to stop the sentinel from noticing that a document
        disappeared.
        """
        last = None
        for attempt in range(retries):
            try:
                if self.provider == "anthropic":
                    resp = self.client.messages.create(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        tools=[{"name": tool_name,
                                "description": "Return the structured result.",
                                "input_schema": schema}],
                        tool_choice={"type": "tool", "name": tool_name},
                        messages=[{"role": "user", "content": user}],
                    )
                    self.calls += 1
                    if getattr(resp, "usage", None):
                        self.input_tokens += resp.usage.input_tokens or 0
                        self.output_tokens += resp.usage.output_tokens or 0
                    for block in resp.content:
                        if getattr(block, "type", None) == "tool_use":
                            return block.input
                    last = RuntimeError("no tool_use block returned")
                else:  # openai
                    resp = self.client.chat.completions.create(
                        model=config.OPENAI_MODEL,
                        max_completion_tokens=max_tokens,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
                        tools=[{"type": "function",
                                "function": {"name": tool_name,
                                             "description": "Return the structured result.",
                                             "parameters": schema}}],
                        tool_choice={"type": "function",
                                     "function": {"name": tool_name}},
                    )
                    self.calls += 1
                    if getattr(resp, "usage", None):
                        self.input_tokens += resp.usage.prompt_tokens or 0
                        self.output_tokens += resp.usage.completion_tokens or 0
                    tc = (resp.choices[0].message.tool_calls or [None])[0]
                    if tc is not None:
                        return json.loads(tc.function.arguments)
                    last = RuntimeError("no tool call returned")
            except Exception as e:
                import time
                last = e
                msg = str(e).lower()
                # Do not burn retries on errors that will never succeed.
                # "Request too large" belongs in this list and not with the
                # rate limits it superficially resembles: the request is over
                # the per-minute ceiling on its own, so waiting changes nothing.
                if any(k in msg for k in ("credit balance", "invalid_api_key",
                                          "authentication", "permission",
                                          "not found", "does not exist",
                                          "413", "request too large",
                                          "reduce your message size",
                                          "context_length_exceeded")):
                    break
                # Rate limits are the normal case on a free tier, not a
                # failure. Back off properly instead of giving up after four
                # seconds and silently degrading a whole account.
                throttled = any(k in msg for k in ("429", "rate limit", "rate_limit",
                                                   "quota", "resource_exhausted",
                                                   "overloaded", "503"))
                budget = retries + 3 if throttled else retries
                if attempt >= budget - 1:
                    break
                time.sleep((8 * (attempt + 1)) if throttled else (2 * (attempt + 1)))
        self.failures += 1
        self.last_error = str(last)[:300]
        print(f"[brain] call failed, degrading this item: {self.last_error}", flush=True)
        return None

    # ------------------------------------------------------------------
    @staticmethod
    def _fit(want_output: int) -> tuple[int, int]:
        """Split the tokens-per-minute ceiling between prompt and answer.

        Returns (max_prompt_characters, max_output_tokens). The provider counts
        the reserved output against the same ceiling as the prompt, so both
        halves have to be chosen together. Roughly four characters per token,
        with 400 tokens held back for the system prompt and the tool schema.
        """
        # 90% of the stated ceiling. Token counting is an estimate on this side
        # and an exact number on theirs, and being 5% under costs nothing while
        # being 1% over costs the whole request.
        ceiling = int(max(2000, config.MODEL_TPM_BUDGET) * 0.90)
        out = max(400, min(want_output, int(ceiling * 0.35)))
        prompt_tokens = max(500, ceiling - out - 700)
        # 3.6 characters per token, deliberately below the usual 4, because
        # going over the ceiling is a hard rejection and going under it only
        # costs a little context.
        return int(prompt_tokens * 3.6), out

    def read_document(self, *, account_name: str, lifecycle: str, doc_type: str,
                      title: str, content: str) -> dict:
        if not self.enabled:
            return _fallback_doc(content, doc_type)
        room, out_tokens = self._fit(1600)
        head = (
            f"ACCOUNT: {account_name}\n"
            f"LIFECYCLE STAGE: {lifecycle}\n"
            f"DOCUMENT TYPE: {doc_type}\n"
            f"DOCUMENT TITLE: {title}\n"
        )
        body = content[:max(500, room - len(head) - 60)]
        user = f"{head}--- BEGIN DOCUMENT ---\n{body}\n--- END DOCUMENT ---"
        out = self._tool_call(system=DOC_SYSTEM, user=user, schema=DOC_SCHEMA,
                              tool_name="record_document_reading",
                              model=config.MODEL_EXTRACT, max_tokens=out_tokens)
        return out if out is not None else _fallback_doc(content, doc_type)

    def dossier_budget_chars(self) -> int:
        """How much dossier the synthesis call can afford to send."""
        return self._fit(2600)[0] if self.enabled else 0

    def synthesise_account(self, *, dossier: str) -> dict:
        if not self.enabled:
            return _fallback_synth()
        room, out_tokens = self._fit(2600)
        out = self._tool_call(system=SYNTH_SYSTEM, user=dossier[:room],
                              schema=SYNTH_SCHEMA,
                              tool_name="record_account_truth",
                              model=config.MODEL_SYNTH, max_tokens=out_tokens)
        return out if out is not None else _fallback_synth()

    def narrate_change(self, *, account: str, before: dict, after: dict,
                       changes: list[dict]) -> str:
        """One sentence explaining why a belief moved. Used on the change feed."""
        if not self.enabled:
            return _fallback_narrative(account, before, after, changes)
        ch = "\n".join(
            f"- {c['change_type']} {c.get('title') or c['source_id']}"
            f"{': ' + c['diff_summary'] if c.get('diff_summary') else ''}"
            for c in changes[:12]
        )
        user = (
            f"Account: {account}\n\nNew or withdrawn source material:\n{ch}\n\n"
            f"Our view BEFORE:\n{json.dumps(before, default=str)[:2500]}\n\n"
            f"Our view AFTER:\n{json.dumps(after, default=str)[:2500]}\n\n"
            "In at most two plain sentences, state what changed in our understanding and "
            "which specific piece of source material caused it. If a document was WITHDRAWN, "
            "say explicitly that the evidence was withdrawn and what we can no longer support. "
            "No em dashes. No preamble."
        )
        sysmsg = "You write terse, factual change notes for a GTM control tower."
        try:
            if self.provider == "anthropic":
                resp = self.client.messages.create(
                    model=config.MODEL_SYNTH, max_tokens=350, system=sysmsg,
                    messages=[{"role": "user", "content": user}])
                self.calls += 1
                return "".join(b.text for b in resp.content
                               if getattr(b, "type", None) == "text").strip()
            resp = self.client.chat.completions.create(
                model=config.OPENAI_MODEL, max_completion_tokens=350,
                messages=[{"role": "system", "content": sysmsg},
                          {"role": "user", "content": user}])
            self.calls += 1
            return (resp.choices[0].message.content or "").strip()
        except Exception:
            return _fallback_narrative(account, before, after, changes)


# --------------------------------------------------------------------------
# Degraded mode. If there is no API key the system still ingests, still
# detects change, still scores, still ranks. It just stops reading prose.
# Announcing that loudly beats pretending.
# --------------------------------------------------------------------------

_TICKET_OPEN = re.compile(r"^\s*\**\s*status\s*:?\s*\**\s*(open|in[- ]progress|escalated|pending)",
                          re.I | re.M)
_TICKET_CRIT = re.compile(r"(sev(erity)?\s*[-: ]?\s*(1|one|critical)|\bP1\b|\bcritical\b)", re.I)
_SLA = re.compile(r"sla[^.\n]{0,40}(breach|miss|exceed|violat|no\b)", re.I)


def _fallback_doc(content: str, doc_type: str) -> dict:
    open_t = len(_TICKET_OPEN.findall(content or ""))
    return {
        "doc_date_text": "",
        "summary": "[degraded mode: no model key, document not read]",
        "people": [], "signals": [], "commitments": [], "commercial_facts": [],
        "support_state": {
            "open_tickets": open_t if doc_type == "ticket" else 0,
            "critical_open": len(_TICKET_CRIT.findall(content or "")) if doc_type == "ticket" else 0,
            "sla_breaches": len(_SLA.findall(content or "")) if doc_type == "ticket" else 0,
            "recurring_issues": 0,
            "notes": "keyword fallback only",
        },
        "overall_sentiment": "neutral",
        "_degraded": True,
    }


def _fallback_synth() -> dict:
    return {
        "one_line": "[degraded mode: deterministic signals only, no narrative synthesis]",
        "situation": "No model key configured. Usage analytics, health scoring, change "
                     "detection and ranking are still live; narrative reasoning is not.",
        "sentiment_label": "neutral", "sentiment_trend": "stable", "sentiment_reason": "",
        "champion_status": "none-identified", "economic_buyer_identified": False,
        "competitor_present": False, "competitor_names": [],
        "commercial_friction": False, "unresolved_commercial_items": 0,
        "top_risks": [], "opportunities": [], "contradictions": [], "next_actions": [],
        "_degraded": True,
    }


def _fallback_narrative(account, before, after, changes) -> str:
    kinds = {}
    for c in changes:
        kinds[c["change_type"]] = kinds.get(c["change_type"], 0) + 1
    parts = ", ".join(f"{v} {k.lower()}" for k, v in kinds.items())
    b = (before or {}).get("health", {}).get("band")
    a = (after or {}).get("health", {}).get("band")
    move = f" Health moved {b} to {a}." if b and a and b != a else ""
    return f"{account}: {parts} detected.{move}"
