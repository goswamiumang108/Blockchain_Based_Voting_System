"""Streamlit pages. Each function is one navigation entry."""
from __future__ import annotations

import json
import pandas as pd
import streamlit as st

from . import context
from ..errors import AuthenticationError, VoteChainError

DISCLAIMER = (
	""
)


def _short(h: str, n: int = 12) -> str:
	return f"{h[:n]}…" if h and len(h) > n else h


STATUS_ICON = {"draft": "📝 draft", "open": "🟢 open", "closed": "🔒 closed"}


# ---------------------------------------------------------------------------------- account
def account_page() -> None:
	services = context.get_services()
	st.title("🗳️ VoteChain: Blockchain based Voting System")
	context.show_flashes()
	tab_login, tab_register = st.tabs(["Sign in", "Register as a voter"])
	
	with tab_login:
		with st.form("login_form"):
			username = st.text_input("Username", key="login_username")
			password = st.text_input("Password", type="password", key="login_password")
			submitted = st.form_submit_button("Sign in", type="primary")
		if submitted:
			try:
				user = services.auth.authenticate(username, password)
			except AuthenticationError as exc:
				st.error(str(exc))
			else:
				context.login(user)
				st.rerun()
	
	with tab_register:
		with st.form("register_form"):
			full_name = st.text_input("Full name", key="reg_full_name")
			reg_user = st.text_input("Username", key="reg_username", help="3-32 letters, digits, _ . -")
			pw1 = st.text_input("Password (min 8 characters)", type="password", key="reg_pw1")
			pw2 = st.text_input("Repeat password", type="password", key="reg_pw2")
			registered = st.form_submit_button("Create account")
		if registered:
			if pw1 != pw2:
				st.error("Passwords do not match.")
			else:
				try:
					services.auth.register(reg_user, full_name, pw1)
				except VoteChainError as exc:
					st.error(str(exc))
				else:
					st.success("Account created. An administrator must enroll you in an election before you can vote. "
					           "You can sign in now.")


# ---------------------------------------------------------------------------------- voting
def vote_page() -> None:
	services, user = context.get_services(), context.current_user()
	voting = services.voting
	st.title("🗳️ Vote")
	context.show_flashes()
	
	open_elections = [e for e in voting.list_elections() if e.status == "open"]
	if not open_elections:
		st.info("There are no open elections right now.")
		return
	election = st.selectbox("Election", open_elections, format_func=lambda e: e.title, key="vote_election")
	if election.description:
		st.write(election.description)
	
	# Every decision below is re-read from the database on each rerun, never from session state.
	if not voting.is_eligible(election.id, user.id):
		st.warning("You are not on the voter roll for this election. Contact an administrator.")
		return
	if voting.has_voted(election.id, user.id):
		st.success("✅ Your ballot has been recorded. You cannot vote again in this election.")
		receipt = st.session_state.get(f"receipt_{election.id}")
		if receipt:
			st.info(f"**Your anonymous ballot receipt** (shown once; save it):\n\n`{receipt.ballot_id}`\n\n"
			        + (f"Included in block #{receipt.block_index}." if receipt.block_index is not None
			           else "Waiting to be mined into a block."))
			st.caption("Look this receipt up on the Blockchain Explorer page to confirm your ballot is on the chain.")
		return
	
	candidates = voting.list_candidates(election.id)
	by_id = {c.id: c for c in candidates}
	with st.form(f"vote_form_{election.id}"):
		choice = st.radio("Select one candidate", [c.id for c in candidates], index=None,
		                  format_func=lambda cid: by_id[cid].name + (
			                  f" — {by_id[cid].description}" if by_id[cid].description else ""))
		confirm = st.checkbox("I understand my vote is final and cannot be changed.")
		submitted = st.form_submit_button("Submit ballot", type="primary")
	if submitted:
		if choice is None:
			st.error("Please select a candidate.")
		elif not confirm:
			st.error("Please confirm that your vote is final.")
		else:
			try:
				receipt = voting.cast_vote(election.id, user.id, choice)
			except VoteChainError as exc:  # includes AlreadyVotedError from a double submit / second tab
				st.error(str(exc))
			else:
				st.session_state[f"receipt_{election.id}"] = receipt
				st.rerun()


# ---------------------------------------------------------------------------------- results
def results_page() -> None:
	services = context.get_services()
	voting = services.voting
	st.title("📊 Results")
	elections = [e for e in voting.list_elections() if e.status != "draft"]
	if not elections:
		st.info("No election has been opened yet.")
		return
	election = st.selectbox("Election", elections, format_func=lambda e: f"{e.title} ({e.status})",
	                        key="results_election")
	res = voting.compute_results(election.id)
	
	if not res.chain_valid:
		st.error("⚠️ Blockchain integrity check FAILED. Only blocks before the first invalid block are counted; "
		         "these results are incomplete. See the Blockchain Explorer.")
	if election.status == "open":
		st.warning("Voting is still open: these are provisional counts from mined (accepted) ballots only.")
	else:
		st.success("Voting is closed. These are the final counts from validated on-chain ballots.")
	
	c1, c2, c3, c4 = st.columns(4)
	c1.metric("Accepted votes", res.accepted_total)
	c2.metric("Eligible voters", res.eligible_voters)
	c3.metric("Turnout", f"{res.turnout:.0%}")
	c4.metric("Pending (unmined)", res.pending_ballots)
	
	names = {c.id: c.name for c in res.candidates}
	df = pd.DataFrame({"Candidate": [names[i] for i in res.counts], "Votes": list(res.counts.values())})
	df["Share"] = (df["Votes"] / res.accepted_total * 100).round(1) if res.accepted_total else 0.0
	st.bar_chart(df.set_index("Candidate")["Votes"])
	st.dataframe(df.sort_values("Votes", ascending=False), hide_index=True)
	if election.status == "closed":
		if res.winners:
			st.subheader("🏆 " + (res.winners[0].name if len(res.winners) == 1
			                     else "Tie: " + ", ".join(w.name for w in res.winners)))
		else:
			st.subheader("No votes were cast.")
	
	with st.expander("Audit details"):
		st.write(f"Participants recorded: **{res.participants}** · accepted on-chain: **{res.accepted_total}** · "
		         f"pending: **{res.pending_ballots}** · unaccounted: **{res.unaccounted}**")
		if res.consistent:
			st.success("Participation records, pending pool and accepted ballots are consistent.")
		else:
			st.error("Inconsistency detected between participation records and accepted ballots.")
		if res.rejected:
			st.write("Rejected transactions found on the chain (not counted):")
			st.dataframe(pd.DataFrame([r.__dict__ for r in res.rejected]), hide_index=True)


# ---------------------------------------------------------------------------------- explorer
def explorer_page() -> None:
	services = context.get_services()
	voting = services.voting
	st.title("⛓️ Blockchain Explorer")
	st.caption(DISCLAIMER)
	
	blocks = voting.get_blocks()
	audit = voting.audit_transactions()
	if audit.chain_valid:
		st.success(f"✅ Chain verified: {len(blocks)} blocks, every hash, link and proof-of-work target is valid.")
	else:
		st.error("❌ Chain verification FAILED")
		for err in audit.chain_errors:
			st.write(f"- {err}")
		st.caption(f"{audit.trusted_blocks} leading block(s) remain trusted; later blocks are excluded from results.")
	
	c1, c2, c3, c4 = st.columns(4)
	c1.metric("Blocks", len(blocks))
	c2.metric("Accepted votes", len(audit.accepted))
	c3.metric("Rejected txs", len(audit.rejected))
	c4.metric("Pending ballots", voting.pending_count())
	st.caption(f"Mining difficulty for new blocks: **{voting.difficulty}** leading hex zeros")
	
	st.subheader("Blocks")
	st.dataframe(pd.DataFrame([{
		"Index": b.index, "Timestamp (UTC)": b.timestamp, "Txs": len(b.transactions), "Nonce": b.nonce,
		"Difficulty": b.difficulty, "Hash": _short(b.hash, 16), "Previous hash": _short(b.previous_hash, 16),
	} for b in reversed(blocks)]), hide_index=True)
	
	st.subheader("Block details")
	idx = st.number_input("Block index", min_value=0, max_value=len(blocks) - 1, value=len(blocks) - 1, step=1)
	block = blocks[int(idx)]
	ok = block.calculate_hash() == block.hash and block.meets_difficulty()
	st.write(("✅ Block hash and proof-of-work are valid" if ok else "❌ Block hash or proof-of-work is INVALID"))
	st.json(block.to_dict())
	
	st.subheader("Verify a ballot receipt")
	receipt = st.text_input("Ballot receipt ID", key="receipt_lookup").strip().lower()
	if receipt:
		hits = [(b.index, t) for b in blocks for t in b.transactions if
		        isinstance(t, dict) and t.get("ballot_id") == receipt]
		accepted = {v.ballot_id for v in audit.accepted}
		if hits:
			bi, tx = hits[0]
			st.success(f"Found in block #{bi}. Status: " + ("accepted ✅" if receipt in accepted else "REJECTED ❌"))
			st.json(tx)
		elif any(p["ballot_id"] == receipt for p in voting.list_pending()):
			st.info("This ballot is pending and has not been mined into a block yet.")
		else:
			st.error("No ballot with that receipt ID was found.")
	
	pending = voting.list_pending()
	with st.expander(f"Pending ballot pool ({len(pending)})"):
		st.dataframe(pd.DataFrame(pending), hide_index=True) if pending else st.write("Empty.")
	
	st.download_button("Download chain as JSON", json.dumps([b.to_dict() for b in blocks], indent=2),
	                   file_name="votechain.json", mime="application/json")


# ---------------------------------------------------------------------------------- admin
def admin_page() -> None:
	services, user = context.get_services(), context.current_user()
	voting, auth = services.voting, services.auth
	st.title("🛠️ Administration")
	if not user.is_admin:  # also enforced inside every service call
		st.error("Administrator access required.")
		st.stop()
	context.show_flashes()
	
	tab_new, tab_manage, tab_mine = st.tabs(["Create election", "Manage election", "Mining"])
	
	with tab_new:
		with st.form("create_election", clear_on_submit=True):
			title = st.text_input("Title")
			description = st.text_area("Description")
			if st.form_submit_button("Create draft election", type="primary"):
				try:
					e = voting.create_election(user.id, title, description)
				except VoteChainError as exc:
					st.error(str(exc))
				else:
					context.flash("success", f"Created draft election “{e.title}”. Add candidates and voters next.")
					st.rerun()
	
	with tab_manage:
		elections = voting.list_elections()
		if not elections:
			st.info("Create an election first.")
		else:
			e = st.selectbox("Election", elections, format_func=lambda x: f"{x.title} — {STATUS_ICON[x.status]}",
			                 key="admin_election")
			candidates = voting.list_candidates(e.id)
			eligible = voting.list_eligible(e.id)
			
			left, right = st.columns(2)
			with left:
				st.subheader(f"Candidates ({len(candidates)})")
				for c in candidates:
					st.write(f"• **{c.name}**" + (f" — {c.description}" if c.description else ""))
				if e.status == "draft":
					with st.form(f"add_cand_{e.id}", clear_on_submit=True):
						name = st.text_input("Candidate name")
						desc = st.text_input("Short description (optional)")
						if st.form_submit_button("Add candidate"):
							try:
								voting.add_candidate(user.id, e.id, name, desc)
							except VoteChainError as exc:
								st.error(str(exc))
							else:
								st.rerun()
					if candidates:
						rm = st.selectbox("Remove candidate", candidates, format_func=lambda c: c.name,
						                  index=None, key=f"rm_cand_{e.id}")
						if rm and st.button("Remove selected candidate", key=f"rm_btn_{e.id}"):
							try:
								voting.remove_candidate(user.id, rm.id)
							except VoteChainError as exc:
								st.error(str(exc))
							else:
								st.rerun()
				else:
					st.caption("Candidates are locked once the election is open.")
			
			with right:
				st.subheader(f"Voter roll ({len(eligible)})")
				enrolled_ids = {u.id for u in eligible}
				available = [u for u in auth.list_users() if u.id not in enrolled_ids]
				if e.status != "closed":
					picks = st.multiselect("Enroll registered users", available,
					                       format_func=lambda u: f"{u.full_name} (@{u.username})", key=f"enroll_{e.id}")
					b1, b2 = st.columns(2)
					if b1.button("Enroll selected", key=f"enroll_btn_{e.id}", disabled=not picks):
						try:
							n = voting.enroll_voters(user.id, e.id, [u.id for u in picks])
						except VoteChainError as exc:
							st.error(str(exc))
						else:
							context.flash("success", f"Enrolled {n} voter(s).")
							st.rerun()
					if b2.button("Enroll all voters", key=f"enroll_all_{e.id}"):
						try:
							n = voting.enroll_all_voters(user.id, e.id)
						except VoteChainError as exc:
							st.error(str(exc))
						else:
							context.flash("success", f"Enrolled {n} new voter(s).")
							st.rerun()
				if eligible:
					voted = {u.id: voting.has_voted(e.id, u.id) for u in eligible}
					st.dataframe(pd.DataFrame([{"Voter": u.full_name, "Username": u.username,
					                            "Voted": "yes" if voted[u.id] else "no"} for u in eligible]),
					             hide_index=True)
			
			st.divider()
			st.subheader("Lifecycle")
			if e.status == "draft":
				if st.button("▶️ Open election for voting", type="primary", key=f"open_{e.id}"):
					try:
						voting.open_election(user.id, e.id)
					except VoteChainError as exc:
						st.error(str(exc))
					else:
						context.flash("success", "Election is now open.")
						st.rerun()
			elif e.status == "open":
				st.caption("Closing stops voting and mines all remaining pending ballots.")
				if st.button("⏹️ Close election", key=f"close_{e.id}"):
					try:
						voting.close_election(user.id, e.id)
					except VoteChainError as exc:
						st.error(str(exc))
					else:
						context.flash("success", "Election closed; results are final.")
						st.rerun()
			else:
				st.info("This election is closed.")
	
	with tab_mine:
		pending = voting.pending_count()
		st.write(f"Pending ballots: **{pending}** · difficulty: **{voting.difficulty}** · "
		         f"auto-mine: **{'on' if voting.auto_mine else 'off'}** · max txs/block: **{voting.max_txs}**")
		if st.button("⛏️ Mine pending ballots", disabled=pending == 0):
			try:
				mined = voting.mine_pending()
			except VoteChainError as exc:
				st.error(str(exc))
			else:
				context.flash("success", f"Mined {len(mined)} block(s).")
				st.rerun()
