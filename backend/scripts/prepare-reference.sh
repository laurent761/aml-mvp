#!/usr/bin/env bash
# Build, preserve, validate and register a new immutable reference target version.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d --wait target-registry
bundle=var/bundles/finance-reference.json
uv run adversarial-bundle build-reference --output "$bundle" "$@"
docker compose exec -T api sh -c 'cat > /app/var/uploads/finance-reference.json' < "$bundle"
docker compose exec -T api python -m adversarial_agent_mvp.bundle_cli validate /app/var/uploads/finance-reference.json
docker compose exec -T api python -m adversarial_agent_mvp.bundle_cli register --verify-image /app/var/uploads/finance-reference.json
printf '\nTarget preserved and registered. Refresh AML and select this version for a new campaign. Existing campaigns retain their original version.\n'
