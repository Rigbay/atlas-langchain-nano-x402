owner: codex
purpose: local LangChain Nano x402 bounty candidate
keep-until: permanent

# Guarded LangChain Nano x402 candidate

**Status: experimental.** The real buyer's unpaid quote was observed, a LangChain
agent called this tool against local HTTP, and synthetic block signing was
checked. No real wallet payment, public repository, independent security review,
isolated buyer-container test or buyer acceptance has occurred. Do not treat this as
production-ready or a passing bounty claim.

This tool makes one direct POST to a configured HTTPS endpoint. A 402 response
must offer one exact Nano mainnet payment to an allowlisted destination and fit
the explicit per-call and cumulative caps. It reserves the request durably
before signing, records the public signed-block hash before sending, and never
automatically retries a paid call. It refuses redirects, expired or unsupported
quotes, duplicate requests, an incomplete journal, and a prior unresolved call.

## Install and offline tests

The package pins LangChain 1.4.7 and feeless402 0.2.13. In a disposable Python
3.11+ environment, install `python -m pip install .` and run:

```sh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false python -m unittest discover -s tests -v
```

The tests create a `test-state` directory and retain its synthetic journals.
They do not use a real wallet, send Nano, or call a model endpoint. A framework
agent loop uses a scripted model and an HTTP server on 127.0.0.1. The signing
test uses an obviously public synthetic private key and deliberately invalid
work; it is not a valid network transaction. Set `X402_TEST_STATE_DIR` to place
test journals in another existing workspace. The suite passed on Windows Python
3.14 and in a fresh WSL Linux Python 3.12 environment after installing the local
package and its declared dependencies. That Linux environment is not the buyer's
isolated container or a live payment test.

## Framework example for a later, explicitly authorized live test

`examples/live_agent.py` calls the guarded tool from a real LangChain agent loop
with a scripted chat model. It requires `--live` and an existing, explicitly
supplied wallet, RPC URL, destination and journal; it never creates or funds a
wallet. The tool is pinned to `https://pursekeeper.dev/v1/hash` and the example
caps both the call and the journal lifetime at 0.001 XNO. Obtain and independently
verify the current merchant destination before constructing a policy. A quote
can change; the tool fetches and validates a fresh quote at action time.

Initialize an empty journal **once**, at a deliberate private location:

```python
from atlas_x402.client import initialize_journal
initialize_journal('/chosen/existing/directory/payment-journal.jsonl')
```

Then, after an operator has chosen the wallet, trusted RPC and destination, the
example can be run as follows. It can result in a real transfer:

```sh
python examples/live_agent.py --live --wallet-file /chosen/wallet.json \
  --rpc-url https://chosen-nano-rpc.example --pay-to nano_CHOSEN_ALLOWLISTED_ADDRESS \
  --journal /chosen/existing/directory/payment-journal.jsonl \
  --request-id one-specific-operation --text 'public synthetic test'
```

Use one persistent journal for all clients sharing a wallet; protect it from
deletion, replacement, rollback and untrusted edits. Losing or recreating it can
lose the duplicate and cap history. Never point other unguarded tools at the
same wallet while calls are pending. A 2xx response is not independent proof of
ledger settlement; the returned hash lets an operator check. An incomplete
reservation may be recorded even when nothing was sent; reconcile it manually
before any further spending. This candidate has no automated recovery mechanism.

The Nano SDK wallet file can contain an unencrypted seed. Choose custody and
backups deliberately and never put a real wallet, seed or secret in this repo.
An incoming seed may first need a signed receive/open block before it becomes a
spendable balance. This example checks spendable balance but does not create or
broadcast a receive block.
The example uses only the explicitly configured RPC, but the SDK can ask that
RPC for proof of work and can fall back to local computation. It always supplies
work even where `extra.workOptional` permits omitting it; a slow signer can
outlive the quote and be refused. Neither an actual paid call nor that PoW path
was tested in this pass. The operator is responsible for RPC trust, ledger
confirmation and safe custody.

The client is intentionally narrow: one configured endpoint, one quoted offer,
Nano mainnet only, no redirect following, and no unknown offer extensions. It
does not authenticate the seller beyond HTTPS and the operator's endpoint and
destination choices. It does not establish that a bounty is funded or won.
