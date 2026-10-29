"""Framework-native tool bound to an operator-configured guarded client."""
import json
from langchain.tools import tool


def make_paid_text_tool(client):
    @tool
    def paid_text_request(text: str, request_id: str) -> str:
        """Send text to the configured x402 service, subject to operator payment limits."""
        return json.dumps(client.invoke(text, request_id), sort_keys=True)
    return paid_text_request
