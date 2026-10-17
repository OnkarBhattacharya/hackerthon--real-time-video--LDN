"""Keep unit tests off the live archive and off real webhooks."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["VSS_USE_FIXTURE"] = "1"
os.environ.pop("WANDB_API_KEY", None)
os.environ.pop("TICKET_WEBHOOK_URL", None)
os.environ["SNAPSHOT_PATH"] = str(Path(tempfile.gettempdir()) / "hackerthon-test-last-run.json")
