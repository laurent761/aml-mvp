"""Create credentials for a separate, loopback-only acceptance deployment."""
import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("var/research-smoke.env"))
    parser.add_argument("--cached", action="store_true", help="Use the documented validation image tags")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    env = {
        "RESEARCH_SMOKE_DB_PASSWORD": secrets.token_hex(24),
        "RESEARCH_SMOKE_SUPERVISOR_TOKEN": secrets.token_urlsafe(32),
        "RESEARCH_SMOKE_SIGNING_KEY": secrets.token_urlsafe(32),
        "RESEARCH_SMOKE_AUTH_TOKENS": json.dumps({hashlib.sha256(token.encode()).hexdigest(): {
            "owner_id": "acceptance", "scopes": ["operator", "research", "evaluation", "evidence"]}}, separators=(",", ":")),
    }
    if args.cached:
        env.update({f"RESEARCH_SMOKE_{role}_IMAGE": "aml-research-backend:validation" for role in ("API", "WORKER", "SUPERVISOR")})
        env.update(RESEARCH_SMOKE_BLUE_IMAGE="aml-research-blue:validation", RESEARCH_SMOKE_UI_IMAGE="aml-research-ui:validation")
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write("\n".join(f"{key}='{value}'" for key, value in env.items()) + "\n")
    client_path = args.output.with_suffix(".client.json")
    fd = os.open(client_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump({"api_url": "http://127.0.0.1:18000", "ui_url": "http://127.0.0.1:13000", "token": token}, output)
    print(f"Compose configuration: {args.output}\nSDK connection configuration: {client_path}")


if __name__ == "__main__":
    main()
