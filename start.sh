#!/usr/bin/env bash
# Start CodeLM in browser mode (no Electron needed)
# Usage: ./start.sh /path/to/your/project

set -e

ROOT="${1:-$(pwd)}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "CodeLM — starting..."
echo "Project root: $ROOT"

cd "$SCRIPT_DIR/backend"

# Build frontend if static dir is missing or stale
if [ ! -f "static/index.html" ]; then
  echo "Building frontend..."
  cd "$SCRIPT_DIR/frontend"
  npm install --silent
  npm run build
  cd "$SCRIPT_DIR/backend"
fi

# Start backend
python main.py &
BACKEND_PID=$!

# Wait for backend
echo "Waiting for backend..."
for i in $(seq 1 20); do
  curl -sf http://localhost:8765/health > /dev/null 2>&1 && break
  sleep 0.5
done

# Open browser
URL="http://localhost:8765?root=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$ROOT'))")"
echo "Opening: $URL"

if command -v xdg-open &>/dev/null; then
  xdg-open "$URL"
elif command -v open &>/dev/null; then
  open "$URL"
else
  echo "Open your browser at: $URL"
fi

wait $BACKEND_PID
