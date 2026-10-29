"""A conservative x402 client. Its signing and journal boundary is explicit.

This candidate has not been used with real funds. Review its documented limits
before doing so. Journal reservations are never removed or automatically retried.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

import requests
from nano_pay.x402 import build_payment_header, collect_offers, parse_quote


class Refused(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    endpoint: str
    pay_to: frozenset[str]
    per_call_raw: int
    cumulative_raw: int
    # Only for the loopback fixture; never use this in an actual paid run.
    allow_test_loopback_http: bool = False

    def validate(self):
        p = urlsplit(self.endpoint)
        try:
            _ = p.port
        except ValueError as exc:
            raise Refused("invalid endpoint port") from exc
        local_test = (self.allow_test_loopback_http and p.scheme == "http" and
                      p.hostname == "127.0.0.1")
        if (p.scheme != "https" and not local_test) or not p.hostname:
            raise Refused("endpoint must use HTTPS")
        if p.username is not None or p.password is not None or p.fragment:
            raise Refused("endpoint has userinfo or fragment")
        if not self.pay_to or any(not isinstance(x, str) or not x for x in self.pay_to):
            raise Refused("an explicit destination allowlist is required")
        if (type(self.per_call_raw) is not int or type(self.cumulative_raw) is not int or
                self.per_call_raw <= 0 or self.cumulative_raw <= 0 or
                self.per_call_raw > self.cumulative_raw):
            raise Refused("explicit positive integer spending limits required")


class RequestsTransport:
    """No redirects, retries, ambient proxies, cookies, or credentials."""
    def __init__(self, timeout=15, max_response_bytes=1_048_576):
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def post(self, endpoint, body, payment_header=None):
        headers = {"Accept": "application/json", "Content-Type": "text/plain; charset=utf-8"}
        if payment_header is not None:
            headers["PAYMENT-SIGNATURE"] = payment_header
            headers["X-PAYMENT"] = payment_header
        with requests.Session() as session:
            session.trust_env = False
            with session.post(endpoint, data=body, headers=headers, timeout=self.timeout,
                              allow_redirects=False, stream=True) as response:
                data = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    data.extend(chunk)
                    if len(data) > self.max_response_bytes:
                        raise Refused("response exceeds configured size")
                # parse_quote expects a requests.Response, including .json().
                response._content = bytes(data)
                response._content_consumed = True
                return response


class NanoPaySigner:
    """Adapter for an explicitly supplied, already loaded nano_pay wallet/RPC.

    The caller is responsible for wallet custody and RPC trust. This adapter does
    not discover, create, load, print, or fund any wallet. The SDK builds valid
    work even when the merchant permits workOptional, rather than using a
    workless optimization. Neither path has been tested with real funds here.
    """
    def __init__(self, wallet, rpc):
        self.wallet = wallet
        self.rpc = rpc

    def sign(self, destination, amount_raw):
        block, block_hash, _work_root = self.wallet.build_payment_block(
            self.rpc, destination, amount_raw)
        return block, block_hash


_locks_guard = threading.Lock()
_thread_locks = {}


@contextmanager
def _locked_journal(path):
    """Serialize across local threads and processes; do not delete the file."""
    key = os.path.normcase(str(path.resolve()))
    with _locks_guard:
        lock = _thread_locks.setdefault(key, threading.RLock())
    with lock:
        # Journal must be pre-created deliberately, and is never recreated here.
        with path.open("r+b") as f:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield f
            finally:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def initialize_journal(path):
    """Initialize a new explicit journal once; never replace an existing file."""
    path = Path(path)
    with path.open("xb") as f:
        f.write(b'{"format":"atlas-x402-journal-v1"}\n')
        f.flush()
        os.fsync(f.fileno())


def _append(f, obj):
    f.seek(0, os.SEEK_END)
    f.write((json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n").encode())
    f.flush()
    os.fsync(f.fileno())


def _read_journal(f):
    f.seek(0)
    raw = f.read()
    if not raw.endswith(b"\n"):
        raise Refused("journal is incomplete")
    try:
        rows = [json.loads(line) for line in raw.splitlines()]
    except (ValueError, UnicodeDecodeError) as exc:
        raise Refused("journal is corrupt") from exc
    if not rows or rows[0] != {"format": "atlas-x402-journal-v1"}:
        raise Refused("journal header is invalid")
    reserved, signed, finished = {}, set(), set()
    for row in rows[1:]:
        if not isinstance(row, dict):
            raise Refused("journal row is invalid")
        if row.get("event") == "reserved":
            rid, fp, amount = row.get("request_id_hash"), row.get("fingerprint"), row.get("amount_raw")
            if (not isinstance(rid, str) or len(rid) != 64 or not isinstance(fp, str) or len(fp) != 64
                    or type(amount) is not int or amount <= 0 or rid in reserved
                    or any(r["fingerprint"] == fp for r in reserved.values())):
                raise Refused("journal reservation is invalid")
            reserved[rid] = row
        elif row.get("event") == "signed":
            rid, block_hash = row.get("request_id_hash"), row.get("block_hash")
            if (rid not in reserved or rid in signed or not isinstance(block_hash, str)
                    or len(block_hash) != 64 or any(c not in "0123456789abcdefABCDEF" for c in block_hash)):
                raise Refused("journal signature record is invalid")
            signed.add(rid)
        elif row.get("event") == "response_received":
            rid = row.get("request_id_hash")
            if rid not in signed or rid in finished or type(row.get("status")) is not int:
                raise Refused("journal outcome is invalid")
            finished.add(rid)
        else:
            raise Refused("unknown journal event")
    return reserved, finished


class GuardedClient:
    def __init__(self, *, policy, journal, signer, transport=None, clock=time.monotonic):
        policy.validate()
        self.policy = policy
        self.journal = Path(journal)
        self.signer = signer
        self.transport = transport or RequestsTransport()
        self.clock = clock

    def invoke(self, text, request_id):
        if not isinstance(text, str) or len(text.encode("utf-8")) > 65536:
            raise Refused("text must be UTF-8 and at most 64 KiB")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise Refused("a nonempty bounded request id is required")
        body = text.encode("utf-8")
        request_id_hash = hashlib.sha256(request_id.encode()).hexdigest()
        fingerprint = hashlib.sha256(b"POST\0" + self.policy.endpoint.encode() + b"\0" + body).hexdigest()
        try:
            with _locked_journal(self.journal) as f:
                reserved, finished = _read_journal(f)
                if request_id_hash in reserved or any(x["fingerprint"] == fingerprint for x in reserved.values()):
                    raise Refused("request was previously reserved")
                if set(reserved) - finished:
                    raise Refused("an earlier payment has an unknown outcome; reconcile it first")
                # A slow quote may be fetched once more, but no signed block is retried.
                for attempt in range(2):
                    started = self.clock()
                    response = self.transport.post(self.policy.endpoint, body)
                    if response.status_code != 402:
                        raise Refused("expected a direct 402 quote; redirects are refused")
                    try:
                        quote = parse_quote(response)
                        offers = collect_offers(quote)
                    except Exception as exc:
                        raise Refused("invalid payment quote") from exc
                    if not isinstance(quote, dict) or quote.get("x402Version") != 2:
                        raise Refused("only x402 v2 is supported")
                    if not isinstance(offers, list) or len(offers) != 1 or not isinstance(offers[0], dict):
                        raise Refused("offer is ambiguous")
                    offer = offers[0]
                    if (offer.get("scheme"), offer.get("network"), offer.get("asset")) != ("exact", "nano:mainnet", "XNO"):
                        raise Refused("unapproved scheme, network, or asset")
                    if "paymentId" in offer:
                        raise Refused("unsupported legacy paymentId dialect")
                    destination, amount, ttl = offer.get("payTo"), offer.get("amount"), offer.get("maxTimeoutSeconds")
                    if not isinstance(destination, str) or destination not in self.policy.pay_to:
                        raise Refused("destination is not allowlisted")
                    extra = offer.get("extra", {})
                    if not isinstance(extra, dict) or set(extra) - {"work", "workOptional", "workThreshold", "session"}:
                        raise Refused("unsupported offer extension")
                    if "workOptional" in extra and type(extra["workOptional"]) is not bool:
                        raise Refused("invalid workOptional")
                    if "work" in extra and extra["work"] not in ("optional", "required"):
                        raise Refused("invalid work policy")
                    if ("work" in extra and "workOptional" in extra and
                            (extra["work"] == "optional") != extra["workOptional"]):
                        raise Refused("contradictory work policy")
                    if "workThreshold" in extra and (not isinstance(extra["workThreshold"], str) or
                            len(extra["workThreshold"]) != 16 or any(c not in "0123456789abcdefABCDEF" for c in extra["workThreshold"])):
                        raise Refused("invalid work threshold")
                    if "workThreshold" in extra and int(extra["workThreshold"], 16) > int("fffffff800000000", 16):
                        raise Refused("work threshold is above the supported Nano send threshold")
                    if "session" in extra and (not isinstance(extra["session"], str) or len(extra["session"]) > 256):
                        raise Refused("invalid session")
                    if not isinstance(amount, str) or not amount.isascii() or not amount.isdecimal() or len(amount) > 40:
                        raise Refused("invalid raw amount")
                    amount = int(amount)
                    if not 0 < amount < (1 << 128) or type(ttl) is not int or not 0 < ttl <= 3600:
                        raise Refused("invalid amount or quote lifetime")
                    total = sum(row["amount_raw"] for row in reserved.values())
                    if amount > self.policy.per_call_raw or amount + total > self.policy.cumulative_raw:
                        raise Refused("payment cap exceeded")
                    if self.clock() - started < ttl:
                        break
                    if attempt == 1:
                        raise Refused("quote expired")
                if self.clock() - started >= ttl:
                    raise Refused("quote expired before reservation")
                _append(f, {"event": "reserved", "request_id_hash": request_id_hash,
                            "fingerprint": fingerprint, "amount_raw": amount})
                # Even if signing crashes, the reservation stays and blocks another signature.
                if self.clock() - started >= ttl:
                    raise Refused("quote expired before signing; reservation retained")
                block, block_hash = self.signer.sign(destination, amount)
                if (not isinstance(block, dict) or not isinstance(block_hash, str) or len(block_hash) != 64
                        or any(c not in "0123456789abcdefABCDEF" for c in block_hash)):
                    raise Refused("signer returned an invalid result; reservation retained")
                _append(f, {"event": "signed", "request_id_hash": request_id_hash,
                            "block_hash": block_hash})
                if self.clock() - started >= ttl:
                    raise Refused("quote expired after signing; reservation retained")
                header = build_payment_header(quote, offer, block, destination)
                if self.clock() - started >= ttl:
                    raise Refused("quote expired before presentation; reservation retained")
                paid_response = self.transport.post(self.policy.endpoint, body, header)
                # A non-2xx or exception can mean money moved: leave it unresolved.
                if not 200 <= paid_response.status_code < 300:
                    raise Refused("merchant response is non-success; reservation retained, no retry")
                _append(f, {"event": "response_received", "request_id_hash": request_id_hash,
                            "status": paid_response.status_code})
                return {"status": paid_response.status_code, "block_hash": block_hash,
                        "body": paid_response.text, "settlement": "unverified"}
        except FileNotFoundError as exc:
            raise Refused("explicit pre-created journal is missing") from exc
