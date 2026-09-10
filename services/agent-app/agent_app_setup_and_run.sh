#!/bin/bash
# services/agent-app/agent_app_setup_and_run.sh
# Sets up agent-app's own venv, ALSO sets up mcp-server's own venv if
# missing (separate venvs), points MCP_SERVER_PYTHON/MCP_SERVER_SCRIPT
# at it so mcp_client.py can spawn it as a stdio subprocess, checks
# Postgres and Redis connectivity, then runs the Streamlit app.
#
# Note on JupyterHub: --server.baseUrlPath is deliberately NOT passed.
# jupyter-server-proxy's generic /proxy/<port>/ handler already strips
# the path prefix before forwarding to this app, so Streamlit must be
# left serving at its normal root path — setting baseUrlPath here would
# double-prefix requests and break with a "Not Found" error.
set -e

SERVICE_NAME="agent-app"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$BASE_DIR/venvs/${SERVICE_NAME}"
MCP_SERVER_DIR="$BASE_DIR/services/mcp-server"

LOGS_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOGS_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="$LOGS_DIR/run_${TIMESTAMP}.log"

PORT=8501
while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --port PORT   Streamlit port (default 8501)"
            echo ""
            echo "This script also sets up services/mcp-server's own venv if not"
            echo "already set up, so agent-app can spawn it as a stdio subprocess."
            exit 0
            ;;
        *) echo "Unknown option: $1. Use --help."; exit 1 ;;
    esac
done

echo "============================================================================" | tee "$LOG_FILE"
echo "  Agent App (Streamlit) Setup & Run"                                          | tee -a "$LOG_FILE"
echo "  Port: $PORT"                                                                | tee -a "$LOG_FILE"
echo "  Log: $LOG_FILE"                                                             | tee -a "$LOG_FILE"
echo "============================================================================" | tee -a "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

# Detect JupyterHub, purely for printing the right access URL — no
# baseUrlPath is passed to Streamlit itself (see note above).
JUPYTERHUB_PROXY_PATH=""
if [ -n "$JUPYTERHUB_SERVICE_PREFIX" ]; then
    JUPYTERHUB_PROXY_PATH="${JUPYTERHUB_SERVICE_PREFIX}proxy/${PORT}/"
fi

echo ""
echo "Step 1: Setting up Python environment..."
echo "----------------------------------------------"
if command -v python3.11 &> /dev/null; then
    PYTHON_BIN="python3.11"
    echo "Found python3.11"
elif command -v python3 &> /dev/null; then
    PYTHON_BIN="python3"
    echo "Found python3 ($(python3 --version 2>&1 | awk '{print $2}'))"
else
    echo "ERROR: Python not found!"; exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at: $VENV_DIR"
    $PYTHON_BIN -m venv "$VENV_DIR"
    [ ! -f "$VENV_DIR/bin/activate" ] && echo "ERROR: venv creation failed!" && exit 1
    echo "Virtual environment created"
else
    echo "Virtual environment already exists"
fi
source "$VENV_DIR/bin/activate"
echo "Activated ($(python --version))"

echo ""
echo "Step 2: Installing dependencies..."
echo "----------------------------------------------"
export PIP_BREAK_SYSTEM_PACKAGES=1
pip install --upgrade pip -q
pip install --no-cache-dir -r "$SCRIPT_DIR/requirements.txt" -q
echo "Dependencies installed"

echo ""
echo "Step 3: Ensuring mcp-server's own venv is set up (needed for tool calls)..."
echo "----------------------------------------------"
MCP_VENV_DIR="$BASE_DIR/venvs/mcp-server"
if [ ! -d "$MCP_VENV_DIR" ]; then
    $PYTHON_BIN -m venv "$MCP_VENV_DIR"
    "$MCP_VENV_DIR/bin/pip" install --upgrade pip -q
    "$MCP_VENV_DIR/bin/pip" install --no-cache-dir -r "$MCP_SERVER_DIR/requirements.txt" -q
fi
# IMPORTANT: use the venv's own bin/python path directly. Do NOT resolve
# it with `realpath` — venv python binaries are often symlinks to the
# base interpreter, and resolving the symlink loses venv site-packages
# isolation entirely (the spawned process would then be missing every
# package installed only inside this venv, e.g. redis).
export MCP_TRANSPORT="stdio"
export MCP_SERVER_PYTHON="$MCP_VENV_DIR/bin/python"
export MCP_SERVER_SCRIPT="$MCP_SERVER_DIR/mcp_server.py"
echo "mcp-server venv ready: $MCP_SERVER_PYTHON"

echo ""
echo "Step 4: Loading environment variables..."
echo "----------------------------------------------"
if [ -f "$BASE_DIR/.env" ]; then
    sed -i 's/\r$//' "$BASE_DIR/.env" 2>/dev/null || true
    while IFS= read -r line; do
        [[ $line =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue
        if [[ $line =~ ^([^=]+)=(.*)$ ]]; then
            key=$(echo "${BASH_REMATCH[1]}" | xargs)
            value=$(echo "${BASH_REMATCH[2]}" | sed 's/[[:space:]]*#.*$//' | xargs | sed "s/^['\"]//;s/['\"]$//")
            export "$key=$value"
        fi
    done < "$BASE_DIR/.env"
    echo "Environment variables loaded"
else
    echo "WARNING: .env not found at $BASE_DIR/.env — copy example.env to .env first"
fi

echo ""
echo "Step 5: Checking Postgres connectivity..."
echo "----------------------------------------------"
if [ -z "$DATABASE_URL" ]; then
    echo "ERROR: DATABASE_URL not set — copy example.env to .env and fill it in."
    exit 1
fi
if command -v pg_isready &> /dev/null; then
    if pg_isready -d "$DATABASE_URL" &> /dev/null; then
        echo "Postgres reachable"
    else
        echo "ERROR: Postgres not reachable at DATABASE_URL"
        echo "   Start it natively, or with 'docker compose up postgres'."
        exit 1
    fi
else
    echo "WARNING: pg_isready not found — skipping connectivity check, will fail at runtime if Postgres is down"
fi

echo ""
echo "Step 6: Checking Redis connectivity..."
echo "----------------------------------------------"
REDIS_CHECK_URL="${REDIS_URL:-redis://localhost:6379/1}"
if command -v redis-cli &> /dev/null; then
    if redis-cli -u "$REDIS_CHECK_URL" ping &> /dev/null; then
        echo "Redis reachable at $REDIS_CHECK_URL"
    else
        echo "ERROR: Redis not reachable at $REDIS_CHECK_URL"
        exit 1
    fi
else
    echo "WARNING: redis-cli not found — skipping connectivity check"
fi

echo ""
echo "Step 7: Verifying core files..."
echo "----------------------------------------------"
REQUIRED_FILES=("app.py" "agent_graph.py" "mcp_client.py" "date_utils.py" "db_utils.py" "tracing.py" "grounding.py")
ALL_EXIST=true
for file in "${REQUIRED_FILES[@]}"; do
    if [ -f "$SCRIPT_DIR/$file" ]; then echo "OK: $file"
    else echo "MISSING: $file"; ALL_EXIST=false; fi
done
[ "$ALL_EXIST" = false ] && echo "ERROR: Required files missing!" && exit 1

echo ""
echo "============================================================================"
echo "  Starting Agent App (Streamlit)"
echo "============================================================================"
CMD="streamlit run $SCRIPT_DIR/app.py"
CMD="$CMD --server.port $PORT"
CMD="$CMD --server.address 0.0.0.0"
CMD="$CMD --server.enableCORS false"
CMD="$CMD --server.enableXsrfProtection false"
# Deliberately no --server.baseUrlPath — see note at top of this file.

echo "Running: $CMD"
if [ -n "$JUPYTERHUB_PROXY_PATH" ]; then
    echo "Access via your JupyterHub proxy at: https://<your-jupyterhub-domain>${JUPYTERHUB_PROXY_PATH}"
else
    echo "Not on JupyterHub? Tunnel it: ssh -L ${PORT}:localhost:${PORT} user@host"
fi

$CMD
EXIT_CODE=$?

echo ""
echo "============================================================================"
echo "  Complete | Exit code: $EXIT_CODE | Log: $LOG_FILE"
echo "============================================================================"
exit $EXIT_CODE
