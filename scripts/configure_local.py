"""Create local credentials without printing secrets or overwriting configuration."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
target = root / ".env"
content = (root / ".env.example").read_text().replace("replace-with-a-private-password", secrets.token_urlsafe(24))
try:
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env already exists; existing configuration preserved.")
with os.fdopen(descriptor, "w") as output:
    output.write(content)
print("Created private .env. Open it locally to view your administrator credentials.")
