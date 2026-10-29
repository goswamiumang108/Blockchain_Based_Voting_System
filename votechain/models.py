"""Plain data containers shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

NOTA_NAME = "None of the above (NOTA)"


@dataclass(frozen=True)
class User:
	id: int
	username: str
	full_name: str
	role: str
	
	@property
	def is_admin(self) -> bool:
		return self.role == "admin"


@dataclass(frozen=True)
class Election:
	id: int
	title: str
	description: str
	status: str  # draft | open | closed
	created_at: str
	opened_at: Optional[str] = None
	closed_at: Optional[str] = None
	allow_nota: bool = False


@dataclass(frozen=True)
class Candidate:
	id: int
	election_id: int
	name: str
	description: str = ""
	is_nota: bool = False


@dataclass(frozen=True)
class VoteReceipt:
	election_id: int
	ballot_id: str
	block_index: Optional[int]  # None while the ballot is still waiting to be mined


@dataclass(frozen=True)
class AcceptedVote:
	election_id: int
	candidate_id: int
	ballot_id: str
	block_index: int


@dataclass(frozen=True)
class RejectedVote:
	block_index: int
	ballot_id: str
	reason: str
	election_id: Optional[int] = None


@dataclass
class TransactionAudit:
	accepted: list[AcceptedVote] = field(default_factory=list)
	rejected: list[RejectedVote] = field(default_factory=list)
	chain_valid: bool = True
	chain_errors: list[str] = field(default_factory=list)
	trusted_blocks: int = 0
	total_blocks: int = 0


@dataclass
class ElectionResults:
	election: Election
	candidates: list[Candidate]
	counts: dict[int, int]
	accepted_total: int
	rejected: list[RejectedVote]
	pending_ballots: int
	eligible_voters: int
	participants: int
	chain_valid: bool
	chain_errors: list[str]
	
	@property
	def turnout(self) -> float:
		return self.participants / self.eligible_voters if self.eligible_voters else 0.0
	
	@property
	def unaccounted(self) -> int:
		"""Participants whose ballot is neither accepted on-chain nor pending."""
		return self.participants - self.accepted_total - self.pending_ballots
	
	@property
	def consistent(self) -> bool:
		return self.chain_valid and not self.rejected and self.unaccounted == 0
	
	@property
	def real_candidates(self) -> list[Candidate]:
		return [c for c in self.candidates if not c.is_nota]

	@property
	def nota_votes(self) -> int:
		return sum(self.counts.get(c.id, 0) for c in self.candidates if c.is_nota)

	@property
	def top_candidate_votes(self) -> int:
		return max((self.counts.get(c.id, 0) for c in self.real_candidates), default=0)

	@property
	def nota_leads(self) -> bool:
		"""True when 'None of the above' received more votes than any real candidate."""
		return self.nota_votes > self.top_candidate_votes

	@property
	def winners(self) -> list[Candidate]:
		top = self.top_candidate_votes
		if top == 0 or self.nota_leads:
			return []
		return [c for c in self.real_candidates if self.counts.get(c.id, 0) == top]

	@property
	def outcome(self) -> str:
		if self.election.status != "closed":
			return "Provisional (voting is still open)"
		if self.nota_leads:
			return "None of the above received the most votes; no candidate is declared elected"
		winners = self.winners
		if not winners:
			return "No votes were cast"
		if len(winners) == 1:
			return f"Winner: {winners[0].name}"
		return "Tie: " + ", ".join(w.name for w in winners)
