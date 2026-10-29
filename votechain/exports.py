"""CSV export of election results (pure functions; no database or UI access)."""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

from .models import Election, ElectionResults

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value) -> str:
	"""Neutralise spreadsheet formula injection: a text cell that starts with = + - @ is prefixed
	with an apostrophe so Excel/Sheets show it as text instead of evaluating it."""
	text = "" if value is None else str(value)
	return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def results_filename(election: Election) -> str:
	slug = re.sub(r"[^A-Za-z0-9]+", "-", election.title).strip("-").lower()[:50] or "election"
	return f"votechain-results-{election.id}-{slug}.csv"


def results_to_csv(results: ElectionResults, generated_at: str, chain_height: Optional[int] = None,
                   chain_head_hash: Optional[str] = None) -> bytes:
	"""Results as UTF-8 CSV (with a BOM so Excel reads accented names correctly).

	Layout: a block of Field,Value rows describing the election and its integrity state, one blank
	row, then the per-candidate table with a TOTAL row. Numbers are written as plain numbers.
	"""
	buf = io.StringIO()
	w = csv.writer(buf, lineterminator="\r\n")
	e = results.election
	share = lambda n: round(n * 100 / results.accepted_total, 2) if results.accepted_total else 0.0

	w.writerow(["Field", "Value"])
	meta = [
		("Election", e.title),
		("Election ID", e.id),
		("Status", e.status),
		("Generated (UTC)", generated_at),
		("Blockchain verified", "Yes" if results.chain_valid else "NO - results incomplete"),
		("Chain height (blocks)", chain_height if chain_height is not None else ""),
		("Chain head hash", chain_head_hash or ""),
		("Eligible voters", results.eligible_voters),
		("Participants (voted)", results.participants),
		("Turnout (%)", round(results.turnout * 100, 2)),
		("Accepted votes (on-chain)", results.accepted_total),
		("Pending ballots (not counted)", results.pending_ballots),
		("Rejected transactions", len(results.rejected)),
		("Outcome", results.outcome),
	]
	for key, value in meta:
		w.writerow([key, _cell(value)])
	w.writerow([])

	w.writerow(["Candidate", "Type", "Votes", "Share of accepted votes (%)"])
	for c in results.candidates:
		votes = results.counts.get(c.id, 0)
		w.writerow([_cell(c.name), "NOTA" if c.is_nota else "Candidate", votes, share(votes)])
	w.writerow(["TOTAL", "", results.accepted_total, share(results.accepted_total)])
	return ("\ufeff" + buf.getvalue()).encode("utf-8")
