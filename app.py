"""VoteChain: Streamlit entry point.   Run with:  streamlit run app.py"""
import streamlit as st

st.set_page_config(page_title="VoteChain", page_icon="🗳️", layout="wide")

from votechain.ui import context, pages  # noqa: E402  (after set_page_config)

context.bridge_secrets()

try:
	context.get_services()
except Exception as exc:  # misconfiguration (e.g. weak ADMIN_PASSWORD) should be visible, not a stack trace
	st.error(f"Application failed to start: {exc}")
	st.stop()

PRODUCT_NAME = "VoteChain: Blockchain based Voting System"

user = context.current_user()

if user is None:
	nav_pages = [st.Page(pages.account_page, title="Sign in", icon="🔐", url_path="signin", default=True)]
else:
	nav_pages = [
		st.Page(pages.vote_page, title="Vote", icon="🗳️", url_path="vote", default=True),
		st.Page(pages.results_page, title="Results", icon="📊", url_path="results"),
		st.Page(pages.explorer_page, title="Blockchain Explorer", icon="⛓️", url_path="explorer"),
	]
	if user.is_admin:
		nav_pages.append(st.Page(pages.admin_page, title="Admin", icon="🛠️", url_path="admin"))

# Streamlit's built-in page list is hidden so the sidebar can start with the product name on every page.
navigation = st.navigation(nav_pages, position="hidden")

with st.sidebar:
	st.markdown(f"### {PRODUCT_NAME}")
	for nav_page in nav_pages:
		st.page_link(nav_page)
	if user is not None:
		st.divider()
		st.markdown(f"Signed in as **{user.full_name}**  \n`@{user.username}` · {user.role}")
		if st.button("Sign out"):
			context.logout()
			st.rerun()

navigation.run()
