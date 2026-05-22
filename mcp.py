#!/usr/bin/env python3
"""
MCPClient — connects TurtleQL to a turtleatlas-mcp server.

Auto-detects mode from config.local.yaml:
  standalone  no MCP configured (or repo/Node.js missing)
  local       spawn index.js as a subprocess over stdio
  remote      HTTP/SSE transport (stub, not yet implemented)
"""

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional

CONFIG_FILE = Path(__file__).parent / 'config.local.yaml'


def _load_local_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    config = {}
    with open(CONFIG_FILE, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#') or ':' not in line:
                continue
            key, _, value = line.partition(':')
            value = value.strip()
            if value:
                config[key.strip()] = value
    return config


class MCPClient:
    """Routes MCP tool calls to the right transport based on config.local.yaml."""

    def __init__(self):
        self.mode = 'standalone'
        self._process: Optional[subprocess.Popen] = None
        self._req_id = 0
        self._lock = threading.Lock()
        self._local_path: Optional[str] = None
        self._server_url: Optional[str] = None

        cfg = _load_local_config()
        mcp_server_url = cfg.get('mcp_server_url', '')
        mcp_local_path = cfg.get('mcp_local_path', '')

        if mcp_server_url:
            self.mode = 'remote'
            self._server_url = mcp_server_url
        elif mcp_local_path:
            index_js = Path(mcp_local_path) / 'index.js'
            if index_js.exists():
                self.mode = 'local'
                self._local_path = str(mcp_local_path)
            # If path set but index.js missing → standalone (repo not cloned yet)

    @property
    def available(self) -> bool:
        if self.mode == 'local':
            return self._process is not None
        if self.mode == 'remote':
            return True  # assume available until proven otherwise
        return False

    def connect(self) -> bool:
        """Start MCP connection. Returns True on success."""
        if self.mode == 'standalone':
            return False
        if self.mode == 'local':
            return self._spawn_local()
        if self.mode == 'remote':
            print('   ⚠️  Remote MCP not yet implemented — falling back to standalone')
            self.mode = 'standalone'
            return False
        return False

    def _spawn_local(self) -> bool:
        try:
            self._process = subprocess.Popen(
                ['node', 'index.js'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._local_path,
                text=True,
                bufsize=1,
                encoding='utf-8',
            )
            # MCP handshake
            self._request('initialize', {
                'protocolVersion': '2024-11-05',
                'capabilities': {},
                'clientInfo': {'name': 'turtleql', 'version': '1.0'},
            })
            self._notify('notifications/initialized')
            return True
        except FileNotFoundError:
            print('   ⚠️  node not found — MCP requires Node.js. Falling back to standalone.')
            self.mode = 'standalone'
            self._process = None
            return False
        except Exception as e:
            print(f'   ⚠️  MCP spawn failed: {e}. Falling back to standalone.')
            self.mode = 'standalone'
            self._process = None
            return False

    # ------------------------------------------------------------------ #
    # JSON-RPC transport                                                   #
    # ------------------------------------------------------------------ #

    def _next_id(self) -> int:
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _request(self, method: str, params: dict) -> Optional[dict]:
        if not self._process:
            return None
        req_id = self._next_id()
        msg = json.dumps({'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params})
        try:
            self._process.stdin.write(msg + '\n')
            self._process.stdin.flush()
            while True:
                line = self._process.stdout.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    resp = json.loads(line)
                    if resp.get('id') == req_id:
                        return resp.get('result')
                except json.JSONDecodeError:
                    continue
        except Exception:
            return None

    def _notify(self, method: str):
        if not self._process:
            return
        msg = json.dumps({'jsonrpc': '2.0', 'method': method})
        try:
            self._process.stdin.write(msg + '\n')
            self._process.stdin.flush()
        except Exception:
            pass

    def _call_tool(self, name: str, arguments: dict) -> Optional[str]:
        result = self._request('tools/call', {'name': name, 'arguments': arguments})
        if not result:
            return None
        for item in result.get('content', []):
            if item.get('type') == 'text':
                return item['text']
        return None

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def search_tables(self, query: str, limit: int = 15) -> List[Dict]:
        """
        Search for relevant tables.
        Returns list of {table, score, categories, summary} or empty list.
        The raw response text (with expert knowledge) is stored in self._last_search_raw.
        """
        if not self.available:
            return []
        raw = self._call_tool('search_tables', {'query': query, 'limit': limit})
        if not raw:
            return []
        self._last_search_raw = raw
        return self._parse_search_results(raw)

    def get_table_details(self, table_name: str) -> Optional[Dict]:
        """
        Returns parsed table dict or None.
        Dict structure: {TABLE_NAME, NUMBER_OF_COLUMNS, COLUMNS, POSSIBLE_JOINS}
        COLUMNS: {col_name: {DATA_TYPE, COLUMN_LENGTH, NULLABLE, DOMAIN}}
        DOMAIN (when present): {DOMAIN_NAME, VALUES: {code: description}}
        POSSIBLE_JOINS: {target_table: join_condition}
        """
        if not self.available:
            return None
        raw = self._call_tool('get_table_details', {'table_name': table_name})
        if not raw:
            return None
        return self._extract_json_from_markdown(raw)

    def get_domain_values(self, table_details: Dict) -> Dict[str, Dict[str, str]]:
        """
        Extract domain values from a parsed table dict.
        Returns {col_name: {code: description}} for columns that have domains.
        """
        result = {}
        for col_name, col_info in table_details.get('COLUMNS', {}).items():
            domain = col_info.get('DOMAIN')
            if domain and domain.get('VALUES'):
                result[col_name] = domain['VALUES']
        return result

    def extract_expert_knowledge(self, raw_text: str) -> str:
        """Extract the Expert Knowledge section from a search_tables response."""
        marker = '# Expert Knowledge'
        idx = raw_text.find(marker)
        if idx == -1:
            return ''
        return raw_text[idx + len(marker):].strip()

    def close(self):
        """Terminate local subprocess if running."""
        if self._process:
            try:
                self._process.stdin.close()
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception:
                pass
            self._process = None

    def __repr__(self) -> str:
        return f'MCPClient(mode={self.mode}, available={self.available})'

    # ------------------------------------------------------------------ #
    # Parsers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_search_results(text: str) -> List[Dict]:
        """Parse search_tables markdown output into a list of result dicts."""
        results = []
        for m in re.finditer(r'^## \d+\. (\S+) \(score: (\d+)\)', text, re.MULTILINE):
            table = m.group(1)
            score = int(m.group(2))
            block = text[m.end():m.end() + 600]
            cats_m = re.search(r'\*\*Categories:\*\*\s*(.+)', block)
            categories = [c.strip() for c in cats_m.group(1).split(',')] if cats_m else []
            sum_m = re.search(r'\*\*Summary:\*\*\s*(.+)', block)
            summary = sum_m.group(1).strip() if sum_m else ''
            results.append({'table': table, 'score': score, 'categories': categories, 'summary': summary})
        return results

    @staticmethod
    def _extract_json_from_markdown(text: str) -> Optional[Dict]:
        """Extract the JSON code block from a get_table_details response."""
        m = re.search(r'```json\s*([\s\S]*?)\s*```', text)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
