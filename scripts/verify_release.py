"""Verify the exact, locally checked distribution before publication."""
from pathlib import Path
import argparse
import base64
import csv
import email
import hashlib
import io
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WHEEL_SHA256 = "42fe7d1d49a95e9112b92ef12a65bdcbec60009f284d3fe824deb00c30063902"
BASELINE = {
    "__init__.py": "2af3a0c9ffa41a24b53fb92fc47285983929d0a7a57354625d9c9482bbb3c7bc",
    "client.py": "3cbaa862b80c71a92bdaf839b89d5fc4ec58d81ea9c7644e0d87027a82a8cdba",
    "door.py": "6b30657af4662c16ee79cf788a891342ab37b393f7307e93e8ee7d52e515dbac",
    "tool.py": "5e87b0b5a4b63388e82ba7aaffb57f71a46cd20363aff0038252db368a3c4fac",
    "example.py": "108c6dbf79ebfa21d9dcaa24bffc3f301e9d8923ffa8eeaf9c0d1cbe831bc0e8",
}

def normal(path):
    return path.read_bytes().replace(b"\r\n", b"\n")

def verify(wheel, root):
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != WHEEL_SHA256:
        raise ValueError("Distribution SHA-256 does not match the reviewed wheel")
    dist = "atlas_langchain_nano_x402-0.1.0.dist-info/"
    expected = {"atlas_x402/" + n for n in BASELINE}
    expected |= {dist + n for n in ("licenses/LICENSE", "METADATA", "WHEEL", "top_level.txt", "RECORD")}
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or set(names) != expected:
            raise ValueError("Distribution contents differ from the allowlist")
        for name, digest in BASELINE.items():
            source = normal(root / "src" / "atlas_x402" / name)
            if hashlib.sha256(source).hexdigest() != digest or z.read("atlas_x402/" + name) != source:
                raise ValueError("Code differs from buyer-reviewed baseline: " + name)
        if normal(root / "examples" / "live_agent.py") != z.read("atlas_x402/example.py"):
            raise ValueError("Installed example differs from buyer-reviewed example")
        if normal(root / "LICENSE") != z.read(dist + "licenses/LICENSE"):
            raise ValueError("MIT license differs")
        metadata = z.read(dist + "METADATA")
        message = email.message_from_bytes(metadata)
        if (message["Name"] != "atlas-langchain-nano-x402" or message["Version"] != "0.1.0"
                or message["License-Expression"] != "MIT" or message["Requires-Python"] != ">=3.11"):
            raise ValueError("Project metadata differs")
        requirements = set(message.get_all("Requires-Dist", []))
        if requirements != {"langchain==1.4.7", "feeless402==0.2.13", "nanopy==28.0.1.post26", "requests==2.34.2"}:
            raise ValueError("Dependency metadata differs")
        if metadata.replace(b"\r\n", b"\n").partition(b"\n\n")[2] != normal(root / "README.md"):
            raise ValueError("Package description differs from README")
        rows = list(csv.reader(io.StringIO(z.read(dist + "RECORD").decode())))
        if {r[0] for r in rows} != set(names) or len(rows) != len(names):
            raise ValueError("RECORD does not match distribution contents")
        for name, recorded, size in rows:
            payload = z.read(name)
            if recorded:
                actual = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
                if actual != recorded or int(size) != len(payload):
                    raise ValueError("RECORD digest or size differs: " + name)
    print("Validated release wheel SHA-256 " + WHEEL_SHA256)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, default=ROOT / "release" / "v0.1.0" / "atlas_langchain_nano_x402-0.1.0-py3-none-any.whl")
    parser.add_argument("--source", type=Path, default=ROOT)
    args = parser.parse_args()
    verify(args.wheel, args.source)