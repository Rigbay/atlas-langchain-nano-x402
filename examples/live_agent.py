"""Explicit live example. It never creates a wallet or journal.

Running with --live can spend Nano. See README before running.
"""
import argparse
from pathlib import Path
from urllib.parse import urlsplit

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from nano_pay.rpc import RPC
from nano_pay.wallet import Wallet

from atlas_x402 import GuardedClient, NanoPaySigner, Policy, make_paid_text_tool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--wallet-file", type=Path, required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--pay-to", required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--text", required=True)
    args = parser.parse_args()
    parts = urlsplit(args.rpc_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        parser.error("RPC URL must be an explicit HTTPS URL without credentials or fragment")
    if not args.wallet_file.is_file() or not args.journal.is_file():
        parser.error("the explicitly chosen wallet and initialized journal must already exist")
    wallet = Wallet(path=args.wallet_file).load()
    rpc = RPC(urls=[args.rpc_url], work_urls=[])
    raw = 10**27  # 0.001 XNO; per-call and lifetime of this journal.
    # A freshly seeded account can have a receivable balance but no spendable
    # balance until a receive/open block has been processed separately.
    if wallet.synced_account(rpc).raw_bal < raw:
        parser.error("wallet's spendable balance is below 0.001 XNO")
    policy = Policy("https://pursekeeper.dev/v1/hash", frozenset({args.pay_to}), raw, raw)
    client = GuardedClient(policy=policy, journal=args.journal, signer=NanoPaySigner(wallet, rpc))

    class ScriptedChat(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs): return self

    # A scripted model gives a reproducible framework loop without any model API.
    model = ScriptedChat(responses=[
        AIMessage(content="", tool_calls=[{"name": "paid_text_request",
            "args": {"text": args.text, "request_id": args.request_id},
            "id": "call-1", "type": "tool_call"}]),
        AIMessage(content="The tool returned a result.")])
    result = create_agent(model=model, tools=[make_paid_text_tool(client)]).invoke(
        {"messages": [{"role": "user", "content": "Use the configured tool once."}]})
    for message in result["messages"]:
        if getattr(message, "type", "") == "tool":
            print(message.content)


if __name__ == "__main__":
    main()
