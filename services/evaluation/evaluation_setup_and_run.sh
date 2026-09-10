#!/bin/bash
# services/evaluation/evaluation_setup_and_run.sh
# Sets up a venv for the evaluation harness (installs both its own thin
# requirements.txt AND agent-app's requirements.txt, since
# evaluate_end_to_end.py imports agent_graph.py directly), ensures
# mcp-server's own venv exists so tool calls work, then runs the
# ablation/quality-gate script.
set -e

SERVICE_NAME="evaluation"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$BASE_DIR/venvs/${SERVICE_NAME}"
AGENT_APP_DIR="$BASE_DIR/services/agent-app"
MCP_SERVER_DIR="$BASE_DIR/services/mcp-server"

TESTSET="golden_qa_set.jsonl"
FAITH_THRESHOLD="0.8"
COMPLETE_THRESHOLD="0.7"
while [[ $# -gt 0 ]]; do
    case $1 in
        --testset) TESTSET="$2"; shift 2 ;;
        --faithfulness-threshold) FAITH_THRESHOLD="$2"; shift 2 ;;
        --completeness-threshold) COMPLETE_THRESHOLD="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 [--testset FILE] [--faithfulness-threshold F] [--completeness-threshold F]"
            exit 0
            ;;
        *) echo "Unknown option: $1. Use --help."; exit 1 ;;
    esac
done

LOGS_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOGS_DIR"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="$LOGS_DIR/run_${TIMESTAMP}.log"

echo "============================================================================" | tee "$LOG_FILE"
echo "  Evaluation Harness Setup & Run"                                             | tee -a "$LOG_FILE"
echo "  Log: $LOG_FILE"                                                             | tee -a "$LOG_FILE"
echo "============================================================================" | tee -a "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

echo ""
echo "Step 1: Setting up Python environment..."
echo "----------------------------------------------"
if command -v python3.11 &> /dev/null; then
    PYTHON_BIN="python3.11"
else
    PYTHON_BIN="python3"
fi
if [ ! -d "$VENV_DIR" ]; then
    $PYTHON_BIN -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
echo "Activated ($(python --version))"

echo ""
echo "Step 2: Installing dependencies (evaluation + agent-app, since"
echo "evaluate_end_to_end.py imports agent_graph.py directly)..."
echo "----------------------------------------------"
export PIP_BREAK_SYSTEM_PACKAGES=1
pip install --upgrade pip -q
pip install --no-cache-dir -r "$SCRIPT_DIR/requirements.txt" -q
pip install --no-cache-dir -r "$AGENT_APP_DIR/requirements.txt" -q
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
export MCP_TRANSPORT="stdio"
export MCP_SERVER_PYTHON="$MCP_VENV_DIR/bin/python"
export MCP_SERVER_SCRIPT="$MCP_SERVER_DIR/mcp_server.py"

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
    echo "WARNING: .env not found — copy example.env to .env first"
fi

echo ""
echo "Step 5: Running evaluation (PYTHONPATH includes agent-app)..."
echo "----------------------------------------------"
export PYTHONPATH="$AGENT_APP_DIR:$PYTHONPATH"
cd "$SCRIPT_DIR"
python evaluate_end_to_end.py --testset "$TESTSET" \
    --faithfulness-threshold "$FAITH_THRESHOLD" \
    --completeness-threshold "$COMPLETE_THRESHOLD"
EXIT_CODE=$?

echo ""
echo "============================================================================"
echo "  Complete | Exit code: $EXIT_CODE | Log: $LOG_FILE"
echo "============================================================================"
exit $EXIT_CODE
