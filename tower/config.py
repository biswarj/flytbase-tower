"""TOWER configuration. Everything env-driven so the same code runs in the
container, in GitHub Actions, and on any host."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = ROOT / "web"
PUBLIC = ROOT / "public"          # what GitHub Pages serves

MCP_ENDPOINT = os.environ.get(
    "MCP_ENDPOINT", "https://flytbase-gtm-hackathon.lovable.app/api/mcp"
)
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL_EXTRACT = os.environ.get("TOWER_MODEL_EXTRACT", "claude-sonnet-4-5-20250929")
MODEL_SYNTH = os.environ.get("TOWER_MODEL_SYNTH", "claude-sonnet-4-5-20250929")

# Alternative provider. Used only when no Anthropic key is present, so the
# system is never tied to one vendor's billing state.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("TOWER_OPENAI_MODEL", "gpt-4.1")

POLL_SECONDS = int(os.environ.get("TOWER_POLL_SECONDS", "60"))
MAX_RUN_SECONDS = int(os.environ.get("TOWER_MAX_RUN_SECONDS", str(5 * 3600 + 40 * 60)))

# The corpus uses relative dates ("4 months ago", "6 weeks ago"). Everything
# is resolved against this anchor so timelines are comparable across accounts.
# Defaults to today, overridable for deterministic replay.
REFERENCE_DATE = os.environ.get("TOWER_REFERENCE_DATE", "")

# Renewal risk bands, in days.
RENEWAL_IMMINENT_DAYS = 45
RENEWAL_NEAR_DAYS = 90

LIFECYCLE_ORDER = [
    "pre-sale",
    "newly-sold-onboarding",
    "established",
    "renewal-focused",
    "churned",
]

DOC_AUTHORITY = {
    # When sources contradict each other, higher authority wins and the
    # loser is recorded in the contradiction ledger rather than discarded.
    "transcript": 5,     # the customer's own words, dated
    "email": 4,          # written, attributable
    "ticket": 4,         # system of record for product problems
    "renewal": 3,        # commercial table, often stale
    "note": 2,           # internal opinion
    "profile": 1,        # raw CRM record, staleest of all
    "usage": 5,          # behaviour beats stated intent
}
