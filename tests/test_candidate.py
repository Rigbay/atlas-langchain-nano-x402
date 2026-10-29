"""Local, synthetic tests: no real wallet, model endpoint, or public payment."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from atlas_x402.client import (GuardedClient, NanoPaySigner, Policy, Refused,
                               RequestsTransport, initialize_journal)

RAW = 10**27
DEST = "nano_synthetic_fixture_destination"
OFFER = {"scheme": "exact", "network": "nano:mainnet", "asset": "XNO",
         "payTo": DEST, "amount": str(RAW), "maxTimeoutSeconds": 60,
         "extra": {"work": "optional", "workOptional": True,
                   "workThreshold": "fffffff800000000", "session": "synthetic"}}
QUOTE = {"x402Version": 2, "accepts": [OFFER]}
STATE = Path(os.environ.get("X402_TEST_STATE_DIR", "test-state")) / str(uuid.uuid4())
STATE.mkdir(parents=True)


class FakeSigner:
    def __init__(self): self.calls = 0
    def sign(self, destination, amount):
        self.calls += 1
        return {"type": "state", "signature": "synthetic"}, "a" * 64


class FakeTransport:
    def __init__(self):
        self.quote = copy.deepcopy(QUOTE)
        self.quote_status = 402
        self.paid_status = 200
        self.quoted = self.presented = 0
        self.last_header = None

    def post(self, url, body, payment_header=None):
        result = requests.Response()
        if payment_header is None:
            self.quoted += 1
            result.status_code = self.quote_status
            result._content = json.dumps(self.quote).encode()
            result.headers["Content-Type"] = "application/json"
        else:
            self.presented += 1
            self.last_header = payment_header
            result.status_code = self.paid_status
            result._content = b'{"synthetic":true}'
        return result


class Tests(unittest.TestCase):
    def setUp(self):
        self.path = STATE / (str(uuid.uuid4()) + ".jsonl")
        initialize_journal(self.path)
        self.signer = FakeSigner()
        self.transport = FakeTransport()
        self.policy = Policy("https://seller.example/v1/hash", frozenset({DEST}), RAW, 2 * RAW)
        self.client = GuardedClient(policy=self.policy, journal=self.path,
                                    signer=self.signer, transport=self.transport)

    def call(self, text="hello", rid="request-1"):
        return self.client.invoke(text, rid)

    def reject_before_signing(self):
        with self.assertRaises(Refused): self.call()
        self.assertEqual(self.signer.calls, 0)

    def test_header_echoes_exact_validated_offer_and_public_hash_is_recorded(self):
        result = self.call()
        payload = json.loads(base64.b64decode(self.transport.last_header))
        self.assertEqual(payload["accepted"], OFFER)
        self.assertEqual(payload["payload"]["block"]["link_as_account"], DEST)
        self.assertEqual(result["block_hash"], "a" * 64)
        self.assertEqual(result["settlement"], "unverified")
        journal = self.path.read_text()
        self.assertIn("a" * 64, journal)
        self.assertNotIn("hello", journal)
        self.assertNotIn(self.transport.last_header, journal)

    def test_duplicate_request_id_and_fingerprint_survive_new_client(self):
        self.call()
        second = GuardedClient(policy=self.policy, journal=self.path,
                               signer=self.signer, transport=self.transport)
        with self.assertRaises(Refused): second.invoke("hello", "other-id")
        with self.assertRaises(Refused): second.invoke("different", "request-1")
        self.assertEqual((self.signer.calls, self.transport.presented), (1, 1))

    def test_wrong_offer_fields_and_ambiguous_offer(self):
        for field, value in [("network", "nano:testnet"), ("asset", "BTC"),
                             ("scheme", "other"), ("payTo", "nano_evil"),
                             ("amount", "-1"), ("amount", "0"), ("amount", "1e27"),
                             ("maxTimeoutSeconds", True), ("maxTimeoutSeconds", 0)]:
            with self.subTest(field=field, value=value):
                self.transport.quote = copy.deepcopy(QUOTE)
                self.transport.quote["accepts"][0][field] = value
                self.reject_before_signing()
        self.transport.quote = copy.deepcopy(QUOTE)
        self.transport.quote["accepts"].append(copy.deepcopy(OFFER))
        self.reject_before_signing()

    def test_call_cap_and_cumulative_cap(self):
        self.transport.quote["accepts"][0]["amount"] = str(RAW + 1)
        self.reject_before_signing()
        self.transport.quote = copy.deepcopy(QUOTE)
        self.call("first", "first")
        self.call("second", "second")
        with self.assertRaises(Refused): self.call("third", "third")
        self.assertEqual(self.signer.calls, 2)

    def test_unsupported_split_and_invalid_work_policy_fail_closed(self):
        for extra in [{"splits": [{"payTo": "nano_evil", "amount": "1"}]},
                      {"workOptional": "true"}, {"work": "optional", "workOptional": False},
                      {"workThreshold": "not-hex"}, {"workThreshold": "ffffffffffffffff"}]:
            with self.subTest(extra=extra):
                self.transport.quote = copy.deepcopy(QUOTE)
                self.transport.quote["accepts"][0]["extra"] = extra
                self.reject_before_signing()

    def test_paid_failure_never_retried_and_blocks_other_requests(self):
        self.transport.paid_status = 503
        with self.assertRaises(Refused): self.call()
        with self.assertRaises(Refused): self.call("different", "different")
        self.assertEqual((self.signer.calls, self.transport.presented), (1, 1))
        self.assertIn('"event":"signed"', self.path.read_text())

    def test_signer_exception_is_reserved_and_never_retried(self):
        class BrokenSigner:
            def sign(self, *args): raise RuntimeError("synthetic signer failure")
        self.client.signer = BrokenSigner()
        with self.assertRaises(RuntimeError): self.call()
        with self.assertRaises(Refused): self.call("different", "different")
        self.assertEqual(self.transport.presented, 0)

    def test_expired_quote_refetched_then_refused(self):
        ticks = iter([0, 61, 100, 161])
        self.client.clock = lambda: next(ticks)
        self.reject_before_signing()
        self.assertEqual(self.transport.quoted, 2)

    def test_expired_during_signing_is_not_presented(self):
        ticks = iter([0, 1, 2, 3, 61])
        self.client.clock = lambda: next(ticks)
        with self.assertRaises(Refused): self.call()
        self.assertEqual((self.signer.calls, self.transport.presented), (1, 0))
        self.assertIn("a" * 64, self.path.read_text())

    def test_corrupt_or_missing_journal_fails_closed(self):
        with self.path.open("ab") as f: f.write(b"not-json")
        self.reject_before_signing()
        self.client.journal = STATE / "never-created.jsonl"
        self.reject_before_signing()

    def test_endpoint_requires_https_and_rejects_credentials(self):
        for endpoint in ["http://seller.example", "https://u:p@seller.example/", "https://seller.example/#x"]:
            with self.assertRaises(Refused):
                Policy(endpoint, frozenset({DEST}), RAW, RAW).validate()

    def test_redirect_quote_refused(self):
        self.transport.quote_status = 302
        self.reject_before_signing()

    def test_signer_adapter_passes_destination_and_amount(self):
        class Wallet:
            def build_payment_block(self, rpc, destination, amount):
                self.args = (rpc, destination, amount)
                return {"fixture": True}, "a" * 64, "root"
        wallet = Wallet()
        block, h = NanoPaySigner(wallet, "rpc-fixture").sign(DEST, RAW)
        self.assertEqual(wallet.args, ("rpc-fixture", DEST, RAW))
        self.assertEqual((block, h), ({"fixture": True}, "a" * 64))

    def test_real_nanopy_block_signing_with_public_synthetic_key(self):
        import nanopy
        from nano_pay.wallet import Wallet
        # Public synthetic key and balance; deliberately invalid work, no network.
        account = nanopy.Account(sk="42" * 32)
        account.frontier = "01" * 32
        account.raw_bal = 2 * RAW
        recipient = nanopy.Account(pk="02" * 32)
        wallet = Wallet(path=self.path.with_suffix(".unused-wallet"))
        wallet.synced_account = lambda _rpc: account
        wallet.get_work = lambda _root, _difficulty, _rpc: "0" * 16
        block, block_hash = NanoPaySigner(wallet, object()).sign(recipient.addr, RAW)
        self.assertEqual(block["link"], recipient.pk)
        self.assertEqual(block["balance"], str(RAW))
        reconstructed = nanopy.StateBlock(account, account.rep, int(block["balance"]),
                                           block["previous"], block["link"], block["signature"], block["work"])
        self.assertEqual(reconstructed.hash_, block_hash)
        self.assertTrue(reconstructed.verify_signature())

    def test_two_clients_same_journal_do_not_overspend(self):
        self.policy = Policy(self.policy.endpoint, self.policy.pay_to, RAW, RAW)
        barrier = threading.Barrier(2)
        results = []
        def run(i):
            client = GuardedClient(policy=self.policy, journal=self.path,
                                   signer=self.signer, transport=self.transport)
            barrier.wait()
            try: client.invoke("text" + str(i), "id" + str(i)); results.append("ok")
            except Refused: results.append("refused")
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(results, ["ok", "refused"])
        self.assertEqual(self.signer.calls, 1)

    def test_two_processes_share_journal_cap(self):
        gate = STATE / (str(uuid.uuid4()) + ".gate")
        script = r'''
import json, pathlib, sys, time
import requests
from atlas_x402.client import GuardedClient, Policy, Refused
dest, amount = "nano_synthetic_fixture_destination", 10**27
quote = {"x402Version":2,"accepts":[{"scheme":"exact","network":"nano:mainnet","asset":"XNO","payTo":dest,"amount":str(amount),"maxTimeoutSeconds":60}]}
class Transport:
    def post(self, url, body, payment_header=None):
        r = requests.Response()
        r.status_code = 200 if payment_header else 402
        r._content = b'{}' if payment_header else json.dumps(quote).encode()
        return r
class Signer:
    def sign(self, destination, amount): return {"fixture":True}, "b"*64
path, gate, number = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
gate.with_name(gate.name + "." + number + ".ready").write_text("ready")
for _ in range(4500):
    if gate.exists(): break
    time.sleep(.01)
else: raise RuntimeError("test gate timed out")
client = GuardedClient(policy=Policy("https://seller.example/", frozenset({dest}), amount, amount), journal=path, signer=Signer(), transport=Transport())
try:
    client.invoke("text" + number, "id" + number)
    print("ok")
except Refused:
    print("refused")
'''
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.path), str(gate), str(i)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(2)]
        for _ in range(3000):
            if all(gate.with_name(gate.name + "." + str(i) + ".ready").exists() for i in range(2)):
                break
            time.sleep(.01)
        else:
            self.fail("child processes did not reach test gate")
        gate.write_text("go")
        results = [p.communicate(timeout=45) for p in procs]
        self.assertTrue(all(p.returncode == 0 for p in procs), results)
        self.assertCountEqual([out.strip() for out, _ in results], ["ok", "refused"])


class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append((self.path, body, self.headers.get("PAYMENT-SIGNATURE")))
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/leaked"); self.end_headers(); return
        if self.headers.get("PAYMENT-SIGNATURE"):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"local":true}'); return
        data = json.dumps(QUOTE).encode()
        self.send_response(402)
        self.send_header("Content-Type", "application/json")
        self.send_header("PAYMENT-REQUIRED", base64.b64encode(data).decode())
        self.end_headers(); self.wfile.write(data)


class RealLocalTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.path = STATE / (str(uuid.uuid4()) + ".jsonl")
        initialize_journal(self.path)
        self.signer = FakeSigner()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def client(self, path):
        endpoint = "http://127.0.0.1:%s%s" % (self.server.server_port, path)
        policy = Policy(endpoint, frozenset({DEST}), RAW, RAW, allow_test_loopback_http=True)
        return GuardedClient(policy=policy, journal=self.path, signer=self.signer,
                             transport=RequestsTransport())

    def test_real_http_quote_and_single_synthetic_payment_header(self):
        result = self.client("/hash").invoke("hello", "local-1")
        self.assertEqual(result["status"], 200)
        self.assertEqual(len(self.server.requests), 2)
        self.assertIsNone(self.server.requests[0][2])
        self.assertIsNotNone(self.server.requests[1][2])
        self.assertEqual(self.signer.calls, 1)

    def test_real_http_redirect_is_not_followed(self):
        with self.assertRaises(Refused): self.client("/redirect").invoke("hello", "local-2")
        self.assertEqual([r[0] for r in self.server.requests], ["/redirect"])
        self.assertEqual(self.signer.calls, 0)

    def test_real_langchain_agent_loop_with_local_http_and_fake_model(self):
        from atlas_x402.tool import make_paid_text_tool
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage
        class ScriptedChat(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs): return self
        model = ScriptedChat(responses=[
            AIMessage(content="", tool_calls=[{"name": "paid_text_request", "args": {"text": "hello", "request_id": "loop"}, "id": "call-1", "type": "tool_call"}]),
            AIMessage(content="Done using a synthetic fixture.")])
        agent = create_agent(model=model, tools=[make_paid_text_tool(self.client("/hash"))])
        result = agent.invoke({"messages": [{"role": "user", "content": "Use the tool."}]})
        self.assertTrue(any(getattr(m, "type", "") == "tool" for m in result["messages"]))
        self.assertEqual((self.signer.calls, len(self.server.requests)), (1, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
