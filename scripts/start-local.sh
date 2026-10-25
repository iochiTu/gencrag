#!/usr/bin/env bash
# Restart the configured local environment without changing its catalog or accounts.
set -euo pipefail
cd "$(dirname "$0")/.."
test -x .venv/bin/python || { echo "Create .venv and install requirements.txt first."; exit 1; }
test -f .env || { echo "Copy .env.example to .env and configure it first."; exit 1; }
mkdir -p .local/logs
docker compose up -d
for attempt in {1..30}; do
  if docker compose exec -T db pg_isready -U rag -d kbeauty >/dev/null 2>&1; then break; fi
  sleep 1
done
docker compose exec -T db pg_isready -U rag -d kbeauty >/dev/null
if ! curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  if test -x .local/ollama/ollama; then
    ollama_bin="$PWD/.local/ollama/ollama"
  else
    ollama_bin="$(command -v ollama)" || { echo "Install Ollama first."; exit 1; }
  fi
  OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS="$PWD/.local/models" \
    nohup "$ollama_bin" serve >.local/logs/ollama.log 2>&1 &
  echo $! >.local/ollama.pid
  for attempt in {1..30}; do
    if curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then break; fi
    sleep 1
  done
fi
curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null
if curl -fsS --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "A chatbot is already running at http://localhost:8000"
  exit 0
fi
echo "Chat: http://localhost:8000 | Admin: http://localhost:8000/admin"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
