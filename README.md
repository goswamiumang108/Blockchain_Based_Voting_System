# 🗳️ VoteChain: Blockchain-Based Voting

## Developed by [Umang Goswami (@goswamiumang108)](https://github.com/goswamiumang108)

---

VoteChain is a blockchain-based voting system built with Python, Streamlit and SQLite. Voters register and sign in with bcrypt-protected passwords, while an administrator creates elections, adds candidates, enrolls eligible voters, and opens and closes voting. Each ballot is checked on the server before it is accepted. A single database transaction confirms that the election is open, the voter is on the roll and has not voted, and that the candidate belongs to the election. Database constraints back these checks up. The ballot that is stored carries only the chosen candidate, a random receipt ID and a keyed voter tag, so voter identity is kept apart from ballot data.

Accepted ballots are sealed into blocks on a blockchain written from scratch. Each block holds an index, timestamp, transactions, the previous block’s hash, a nonce and its own SHA-256 hash, and mining uses a Proof of Work with a configurable difficulty. A blockchain explorer verifies the full chain on demand and lets a voter confirm their ballot using the receipt ID. Election results are computed only from validated ballots in verified blocks, so an altered block is detected and excluded from the count. All data is stored in SQLite, so it persists across restarts, and the app can be deployed on Streamlit Community Cloud or any Python host.

## Features

| Area        | What is implemented                                                                                                                                                     |
|-------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Accounts**    | Self-registration (voters only), bcrypt password hashes, input validation, 5-strike lockout, generic login errors                                                       |
| **Admin**       | Create elections, manage candidates, enroll voters, open/close elections, manual mining. Admin accounts can only be created by the operator (env vars or CLI)           |
| **Voting**      | One vote per voter per election, enforced by SQLite PK/FK/triggers inside a `BEGIN IMMEDIATE` transaction                                                               |
| **Blockchain**  | `Block` = index, timestamp, transactions, previous_hash, nonce, hash (+ `difficulty` target). Configurable PoW. Verification of genesis, indices, links, hashes and PoW |
| **Explorer**    | Live integrity verification, block table and details, ballot-receipt lookup, pending pool, JSON export                                                                  |
| **Results**     | Counted **only** from validated, accepted on-chain ballots, with an audit panel                                                                                         |
| **Persistence** | Everything (users, elections, roll, participation, pending ballots, blocks) is stored in SQLite                                                                         |

## Architecture

```
app.py                    Streamlit entry point + st.navigation (Sign in / Vote / Results / Explorer / Admin)
votechain/
  blockchain.py           PURE chain logic: Block, mine_block (PoW), verify_chain, Blockchain
  database.py             SQLite schema, constraints, triggers, transaction helper, block persistence
  auth.py                 Registration, bcrypt login, lockout
  voting.py               Elections, atomic cast_vote, mining, transaction validation, tallying
  services.py             Composition root (builds the services from Settings)
  config.py / models.py / errors.py
  ui/context.py           Cached services, session helpers (session carries only a user id)
  ui/pages.py             Streamlit pages (widgets only, no business rules)
create_admin.py           Operator CLI to create an admin account
```

**Vote flow.** `cast_vote()` runs one write transaction that checks: election open → voter on roll → not already
voted → candidate belongs to election; then inserts a `voter_participation` row (PK `(election_id,user_id)`) and an
anonymous `pending_ballots` row. Pending ballots are mined into a block (automatically, or via Admin → Mining). If
mining
fails after the commit, the vote is not lost, it stays pending.

**Streamlit reruns.** Rerun-safety comes from the server, not from `st.session_state`. The session only stores a
user id; role, eligibility and "already voted" are re-read from SQLite on every run. The vote button lives in an
`st.form`, and a double click, a second browser tab or a replayed request hits the database constraint and
gets `AlreadyVotedError`.

**Identity vs. ballot separation.**

- `voter_participation` records *that* a user voted: no candidate, no ballot id, no timestamp.
- The chain records *what* was cast. A transaction holds `election_id`, `candidate_id`, a random `ballot_id`
  and `voter_tag = HMAC-SHA256(server_secret, "election:user")`. No username or user id. Transactions inside a block
  are sorted by random ballot id so block order does not reveal the order of voting.
- The tag still lets the server verify eligibility and duplicates *on-chain*. During results the chain is checked
  for unknown candidates, tags not on the roll, duplicate tags and duplicate ballot ids; invalid ballots are listed
  as rejected and not counted.
- Caveat: whoever holds the server secret can recompute tags and link voters to ballots, and a database operator
  can correlate timing. This is separation "where practical", not cryptographic ballot secrecy.

## Installation and setup

Requires **Python 3.14+**

### 1. Get the code

Clone the repository (or unzip the project) and open a terminal in the project folder:

```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>            # the folder containing app.py
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure the admin account

The admin account is created automatically on first start from `.streamlit/secrets.toml`.
Copy the template if you haven't already, then set your own values:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

```toml
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "a-long-unique-password"
VOTECHAIN_TAG_SECRET = "64-random-hex-characters"   # python -c "import secrets; print(secrets.token_hex(32))"
VOTECHAIN_DIFFICULTY = "3"
```

`secrets.toml` is git-ignored; never commit it. See [Configuration](#configuration) for all available keys.
Alternatively, create an admin from the command line: `python create_admin.py myadmin` (prompts for the password).

### 4. Launch the app

```bash
streamlit run app.py                 # opens http://localhost:8501
```

Sign in with the `ADMIN_USERNAME` / `ADMIN_PASSWORD` from your secrets file.
Press `Ctrl+C` to stop. Next time, re-activate the environment (`source .venv/bin/activate`)
and run `streamlit run app.py` again; the data in `data/` persists.

### 5. First use

Register voters (Sign in page → *Register as a voter*) → sign in as admin → **Admin → Create election** →
add at least 2 candidates → enroll voters → **Open** → voters vote → **Results** / **Blockchain Explorer** → **Close**.

## Configuration

Environment variables (on Streamlit Cloud, put the same keys in *Secrets*):

| Variable                           | Default                 | Meaning                                                                                                                  |
|------------------------------------|-------------------------|--------------------------------------------------------------------------------------------------------------------------|
| `VOTECHAIN_DB_PATH`                | `data/votechain.db`     | SQLite file (the tag secret key file is created next to it)                                                              |
| `VOTECHAIN_DIFFICULTY`             | `3`                     | PoW target: leading hex zeros for **new** blocks (clamped 1–6; each +1 ≈ 16× work)                                       |
| `VOTECHAIN_TAG_SECRET`             | auto-generated key file | Secret for anonymous voter tags. **Back it up with the DB**; if it is lost, existing ballots fail eligibility validation |
| `VOTECHAIN_AUTO_MINE`              | `true`                  | Mine each ballot immediately. If `false`, the admin mines (or closing the election mines)                                |
| `VOTECHAIN_MAX_TXS_PER_BLOCK`      | `50`                    | Block size limit                                                                                                         |
| `VOTECHAIN_BCRYPT_ROUNDS`          | `12`                    | bcrypt cost (4–15)                                                                                                       |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | unset                   | Bootstrap an admin on first start                                                                                        |

Each block stores the difficulty it was mined with, so changing `VOTECHAIN_DIFFICULTY` later does not invalidate old
blocks.

## Deployment (Streamlit Community Cloud)

```bash
git init && git add . && git commit -m "VoteChain"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

1. Go to <https://share.streamlit.io> → **Create app** → pick the repo, branch `main`, main file `app.py`.
2. **Advanced settings**: choose Python 3.12 and paste your **Secrets** (see below).
```toml
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "a-long-unique-password"
VOTECHAIN_TAG_SECRET = "64-random-hex-characters"   # python -c "import secrets; print(secrets.token_hex(32))"
VOTECHAIN_DIFFICULTY = "3"
```
3. Click **Deploy**. Every later `git push` to `main` redeploys the app.

> **Storage is ephemeral on Community Cloud.** The container filesystem is reset when the app is rebuilt, rebooted,
> or restarts after sleeping, which wipes the SQLite file. Don't rely on results
> surviving. Persistence across application restarts does work wherever the disk persists, such as when you run the app
> locally.

## Security model and limitations

What it does:

- bcrypt password hashes (per-password salt, cost 12), no plaintext storage, passwords of at least 8 characters (max 72
  bytes, the bcrypt limit), lockout after 5 failures.
- Eligibility, "one vote", election state and admin permissions are enforced server-side in transactions, with
  SQLite constraints and triggers as defence in depth (PK/FK/UNIQUE, append-only `blocks`, immutable participation rows,
  frozen candidates/roll once open, forward-only election status).
- Every block is SHA-256 hash-linked and PoW-sealed. Results come only from the verified prefix of the chain, and each
  ballot is
  re-validated. Tampering with a block is detected, and the tampered block and everything after it is excluded from
  results.
- The service refuses to extend a chain that fails verification.

What it does **not** do (do not present it as more than it is):

- **Not decentralized.** One process, one database, one operator. There are no peers, consensus or network. An attacker
  (or operator) with write access to the DB *and* the code can rewrite the whole chain and re-mine it. Low-difficulty
  PoW
  gives no meaningful protection here. Real tamper-resistance would need independent replicas/validators or periodic
  anchoring
  of block hashes to an external system.
- **Not a secret-ballot guarantee.** See the caveat above; there is no blind-signature or zero-knowledge scheme.
- **Not hardened for real elections.** No voter identity proofing, no TLS termination, no persistent sessions,
  no CSRF/rate-limiting beyond Streamlit defaults and the login lockout, no coercion resistance, no independent audit.
  Don't use it for binding votes.
- Sessions are per browser tab (Streamlit session state); a page refresh signs the user out.
