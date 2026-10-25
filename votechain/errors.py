"""Domain exceptions. The UI catches these and shows their message to the user."""


class VoteChainError(Exception):
	"""Base class for all expected, user-presentable errors."""


class ValidationError(VoteChainError):
	"""Input failed validation."""


class AuthenticationError(VoteChainError):
	"""Bad credentials."""


class AccountLockedError(AuthenticationError):
	"""Too many failed sign-in attempts."""


class PermissionDeniedError(VoteChainError):
	"""Actor lacks the required role."""


class ElectionStateError(VoteChainError):
	"""Operation not allowed in the election's current state."""


class NotEligibleError(VoteChainError):
	"""Voter is not on the election's voter roll."""


class AlreadyVotedError(VoteChainError):
	"""Voter has already voted in this election."""


class InvalidCandidateError(ValidationError):
	"""Candidate does not belong to the election."""


class ChainIntegrityError(VoteChainError):
	"""The stored blockchain failed verification."""
