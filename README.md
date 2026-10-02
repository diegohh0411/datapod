# datapod

A persistent, shared SQLite database for AI apps (Claude, ChatGPT), exposed as a
remote MCP server over HTTPS.

- **mcp**: FastMCP server with `execute_sql`, `list_tables` and `describe_table`.
  Any SQL is allowed, confined to one database file (ATTACH, extensions and
  non-introspection PRAGMAs are blocked; queries time out after 10s).
- **pocket-id**: self-hosted passkey OIDC provider. FastMCP's `OIDCProxy` sits in
  front of it to give MCP clients OAuth 2.1 with dynamic client registration.
- **caddy**: automatic HTTPS for `DOMAIN` (MCP) and `AUTH_DOMAIN` (Pocket ID).

Data lives in named volumes: `data` (SQLite DB + encrypted OAuth state),
`pocketid_data`, `caddy_data`.

## Setup

1. Point DNS for `DOMAIN` and `AUTH_DOMAIN` at the host; open ports 80 and 443.
2. `cp .env.example .env`, set the domains and generate the keys
   (`openssl rand -base64 32`).
3. `podman compose up -d pocket-id caddy`, then open
   `https://AUTH_DOMAIN/setup` and register your admin passkey.
4. In Pocket ID: create a `datapod` group and add yourself, then create an OIDC
   client with callback URL `https://DOMAIN/auth/callback`, restricted to that
   group. Put its client ID and secret in `.env`.
5. `podman compose up -d --build`
6. Add `https://DOMAIN/mcp` as a custom connector in Claude
   (Settings → Connectors) or ChatGPT (Developer mode → Connectors).

## Local testing

```sh
cd mcp && pip install . && DATAPOD_NO_AUTH=1 DATAPOD_DB=./dev.db python server.py
npx @modelcontextprotocol/inspector   # connect to http://localhost:8000/mcp
```

## Backup

```sh
podman compose exec mcp python -c "import sqlite3; sqlite3.connect('/data/datapod.db').backup(sqlite3.connect('/data/backup.db'))"
```
