#!/usr/bin/env python3
"""
TurtleQL Web UI — FastAPI backend

    python app.py          # starts on http://localhost:8084
"""

import json
import os
import re
import secrets
import threading
import time
import uuid
import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ---------------------------------------------------------------------------
# Reuse TurtleQL core modules (same imports as db2cli.py)
# ---------------------------------------------------------------------------
from mcp import MCPClient
from adapters import DbAdapter, get_adapter

# ---------------------------------------------------------------------------
# Shared resources (loaded once at startup)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
PROFILES_PATH = BASE_DIR / "data" / "db_profiles.yaml"


def _load_profiles() -> List[Dict]:
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f).get("profiles", [])
    except FileNotFoundError:
        return []


def _load_local_config():
    """Push config.local.yaml provider keys into env vars (won't overwrite existing env)."""
    cfg_path = BASE_DIR / "config.local.yaml"
    if not cfg_path.exists():
        return
    _yaml_to_env = {
        "azure_openai_endpoint":           "AZURE_OPENAI_ENDPOINT",
        "azure_openai_api_key":            "AZURE_OPENAI_API_KEY",
        "azure_openai_large_deployment":   "AZURE_OPENAI_LARGE_DEPLOYMENT",
        "azure_openai_small_deployment":   "AZURE_OPENAI_SMALL_DEPLOYMENT",
        "azure_openai_api_version":        "AZURE_OPENAI_API_VERSION",
    }
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                env_key = _yaml_to_env.get(key)
                if env_key and value and env_key not in os.environ:
                    os.environ[env_key] = value
    except Exception:
        pass


PROFILES = _load_profiles()
_load_local_config()

MODEL_LARGE = os.environ.get("TURTLEQL_LARGE_MODEL", "claude-sonnet-4-6")
MODEL_SMALL = os.environ.get("TURTLEQL_SMALL_MODEL", "claude-haiku-4-5-20251001")

_db_adapter: DbAdapter = get_adapter(os.environ.get("TURTLEQL_DB_ADAPTER", "sqlite"))


# ---------------------------------------------------------------------------
# Session store  (in-memory, single-user local tool)
# ---------------------------------------------------------------------------
SESSION_TIMEOUT = 1800  # 30 minutes

class Session:
    """Holds per-browser-session state: DB connection, MCP client, history."""

    def __init__(self):
        self.id: str = secrets.token_urlsafe(16)
        self.connection = None
        self.db_adapter: DbAdapter = _db_adapter
        self.username: Optional[str] = None
        self.current_profile: Optional[Dict] = None
        self.mcp: MCPClient = MCPClient()
        self.mcp.connect()
        self.mcp_thread: List[Dict] = []
        self.conversation_history: List[Dict] = []
        self.last_generated_sql: Optional[str] = None
        self.last_commentary: str = ""
        self.last_active: float = time.time()
        self._active_cursors: Dict[str, object] = {}
        self._cancel_requested: bool = False

    def touch(self):
        self.last_active = time.time()

    @property
    def connected(self) -> bool:
        return self.connection is not None

    def close(self):
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None
        self.mcp.close()


SESSIONS: Dict[str, Session] = {}
_cleanup_lock = threading.Lock()


def _get_session(request: Request) -> Session:
    sid = request.cookies.get("turtleql_session")
    if sid and sid in SESSIONS:
        s = SESSIONS[sid]
        s.touch()
        return s
    raise HTTPException(status_code=401, detail="No active session")


def _get_or_create_session(request: Request) -> Session:
    sid = request.cookies.get("turtleql_session")
    if sid and sid in SESSIONS:
        s = SESSIONS[sid]
        s.touch()
        return s
    s = Session()
    SESSIONS[s.id] = s
    return s


def _cleanup_sessions():
    """Remove sessions idle longer than SESSION_TIMEOUT."""
    with _cleanup_lock:
        now = time.time()
        expired = [sid for sid, s in SESSIONS.items()
                    if now - s.last_active > SESSION_TIMEOUT]
        for sid in expired:
            SESSIONS[sid].close()
            del SESSIONS[sid]


# ---------------------------------------------------------------------------
# LLM provider (abstracted — Bedrock / Anthropic / Azure OpenAI)
# ---------------------------------------------------------------------------
from providers import get_provider

_provider = None


def _get_provider():
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


def _extract_last_sql(text: str) -> tuple:
    """Extract SQL from Claude response. Returns (sql, commentary)."""
    commentary = ""

    def _trim_preamble(s):
        lines = s.split("\n")
        for i, line in enumerate(lines):
            if line and line[0] in (" ", "\t"):
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            if re.match(r"SELECT(?:\s+(?!\()|\s*$)", stripped, re.IGNORECASE):
                return "\n".join(lines[i:]).strip() if i > 0 else s
            if re.match(r"(INSERT\b|UPDATE\b|DELETE\b|WITH\b)", stripped, re.IGNORECASE):
                return "\n".join(lines[i:]).strip() if i > 0 else s
        return s

    # Priority 1: code fence
    fence_matches = list(re.finditer(r"```(?:sql)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE))
    for fm in reversed(fence_matches):
        candidate = _trim_preamble(fm.group(1).strip())
        if re.match(r"(SELECT\s+(?!\()\S|INSERT\b|UPDATE\b|DELETE\b|WITH\s+\w+\s+AS\s*\()",
                    candidate, re.IGNORECASE):
            before = text[:fm.start()].strip()
            after = text[fm.end():].strip()
            commentary = "\n\n".join(filter(None, [before, after]))
            return candidate, commentary

    clean = _trim_preamble(text.replace("```sql", "").replace("```", "").strip())

    # Priority 2: terminated SQL
    sql_pattern = re.compile(
        r"((?:SELECT|INSERT|UPDATE|DELETE)\s[\s\S]*?(?:;|FETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY\s*;?)"
        r"|WITH\s+\w+\s+AS\s*\([\s\S]*?(?:;|FETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY\s*;?))",
        re.IGNORECASE,
    )
    matches = sql_pattern.findall(clean)
    if matches:
        sql = matches[-1].strip()
        if "|---|" not in sql and not re.search(r"\n\s*\|[^\n|]+\|", sql):
            end_pos = clean.rfind(sql.rstrip(";"))
            if end_pos != -1:
                after = clean[end_pos + len(sql.rstrip(";")):].lstrip(";").strip()
                if after:
                    commentary = after
            return sql, commentary

    # Priority 3: paragraph-break
    sql_start = re.search(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH\s+\w+\s+AS\s*\()",
                          clean, re.IGNORECASE | re.MULTILINE)
    if sql_start:
        after_start = clean[sql_start.start():]
        para_break = re.search(r"\n\s*\n", after_start)
        if para_break:
            sql = after_start[:para_break.start()].strip()
            commentary = after_start[para_break.end():].strip()
            return sql, commentary
        return after_start.strip(), ""

    return text, ""


def _extract_sql_with_small_model(response_text: str) -> Optional[tuple]:
    """Use the small model to extract SQL. Returns (sql, commentary) or None."""
    prompt = (
        "The text below is an AI assistant response that contains a SQL query "
        "mixed with explanation text, markdown tables, or comments.\n"
        "Extract and return ONLY the complete, executable SQL statement.\n"
        "Rules:\n"
        "- No code fences (no ```sql or ```)\n"
        "- No explanation text, no comments, no markdown\n"
        "- If multiple SQL versions exist, return the LAST (most final) one\n"
        "- If there is no SQL at all, return exactly: NO_SQL\n\n"
        "TEXT:\n" + response_text
    )
    try:
        resp = _get_provider().complete(
            [{"role": "user", "content": prompt}],
            system="", tools=[], model=MODEL_SMALL, max_tokens=2048,
        )
        sql = (resp.text or "").strip()
        if not sql or sql.strip() == "NO_SQL":
            return None
        sql = sql.replace("```sql", "").replace("```", "").strip()
        SQL_STARTS = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "CREATE", "DROP", "ALTER")
        if not sql.upper().startswith(SQL_STARTS):
            return None
        commentary = re.sub(r"```(?:sql)?\s*[\s\S]*?```", "", response_text, flags=re.IGNORECASE)
        commentary = re.sub(r"\n{3,}", "\n\n", commentary).strip()
        return sql, commentary
    except Exception:
        return None


def _load_tools_config() -> List[Dict]:
    """Load tool definitions from tools/default_tools.yaml."""
    tools_path = BASE_DIR / "tools" / "default_tools.yaml"
    if not tools_path.exists():
        return []
    try:
        with open(tools_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data.get("tools", [])
    except Exception:
        return []


def _translate_nl_to_sql(session: Session, nl: str, on_progress=None) -> Dict:
    """Translate natural language to SQL via MCP. Returns {sql, commentary, error}."""
    if not session.mcp.available:
        session.mcp.connect()
    if not session.mcp.available:
        return {"sql": None, "commentary": None,
                "error": "MCP server not available — configure mcp_server_url in config.local.yaml"}
    return _translate_with_mcp(session, nl, on_progress)


def _translate_with_mcp(session: Session, nl: str, on_progress=None) -> Dict:
    """LLM uses MCP tools to explore schema, then generates SQL."""
    _prog = on_progress or (lambda m: None)

    system_prompt = "You are a SQL expert. Convert the user's request to valid SQL for the connected database.\n\nUse the available tools to find relevant table schemas before writing SQL:\n1. Call get_sql_rules first\n2. Call list_experts / get_expert for relevant domain knowledge\n3. Call search_tables to find relevant tables\n4. Call get_table_details for each table you need\n5. Generate the SQL"

    tools = _load_tools_config()

    _tool_labels = {
        "get_sql_rules": "Loading SQL rules",
        "list_experts": "Checking available expert knowledge",
        "get_expert": "Loading expert knowledge",
        "search_tables": "Searching for relevant tables",
        "get_table_details": "Reading table schema",
    }

    if not session.mcp_thread:
        session.mcp_thread = [{"role": "user", "content": nl}]
    else:
        session.mcp_thread.append({"role": "user", "content": nl})
    messages = session.mcp_thread

    try:
        for round_num in range(10):
            if session._cancel_requested:
                return {"sql": None, "commentary": None, "error": "Cancelled"}
            _prog("Thinking..." if round_num == 0 else "Refining query...")
            resp = _get_provider().complete(messages, system=system_prompt, tools=tools,
                                            model=MODEL_LARGE, max_tokens=4096)

            if resp.stop_reason == "tool_calls":
                tool_results = []
                for tc in resp.tool_calls:
                    label = _tool_labels.get(tc.name, tc.name)
                    hint = tc.input.get("query") or tc.input.get("table_name") or tc.input.get("name") or ""
                    _prog(f"{label}: {hint}" if hint else label)
                    result = session.mcp._call_tool(tc.name, tc.input)
                    tool_results.append({"id": tc.id, "content": result or "No result returned"})
                messages.append({"role": "assistant", "tool_calls": resp.tool_calls, "text": None})
                messages.append({"role": "tool_results", "tool_results": tool_results})

            elif resp.stop_reason == "end_turn":
                _prog("Generating SQL...")
                text = resp.text or ""
                result = _extract_sql_with_small_model(text)
                if result:
                    sql, commentary = result
                else:
                    sql, commentary = _extract_last_sql(text)
                messages.append({"role": "assistant", "text": text or None, "tool_calls": []})
                session.conversation_history.append({
                    "role": "user", "content": nl, "timestamp": datetime.now().isoformat(),
                })
                session.conversation_history.append({
                    "role": "assistant", "content": sql, "commentary": commentary,
                    "timestamp": datetime.now().isoformat(),
                })
                return {"sql": sql, "commentary": commentary, "error": None}
            else:
                return {"sql": None, "commentary": None, "error": f"Unexpected stop_reason: {resp.stop_reason}"}

        return {"sql": None, "commentary": None, "error": "Max tool rounds reached"}
    except Exception as e:
        return {"sql": None, "commentary": None, "error": str(e)}


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------
def _parse_operation(sql: str) -> str:
    su = sql.strip().upper()
    if su.startswith("WITH"):
        return "SELECT"
    for op in ["SELECT", "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE"]:
        if su.startswith(op):
            return op
    return "UNKNOWN"


def _is_destructive(op: str) -> bool:
    return op in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(title="TurtleQL", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/health")
async def health():
    return {"status": "ok", "adapter": _db_adapter.name(), "sessions": len(SESSIONS)}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = BASE_DIR / "static" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/profiles")
async def get_profiles():
    _cleanup_sessions()
    return [
        {"name": p["name"], "host": p.get("host", ""), "port": p.get("port", ""),
         "database": p.get("database", p.get("path", ""))}
        for p in PROFILES
    ]


@app.post("/connect")
async def connect(request: Request):
    body = await request.json()
    profile_name = body.get("profile", "")
    username = body.get("username", "")
    password = body.get("password", "")

    if not username or not password:
        raise HTTPException(400, "username and password required")

    matches = [p for p in PROFILES if profile_name.lower() in p["name"].lower()]
    if not matches:
        raise HTTPException(404, f"Profile '{profile_name}' not found")
    profile = matches[0]

    session = _get_or_create_session(request)
    try:
        connect_kwargs = {k: v for k, v in profile.items() if k != "name"}
        if username:
            connect_kwargs["user"] = username
        if password:
            connect_kwargs["password"] = password
        session.connection = session.db_adapter.connect(**connect_kwargs)
        session.username = username
        session.current_profile = profile
    except Exception as e:
        raise HTTPException(500, f"Connection failed: {e}")

    resp = JSONResponse({
        "status": "connected",
        "database": profile["database"],
        "profile": profile["name"],
    })
    resp.set_cookie("turtleql_session", session.id, httponly=True, samesite="lax")
    return resp


@app.post("/disconnect")
async def disconnect(request: Request):
    session = _get_session(request)
    session.close()
    session.connection = None
    session.username = None
    session.current_profile = None
    session.mcp_thread = []
    return {"status": "disconnected"}


@app.post("/query")
async def post_query(request: Request):
    """Accept NL or raw SQL, translate + execute, stream progress via SSE."""
    import queue as _queue

    session = _get_or_create_session(request)
    body = await request.json()
    nl = body.get("nl", "").strip()
    raw_sql = body.get("sql", "").strip()
    password = body.get("password")  # for destructive-op confirmation

    if not nl and not raw_sql:
        raise HTTPException(400, "Provide 'nl' or 'sql'")

    session._cancel_requested = False
    query_id = str(uuid.uuid4())[:8]

    # Queue bridges the blocking worker thread → SSE generator
    q: _queue.Queue = _queue.Queue()
    _SENTINEL = object()

    def _emit(event_type, data):
        q.put((event_type, data))

    def _worker():
        """Runs in a background thread — does all the heavy lifting."""
        sql = raw_sql
        commentary = ""

        try:
            # --- NL → SQL translation ---
            if nl:
                SQL_STARTERS = ("SELECT", "INSERT", "UPDATE", "DELETE", "DROP",
                                "TRUNCATE", "ALTER", "WITH", "CREATE")
                if nl.upper().startswith(SQL_STARTERS):
                    sql = nl
                else:
                    _emit("progress", {"message": "Understanding your question..."})
                    result = _translate_nl_to_sql(
                        session, nl,
                        on_progress=lambda msg: _emit("progress", {"message": msg}),
                    )

                    if session._cancel_requested:
                        _emit("cancelled", {"message": "Cancelled"})
                        return

                    if result["error"]:
                        _emit("error", {"error": result["error"]})
                        return
                    sql = result["sql"]
                    commentary = result["commentary"] or ""
                    if not sql:
                        _emit("error", {"error": "Could not generate SQL"})
                        return

                    # Claude answered conversationally (not SQL)
                    if not sql.strip().upper().startswith(SQL_STARTERS):
                        _emit("result", {"sql": None, "commentary": sql,
                                         "columns": [], "rows": [], "rowcount": 0})
                        return

            session.last_generated_sql = sql
            session.last_commentary = commentary
            _emit("sql", {"sql": sql, "commentary": commentary})

            # --- Not connected — just return the SQL ---
            if not session.connected:
                _emit("result", {"sql": sql, "commentary": commentary,
                                 "columns": [], "rows": [], "rowcount": 0,
                                 "note": "Not connected — SQL generated but not executed"})
                return

            operation = _parse_operation(sql)

            # Destructive operation guard
            if _is_destructive(operation):
                if not password:
                    _emit("destructive", {
                        "destructive": True, "operation": operation,
                        "sql": sql, "commentary": commentary,
                        "message": "Password required to confirm destructive operation",
                    })
                    return

            # --- Execute SQL ---
            _emit("progress", {"message": "Executing query..."})

            if session._cancel_requested:
                _emit("cancelled", {"message": "Cancelled"})
                return

            db_result = session.db_adapter.execute(session.connection, sql)

            # Serialize non-JSON-native types
            for i, row in enumerate(db_result["rows"]):
                db_result["rows"][i] = [
                    str(v) if v is not None and not isinstance(v, (str, int, float, bool)) else v
                    for v in row
                ]

            _emit("result", {"query_id": query_id, "sql": sql,
                             "commentary": commentary, **db_result})

        except Exception as e:
            session._active_cursors.pop(query_id, None)
            _emit("error", {"error": str(e), "sql": sql, "commentary": commentary})

    def generate_events():
        """Synchronous generator — drains the queue as the worker pushes events."""
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

        while True:
            try:
                item = q.get(timeout=0.25)
            except _queue.Empty:
                # Worker still running — send a keep-alive comment to flush buffers
                if thread.is_alive():
                    yield ": keepalive\n\n"
                    continue
                else:
                    break
            if item is _SENTINEL:
                break
            event_type, data = item
            yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            # Terminal events — stop after yielding
            if event_type in ("result", "error", "cancelled", "destructive"):
                break

        thread.join(timeout=1)

    return StreamingResponse(generate_events(), media_type="text/event-stream")


@app.post("/query/cancel")
async def cancel_query(request: Request):
    session = _get_session(request)
    session._cancel_requested = True
    # Cancel any active DB cursors
    for qid, cursor in list(session._active_cursors.items()):
        try:
            cursor.cancel()
        except Exception:
            pass
    session._active_cursors.clear()
    return {"status": "cancelled"}


@app.get("/schema/search")
async def schema_search(q: str, request: Request):
    session = _get_session(request)
    if not session.mcp.available:
        raise HTTPException(503, "MCP server not available")
    results = session.mcp.search_tables(q)
    return {"results": results}


@app.get("/schema/table/{name}")
async def schema_table(name: str, request: Request):
    session = _get_session(request)
    if not session.mcp.available:
        raise HTTPException(503, "MCP server not available")
    details = session.mcp.get_table_details(name)
    if details:
        return details
    raise HTTPException(404, f"Table '{name}' not found")


@app.get("/history")
async def get_history(request: Request):
    session = _get_session(request)
    return {"history": session.conversation_history}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _get_web_port() -> int:
    """Read web_port from config.local.yaml, default 3000."""
    cfg_path = BASE_DIR / "config.local.yaml"
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("web_port:"):
                    try:
                        return int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
    return 3000


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
    port = _get_web_port()
    print(f"\n  TurtleQL Web UI")
    print(f"  Adapter: {_db_adapter.name()}")
    print(f"  Profiles: {len(PROFILES)}")
    print(f"  http://localhost:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
