# TurtleQL

Natural language to SQL, in a browser. Ask questions in plain English — TurtleQL generates and runs the SQL against your database.

Powered by any Claude-compatible LLM (Anthropic, AWS Bedrock, Azure OpenAI) and an optional MCP knowledge server for schema and domain context.

## What it does

- Translates plain English to SQL using a tool-use loop
- Executes the query and shows results in a table
- Auto-fixes SQL errors by re-checking schema via MCP tools
- Conversation threading — follow-up questions refine the previous query
- Pluggable database adapters (SQLite out of the box, Postgres optional)
- Pluggable LLM providers — switch via environment variable

## Quick start

```bash
pip install -r requirements.txt
cp config.example.yaml config.local.yaml
# Edit config.local.yaml — set mcp_server_url and db_adapter
python app.py        # http://localhost:8084
```

### LLM provider

Set one of these environment variables (first match wins):

```bash
ANTHROPIC_API_KEY=...            # Anthropic direct (default)
CLAUDE_CODE_USE_BEDROCK=1        # AWS Bedrock (also set AWS_PROFILE)
AZURE_OPENAI_ENDPOINT=https://…  # Azure OpenAI
```

### Database profiles

Add your connections to `data/db_profiles.yaml`:

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

## Knowledge server

TurtleQL integrates with [turtleatlas-mcp](https://github.com/tommaone/turtleatlas-mcp) — a generic MCP server that serves table schemas and domain expert files to the LLM.

```yaml
# config.local.yaml
mcp_server_url: http://localhost:3000/mcp
```

Without a knowledge server, TurtleQL falls back to standalone mode — the LLM generates SQL without schema context.

## Architecture

```
User prompt
    │
    ▼
LLM (tool-use loop)
    ├── get_sql_rules        ──▶  turtleatlas-mcp
    ├── list_experts
    ├── get_expert
    ├── search_tables
    └── get_table_details
    │
    ▼
SQL extraction
    │
    ▼
DbAdapter.execute()          ──▶  SQLite / Postgres / ...
```

## Configuration

See `config.example.yaml` and `CLAUDE.md` for the full setup guide.

## License

MIT
