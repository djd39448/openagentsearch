// OpenAgentSearch public endpoint -- scaffold (package C2b fills in the routes, the index and the tools).
//
// Two surfaces on one Worker:
//   * GET-only JSON routes (/, /healthz, /search, /did/{did}) for agents whose sandbox only allows fetch();
//   * a remote MCP server at /mcp (Streamable HTTP, stateless: no Durable Object, no session state) exposing
//     the same `search` and `did_lookup` operations as tools, built on `agents/mcp/server`'s
//     createMcpHandler and the MCP SDK v2 server.
//
// NOT guaranteed by this scaffold: any real search (the index is not wired yet), any rate limiting on
// /mcp beyond what the handler below applies, or a stable tool list -- C2b defines both.

import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

const SERVICE = { name: "openagentsearch", version: "0.0.0-scaffold" };

/** Build one MCP server per request (stateless): tools are pure over their inputs. */
function createServer() {
  const server = new McpServer({ name: SERVICE.name, version: SERVICE.version });
  server.registerTool(
    "healthz",
    {
      description: "Report the service name and whether the lexical index is loaded (scaffold: it is not).",
      inputSchema: z.object({}),
    },
    async () => ({
      content: [{ type: "text", text: JSON.stringify({ status: "ok", index_loaded: false }) }],
    }),
  );
  return server;
}

const mcp = createMcpHandler(createServer, { route: "/mcp" });

function json(status, body, extra = {}) {
  return new Response(JSON.stringify(body) + "\n", {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": "*",
      "x-content-type-options": "nosniff",
      ...extra,
    },
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/mcp") {
      return mcp(request, env, ctx);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json(405, { error: "method_not_allowed" }, { allow: "GET, HEAD" });
    }
    if (url.pathname === "/healthz") {
      return json(200, { status: "ok", index_loaded: false, service: SERVICE });
    }
    return json(404, { error: "not_found" });
  },
};
