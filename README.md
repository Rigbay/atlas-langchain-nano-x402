# Guarded LangChain Nano x402

A small LangChain tool for one explicitly configured HTTPS endpoint. It reads an
x402 quote, checks the Nano mainnet destination and the operator's spending
limits, and records a durable reservation and signed-block hash before a
payment. It rejects redirects, duplicate requests, expired or unsupported
quotes, and unresolved earlier payments. It does not automatically retry a
paid request.

The buyer's independent 29 October 2026 run at commit
6973d96edb2d84d25de36b992dff9c120e26eebf reported that rules 1–4 held:
23 offline tests passed and the LangChain agent example completed one real
0.001 XNO request. The buyer found rule 5 unmet because the package was not
available on PyPI. See the [buyer verdict](https://github.com/pursekeeper/api/issues/133#issuecomment-6497956396).
A package release or later test does not by itself establish bounty acceptance
or payment.

## Install from PyPI

For a published 0.1.0 release, use a Python 3.11+ environment:

~~~sh
python -m pip install --only-binary=:all: "atlas-langchain-nano-x402==0.1.0"
~~~

This installs binary distributions without running a source build. The project
pins LangChain 1.4.7, feeless402 0.2.13, nanopy 28.0.1.post26 and requests
2.34.2. Compatible wheels must be available for your platform.

## Live LangChain example

The installed module below uses the framework's own agent loop with a scripted
chat model. It does not contact a model API. It requires an existing, explicitly
chosen Nano wallet, allowlisted destination and initialized journal. Running
with --live can spend Nano; review the controls below first.

Initialize one empty journal in an existing private directory:

~~~python
from atlas_x402.client import initialize_journal
initialize_journal('/chosen/existing/directory/payment-journal.jsonl')
~~~

Then, after independently verifying the current merchant address:

~~~sh
python -m atlas_x402.example --live --wallet-file /chosen/wallet.json \
  --pay-to nano_CHOSEN_ALLOWLISTED_ADDRESS \
  --journal /chosen/existing/directory/payment-journal.jsonl \
  --request-id one-specific-operation --text 'public synthetic test'
~~~

The example uses https://pursekeeper.dev/v1/hash and caps the call and lifetime
of this journal at 0.001 XNO. By default it uses the seller's account-state
and free-work endpoints; --rpc-url may select a different trusted HTTPS node.
It does not create or fund a wallet or receive an unopened account's funds.

The Nano SDK wallet file can contain an unencrypted seed. Choose custody and
backups deliberately; never place a real wallet, seed or secret in this
repository. Account state is trusted: a dishonest node can misstate a balance,
changing the actual transfer even if the quote is capped. Independently verify
the previous block, balance and frontier before spending. An HTTP success alone
does not prove ledger settlement.

Use one persistent journal for all clients sharing a wallet. Protect it from
deletion, replacement, rollback and untrusted edits. Do not use unguarded tools
with that wallet while a call is pending. If a reservation is incomplete,
reconcile it manually; do not sign a replacement or retry automatically.

When the quote explicitly permits seller-provided work, the signer uses the
scheme's workless placeholder. Otherwise it asks the configured work source
and validates the result. The seller adapter fails closed if free work is not
available. Quote expiration can still prevent a slow signing operation from
being presented.

## Offline tests from the source checkout

After installing the package, the repository's tests can be run separately:

~~~sh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false python -m unittest discover -s tests -v
~~~

Tests use synthetic keys, a local HTTP server and a scripted model. They do not
spend funds or call a model endpoint. The tool is intentionally narrow: one
configured endpoint, one quoted offer, Nano mainnet only, no redirect following
and no unknown offer extensions. The buyer's review was not a security audit.
