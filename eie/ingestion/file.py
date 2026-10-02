"""Reads emails from a local JSON file - for demos and testing without a mailbox."""
import json
from pathlib import Path

from ..models import Email


class JsonFileSource:
    name = "file"

    def __init__(self, path: str):
        self.path = Path(path)

    def fetch_unread(self, limit: int) -> list[Email]:
        items = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            Email(
                message_id=item["message_id"],
                subject=item.get("subject", ""),
                sender=item.get("sender", ""),
                received_at=item.get("received_at", ""),
                body=item.get("body", ""),
                source=self.name,
                source_ref=item["message_id"],
                conversation_id=item.get("conversation_id", ""),
            )
            for item in items[:limit]
        ]

    def mark_processed(self, email: Email) -> None:
        pass
