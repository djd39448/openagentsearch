import hashlib
import json
import pathlib


class RawStore:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.raw_dir = root / "raw"
        self.provenance_file = root / "raw" / "provenance.jsonl"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def put(self, url: str, body: bytes, status: int, robots_allowed: bool, fetched_at: float) -> str:
        sha256 = hashlib.sha256(body).hexdigest()
        filepath = self.raw_dir / f"{sha256}.html"

        # Write the body to a file if it doesn't already exist
        if not filepath.exists():
            with open(filepath, "wb") as f:
                f.write(body)

        # Append provenance data
        provenance_data = {
            "url": url,
            "fetched_at": fetched_at,
            "status": status,
            "sha256": sha256,
            "robots_allowed": robots_allowed
        }

        with open(self.provenance_file, "a") as f:
            f.write(json.dumps(provenance_data) + "\n")

        return sha256