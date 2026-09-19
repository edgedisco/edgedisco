# Deploy on macOS

This guide runs both the server and collector on one Mac for evaluation. A production rollout should host the server centrally behind HTTPS and install only the collector on endpoints.

## Install from a clone

```bash
git clone https://github.com/nsabharwal/edgedisco.git
cd edgedisco
python3 -m venv "$HOME/AIInventory/venv"
"$HOME/AIInventory/venv/bin/python" -m pip install .
mkdir -p "$HOME/AIInventory/data" "$HOME/AIInventory/config" "$HOME/AIInventory/logs"
```

Verify the installed version:

```bash
"$HOME/AIInventory/venv/bin/python" -c "import ai_asset_inventory; print(ai_asset_inventory.__version__)"
```

## Start the local server

```bash
ADMIN_TOKEN=$(openssl rand -hex 32)
ENROLL_TOKEN=$(openssl rand -hex 32)
umask 077
printf 'export AAI_ADMIN_TOKEN=%s\nexport AAI_ENROLLMENT_TOKEN=%s\n' \
  "$ADMIN_TOKEN" "$ENROLL_TOKEN" > "$HOME/AIInventory/server.env"
chmod 600 "$HOME/AIInventory/server.env"

source "$HOME/AIInventory/server.env"
nohup env AAI_ADMIN_TOKEN="$AAI_ADMIN_TOKEN" AAI_ENROLLMENT_TOKEN="$AAI_ENROLLMENT_TOKEN" \
  "$HOME/AIInventory/venv/bin/ai-inventory" server \
  --host 127.0.0.1 --port 8080 --db "$HOME/AIInventory/data/inventory.db" \
  > "$HOME/AIInventory/logs/server.log" 2>&1 &
echo $! > "$HOME/AIInventory/server.pid"
```

Verify the service:

```bash
curl http://127.0.0.1:8080/healthz
```

## Configure the collector

```bash
source "$HOME/AIInventory/server.env"
cat > "$HOME/AIInventory/config/agent.json" <<EOF
{
  "server_url": "http://127.0.0.1:8080",
  "enrollment_token": "$AAI_ENROLLMENT_TOKEN",
  "scan_interval_seconds": 300
}
EOF
chmod 600 "$HOME/AIInventory/config/agent.json"
```

Preview, enroll, and start:

```bash
"$HOME/AIInventory/venv/bin/ai-inventory" agent scan \
  --config "$HOME/AIInventory/config/agent.json"

"$HOME/AIInventory/venv/bin/ai-inventory" agent enroll \
  --config "$HOME/AIInventory/config/agent.json"

nohup "$HOME/AIInventory/venv/bin/ai-inventory" agent run \
  --config "$HOME/AIInventory/config/agent.json" \
  > "$HOME/AIInventory/logs/agent.log" 2>&1 &
echo $! > "$HOME/AIInventory/agent.pid"
```

Open `http://127.0.0.1:8080` and sign in with the administrator token:

```bash
source "$HOME/AIInventory/server.env"
echo "$AAI_ADMIN_TOKEN"
```

## Logs and lifecycle

```bash
tail -f "$HOME/AIInventory/logs/server.log"
tail -f "$HOME/AIInventory/logs/agent.log"
```

Stop the evaluation processes:

```bash
kill "$(cat "$HOME/AIInventory/agent.pid")"
kill "$(cat "$HOME/AIInventory/server.pid")"
```

For managed endpoints, adapt `deploy/com.trust3.ai-inventory.plist`, install it through MDM, and protect both the configuration and logs with the intended service identity.
