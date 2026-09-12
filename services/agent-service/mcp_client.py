import logging
import os

from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://mcp-server:8000/sse")
MCP_SERVER_PYTHON = os.getenv("MCP_SERVER_PYTHON")
MCP_SERVER_SCRIPT = os.getenv("MCP_SERVER_SCRIPT")

_ctx = None
_session: ClientSession | None = None
_tools_cache = None


async def start_mcp_session() -> None:
    global _ctx, _session
    if _session is not None:
        logger.info("mcp session already open — reusing it")
        return
    if MCP_TRANSPORT == "sse":
        logger.info("connecting to mcp-server over SSE: %s", MCP_SERVER_URL)
        _ctx = sse_client(MCP_SERVER_URL)
    else:
        if not MCP_SERVER_PYTHON or not MCP_SERVER_SCRIPT:
            raise RuntimeError(
                "MCP_SERVER_PYTHON / MCP_SERVER_SCRIPT not set — "
                "run via agent_app_setup_and_run.sh (or evaluation_setup_and_run.sh), "
                "which set these automatically.")
        logger.info("spawning mcp-server via stdio: %s %s", MCP_SERVER_PYTHON, MCP_SERVER_SCRIPT)

        _ctx = stdio_client(StdioServerParameters(
            command=MCP_SERVER_PYTHON, args=["-u", MCP_SERVER_SCRIPT],
            env=dict(os.environ)))

    read, write = await _ctx.__aenter__()
    _session = ClientSession(read, write)
    await _session.__aenter__()
    await _session.initialize()
    logger.info("mcp-server session ready")


async def stop_mcp_session() -> None:
    global _ctx, _session, _tools_cache
    if _session is not None:
        try:
            await _session.__aexit__(None, None, None)
        except Exception:
            logger.warning("error closing MCP session (likely already dead) — ignoring")
    if _ctx is not None:
        try:
            await _ctx.__aexit__(None, None, None)
        except Exception:
            logger.warning("error closing MCP transport (likely already dead) — ignoring")
    _session = None
    _ctx = None
    _tools_cache = None


async def reconnect_mcp_session() -> None:
    """Tears down a dead MCP session/transport and opens a fresh one.
    Also clears _tools_cache — the cached tool wrappers close over the
    OLD _session object, so they'd stay broken even after reconnecting
    if the cache weren't cleared too."""
    logger.warning("reconnecting MCP session after a detected transport failure...")
    await stop_mcp_session()
    await start_mcp_session()
    logger.info("MCP session reconnected successfully")


async def get_mcp_tools():
    global _tools_cache
    if _tools_cache is None:
        _tools_cache = await load_mcp_tools(_session)
        logger.info("discovered tools: %s", [t.name for t in _tools_cache])
    return _tools_cache
