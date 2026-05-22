# CLAUDE.md

## What This Is

**TurtleQL** is a natural-language SQL web UI. Ask a question in plain English, get SQL back — executed against your database and shown in a results table.

Pluggable LLM backends (Anthropic, AWS Bedrock, Azure OpenAI) and pluggable database adapters (SQLite, Postgres). Connects to a [turtleatlas-mcp](https://github.com/tommaone/turtleatlas-mcp) server for table schema and domain context.

## Key Files

| File | Role |
|------|------|
| `app.py` | FastAPI backend — sole entry point |
| `adapters.py` | Database adapter interface + SQLite / Postgres implementations |
| `providers.py` | LLM provider abstraction (Anthropic / Bedrock / Azure OpenAI) |
| `mcp.py` | MCP transport layer (local stdio subprocess or remote HTTP/SSE) |
| `tools/default_tools.yaml` | Tool definitions passed to the LLM during SQL generation |
| `data/db_profiles.yaml` | Database connection profiles (no credentials stored) |

## Configuration

Copy `config.example.yaml` to `config.local.yaml`:

```yaml
# MCP knowledge server (required for schema/domain context)
mcp_server_url: http://localhost:3000/mcp    # remote
# mcp_local_path: /path/to/turtleatlas-mcp  # or local subprocess

# Database adapter
db_adapter: sqlite    # or: postgres

# Web UI port
web_port: 8084

# LLM provider (optional — env vars take precedence)
azure_openai_endpoint: https://...
azure_openai_api_key: ...
azure_openai_large_deployment: sonnet
azure_openai_small_deployment: haiku
```

LLM provider is selected by environment variable (first match wins):
- `AZURE_OPENAI_ENDPOINT` → Azure OpenAI
- `CLAUDE_CODE_USE_BEDROCK=1` → AWS Bedrock
- `ANTHROPIC_API_KEY` → Anthropic direct (default)

## Running

```bash
pip install -r requirements.txt
python app.py        # http://localhost:8084
```

## Database Profiles

Add connections to `data/db_profiles.yaml`:

```yaml
profiles:
  - name: "My SQLite DB"
    path: "/path/to/database.db"

  - name: "My Postgres DB"
    host: "localhost"
    port: 5432
    database: "mydb"
    user: "..."
    password: "..."
```

## MCP Integration

TurtleQL calls these tools during SQL generation:

| Tool | Purpose |
|------|---------|
| `get_sql_rules` | SQL dialect rules — always called first |
| `list_experts` | Discover available domain expert files |
| `get_expert` | Load a specific expert by name |
| `search_tables` | Search tables by keyword |
| `get_table_details` | Full schema for a specific table |

Edit `tools/default_tools.yaml` to match your MCP server's actual tools.

## Extending

- **New database**: implement `DbAdapter` in `adapters.py`, add to `get_adapter()` factory
- **New LLM provider**: implement a `complete()` method in `providers.py`, add to `get_provider()`
- **New MCP tools**: add entries to `tools/default_tools.yaml`
