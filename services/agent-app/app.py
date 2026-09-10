import logging
import os
import uuid

import db_utils
import httpx
import pandas as pd
import plotly.express as px
import streamlit as st

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AGENT_SERVICE_URL = os.environ["AGENT_SERVICE_URL"]


@st.cache_resource(show_spinner=False)
def _get_http_client() -> httpx.Client:
    return httpx.Client(base_url=AGENT_SERVICE_URL, timeout=200.0,
                         headers={"x-app-secret": os.environ["APP_SHARED_SECRET"]})


@st.cache_resource(show_spinner=False)
def _ensure_db_initialized() -> bool:
    db_utils.init_db()
    return True


def run_query(query: str, thread_id: str) -> dict:
    response = _get_http_client().post("/query", json={"query": query, "thread_id": thread_id})
    response.raise_for_status()
    return response.json()


def get_thread_messages(thread_id: str) -> list[dict]:
    response = _get_http_client().get(f"/threads/{thread_id}/messages")
    response.raise_for_status()
    return response.json()


def build_party_chart(chart_data):
    """Build the Plotly figure from the structured chart_data payload the
    backend returned/persisted. Used identically for a live answer and
    for history reloaded from the DB.

    Accepts a structured payload {"type": "party_distribution", "distributions": [...]}
    or a legacy single distribution dict. Normalizes to a list and renders
    N distributions uniformly (N=1 is the standard chart, N>1 is a grouped
    bar chart with one color per period). Returns None for empty/invalid data
    so the caller can fail gracefully without rendering a broken chart.
    """
    if not chart_data:
        return None

    # Normalize to a list of distribution dicts + a chart_type
    if isinstance(chart_data, dict) and chart_data.get("type") == "party_distribution":
        distributions = chart_data.get("distributions") or []
        chart_type = chart_data.get("chart_type", "bar")
    elif isinstance(chart_data, dict) and "counts" in chart_data:
        # Legacy single-distribution row from the DB
        distributions = [chart_data]
        chart_type = chart_data.get("chart_type", "bar")
    else:
        distributions = [chart_data] if chart_data else []
        chart_type = "bar"

    distributions = [d for d in distributions if d]
    if not distributions:
        return None

    chart_fn = px.scatter if chart_type == "scatter" else px.bar

    rows = []
    for dist in distributions:
        label = f"WP {dist['wahlperiode']}" if dist.get("wahlperiode") else (dist.get("date_range") or "—")
        for party, count in (dist.get("counts") or {}).items():
            rows.append({"Party": party, "Count": count, "Period": str(label)})
    df = pd.DataFrame(rows, columns=["Party", "Count", "Period"])

    is_comparison = len(distributions) > 1
    title = ("Party Distribution Comparison" if is_comparison
             else f"Party Distribution (Wahlperiode {distributions[0].get('wahlperiode', '')})")

    kwargs = {"x": "Party", "y": "Count", "title": title, "template": "plotly_white"}
    if is_comparison:
        kwargs["color"] = "Period"
    if chart_type != "scatter":
        kwargs["barmode"] = "group"

    return chart_fn(df, **kwargs)



def _check_password() -> bool:
    if st.session_state.get("authenticated"):
        return True
    st.title("DIP Parliamentary Agent")
    pwd = st.text_input("Password", type="password")
    if st.button("Enter"):
        if pwd == os.environ["APP_PASSWORD"]:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False



def main():
    if not _check_password():
        return
    st.set_page_config(page_title="DIP Parliamentary Agent")
    _ensure_db_initialized()

    query_params = st.query_params
    if "thread_id" not in st.session_state:
        if "thread" in query_params:
            st.session_state.thread_id = query_params["thread"]
            st.session_state.thread_persisted = True
            # Initialize empty; we will populate it from DB below to avoid duplicates
            st.session_state.chat_messages = []
            st.session_state.loaded_from_db = False
            logger.info("Loading chat session: thread_id=%s", st.session_state.thread_id)
        else:
            st.session_state.thread_id = str(uuid.uuid4())
            st.session_state.thread_persisted = False
            st.session_state.chat_messages = []
            st.session_state.loaded_from_db = True
            logger.info("New chat session: thread_id=%s", st.session_state.thread_id)

    # Load messages from DB only once per thread to prevent duplicates on rerun
    if not st.session_state.get("loaded_from_db", False):
        try:
            db_msgs = get_thread_messages(st.session_state.thread_id)
            st.session_state.chat_messages.extend(db_msgs)
        except httpx.HTTPError:
            logger.exception("failed to load chat history (thread_id=%s)", st.session_state.thread_id)
            st.warning("Couldn't load this conversation's history — starting fresh from here.")
        st.session_state.loaded_from_db = True

    st.query_params["thread"] = st.session_state.thread_id

    with st.sidebar:
        st.subheader("Conversations")
        st.caption("Public demo — every visitor can see every thread here.")
        if st.button("+ New chat", use_container_width=True):
            st.session_state.thread_id = str(uuid.uuid4())
            st.session_state.thread_persisted = False
            st.session_state.chat_messages = []
            st.rerun()
        st.divider()
        for thread in db_utils.list_threads():
            tid = str(thread["thread_id"])
            label = thread["title"] or "Untitled conversation"
            if st.button(label, key=f"thread_{tid}", use_container_width=True):
                st.session_state.thread_id = tid
                st.session_state.thread_persisted = True
                try:
                    st.session_state.chat_messages = get_thread_messages(tid)
                except httpx.HTTPError:
                    logger.exception("failed to load chat history (thread_id=%s)", tid)
                    st.session_state.chat_messages = []
                    st.warning("Couldn't load this conversation's history — starting fresh from here.")
                st.session_state.loaded_from_db = True
                st.rerun()

    st.title("DIP Parliamentary Agent")

    for idx, msg in enumerate(st.session_state.chat_messages):
        with st.chat_message(msg["role"]):
            if msg.get("thinking_log"):
                with st.expander("🧠 Thinking", expanded=False):
                    for step in msg["thinking_log"]:
                        st.markdown(f"- {step}")
            st.markdown(msg["content"])
            if msg.get("chart_data"):
                fig = build_party_chart(msg["chart_data"])
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True,
                                    key=f"history_plot_{idx}")

    query = st.chat_input("Ask about German parliamentary data...")
    if query:
        if not st.session_state.thread_persisted:
            db_utils.create_thread(st.session_state.thread_id, title=query)
            st.session_state.thread_persisted = True

        st.session_state.chat_messages.append({"role": "user", "content": query})
        with st.chat_message("user"):
            st.markdown(query)

        with st.chat_message("assistant"), st.spinner("Thinking..."):
            try:
                result = run_query(query, st.session_state.thread_id)
                answer = result["answer"]
                chart_data = result.get("party_distribution")
                thinking_log = result.get("thinking_log") or []
            except httpx.HTTPError:
                logger.exception("agent-service request failed")
                answer = "Sorry, the backend took too long to respond. Please try again."
                chart_data = None
                thinking_log = []

            if thinking_log:
                with st.expander("🧠 Thinking", expanded=False):
                    for step in thinking_log:
                        st.markdown(f"- {step}")

            st.markdown(answer)

            # chart_data may be a structured wrapper or a legacy dict.
            # build_party_chart normalizes it and returns None if invalid.
            msg = {"role": "assistant", "content": answer, "chart_data": chart_data,
                   "thinking_log": thinking_log}

            fig = build_party_chart(chart_data) if chart_data else None
            if fig is not None:
                chart_key = f"current_plot_{len(st.session_state.chat_messages)}"
                st.plotly_chart(fig, use_container_width=True, key=chart_key)

            st.session_state.chat_messages.append(msg)
            db_utils.touch_thread(st.session_state.thread_id)




if __name__ == "__main__":
    main()