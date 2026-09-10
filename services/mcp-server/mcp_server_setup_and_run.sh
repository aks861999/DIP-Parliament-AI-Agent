#!/bin/bash
# services/mcp-server/mcp_server_setup_and_run.sh
# Sets up services/mcp-server's own venv (python3 -m venv, no uv), loads
# .env from the repo root, checks Redis connectivity, and runs
# mcp_server.py directly. Normally you don't run this by hand —
# agent_app_setup_and_run.sh spawns it automatically as a stdio
# subprocess. Run it standalone only to test/inspect the MCP server.
set -e

SERVICE_NAME="mcp-server"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$BASE_DIR/venvs/${SERVICE_NAME}"

LOGS_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOGS_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="$LOGS_DIR/run_${TIMESTAMP}.log"

TRANSPORT="stdio"
while [[ $# -gt 0 ]]; do
    case $1 in
        --transport) TRANSPORT="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --transport MODE   stdio (default, for agent-app to spawn) or sse (network, for Docker)"
            exit 0
            ;;
        *) echo "Unknown option: $1. Use --help."; exit 1 ;;
    esac
done

echo "============================================================================" | tee "$LOG_FILE"
echo "  MCP Server Setup & Run"                                                     | tee -a "$LOG_FILE"
echo "  Transport: $TRANSPORT"                                                      | tee -a "$LOG_FILE"
echo "  Log: $LOG_FILE"                                                             | tee -a "$LOG_FILE"
echo "============================================================================" | tee -a "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

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
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    pip install --no-cache-dir -r "$SCRIPT_DIR/requirements.txt" -q
    echo "Dependencies installed"
else
    echo "WARNING: requirements.txt not found"
fi

echo ""
echo "Step 3: Loading environment variables..."
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
echo "Environment Configuration:"
echo "  DIP_API_BASE_URL      : ${DIP_API_BASE_URL:-(default: search.dip.bundestag.de)}"
echo "  MCP_CACHE_TTL_SECONDS : ${MCP_CACHE_TTL_SECONDS:-(default: 1800)}"
echo "  REDIS_URL             : ${REDIS_URL:-(default: redis://localhost:6379/1)}"

echo ""
echo "Step 4: Checking Redis connectivity..."
echo "----------------------------------------------"
REDIS_CHECK_URL="${REDIS_URL:-redis://localhost:6379/1}"
if command -v redis-cli &> /dev/null; then
    if redis-cli -u "$REDIS_CHECK_URL" ping &> /dev/null; then
        echo "Redis reachable at $REDIS_CHECK_URL"
    else
        echo "ERROR: Redis not reachable at $REDIS_CHECK_URL"
        echo "   Start it with 'redis-server' (native) or 'docker compose up redis'."
        exit 1
    fi
else
    echo "WARNING: redis-cli not found — skipping connectivity check, will fail at runtime if Redis is down"
fi

echo ""
echo "Step 5: Verifying core files..."
echo "----------------------------------------------"
REQUIRED_FILES=("mcp_server.py" "dip_client.py")
ALL_EXIST=true
for file in "${REQUIRED_FILES[@]}"; do
    if [ -f "$SCRIPT_DIR/$file" ]; then echo "OK: $file"
    else echo "MISSING: $file"; ALL_EXIST=false; fi
done
[ "$ALL_EXIST" = false ] && echo "ERROR: Required files missing!" && exit 1

echo ""
echo "============================================================================"
echo "  Starting MCP Server (transport=$TRANSPORT)"
echo "============================================================================"
export MCP_TRANSPORT="$TRANSPORT"
python "$SCRIPT_DIR/mcp_server.py"
EXIT_CODE=$?

echo ""
echo "============================================================================"
echo "  Complete | Exit code: $EXIT_CODE | Log: $LOG_FILE"
echo "============================================================================"
exit $EXIT_CODE
