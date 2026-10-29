"""Account state and free-work adapter for the configured Pursekeeper door."""
import requests

from .client import Refused


class PursekeeperRPC:
    """Explicitly trusted account-state source, not an independent ledger proof.

    This narrow adapter only talks to the same HTTPS door used by the example.
    Never use it with an account containing more than one can afford to lose
    without independently verifying the reported frontier and balance.
    """
    origin = "https://pursekeeper.dev"

    def _request(self, method, path, **kwargs):
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, self.origin + path, timeout=15,
                                       allow_redirects=False, **kwargs)
            if response.status_code != 200 or len(response.content) > 1_048_576:
                raise Refused("account information or free work unavailable")
            try:
                value = response.json()
            except ValueError as exc:
                raise Refused("invalid account-information response") from exc
            if not isinstance(value, dict):
                raise Refused("invalid account-information response")
            return value

    def account_info(self, address):
        value = self._request("GET", "/v1/account_info", params={"account": address})
        if value.get("found") is False:
            return None
        if value.get("found") is not True:
            raise Refused("account state is indeterminate")
        frontier, balance, representative = (value.get("frontier"), value.get("balance_raw"),
                                             value.get("representative"))
        if (not isinstance(frontier, str) or len(frontier) != 64 or
                any(c not in "0123456789abcdefABCDEF" for c in frontier) or
                not isinstance(balance, str) or not balance.isascii() or not balance.isdecimal() or
                int(balance) >= (1 << 128) or not isinstance(representative, str)):
            raise Refused("invalid account state")
        return {"frontier": frontier, "balance": balance, "representative": representative}

    def work_generate(self, root, difficulty):
        # A non-200, including 402, is a refusal, never a paid work retry.
        value = self._request("POST", "/v1/work", json={"hash": root})
        work = value.get("work")
        if not isinstance(work, str) or len(work) != 16:
            raise Refused("free work unavailable")
        return work  # nano_pay validates the work against root and difficulty.
