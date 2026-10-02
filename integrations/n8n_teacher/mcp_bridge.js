"use strict";

const {
  Client
} = require(
  "@modelcontextprotocol/sdk/client/index.js"
);

const {
  StreamableHTTPClientTransport
} = require(
  "@modelcontextprotocol/sdk/client/streamableHttp.js"
);


const MCP_URL =
  process.env.N8N_MCP_URL ||
  "https://myaisupport.app.n8n.cloud/mcp-server/http";


function requireToken() {
  const token =
    process.env.N8N_MCP_TOKEN;

  if (!token) {
    throw new Error(
      "N8N_MCP_TOKEN is not set."
    );
  }

  return token.startsWith("Bearer ")
    ? token
    : `Bearer ${token}`;
}


async function connect() {
  const client =
    new Client({
      name:
        "vr-agent-n8n-teacher",

      version:
        "0.1.0"
    });

  const transport =
    new StreamableHTTPClientTransport(
      new URL(MCP_URL),
      {
        requestInit: {
          headers: {
            Authorization:
              requireToken()
          }
        }
      }
    );

  await client.connect(
    transport
  );

  return {
    client,
    transport
  };
}


async function listTools() {
  const {
    client
  } = await connect();

  const result =
    await client.listTools();

  return (
    result.tools || []
  ).map(tool => ({
    name:
      tool.name,

    description:
      tool.description || ""
  }));
}


async function callTool(
  name,
  args = {}
) {
  const {
    client
  } = await connect();

  return await client.callTool({
    name,
    arguments: args
  });
}


async function status() {
  const tools =
    await listTools();

  return {
    connected: true,

    endpoint:
      MCP_URL,

    tool_count:
      tools.length,

    tools:
      tools.map(
        tool => tool.name
      )
  };
}


async function searchNodes(
  query
) {
  return await callTool(
    "search_nodes",
    {
      query
    }
  );
}


function success(
  request,
  result
) {
  return {
    id:
      request.id ?? null,

    ok: true,

    source:
      "mcp",

    action:
      request.action ||
      request.command,

    result,

    error: null
  };
}


function failure(
  request,
  error
) {
  return {
    id:
      request.id ?? null,

    ok: false,

    source:
      "mcp",

    action:
      request.action ||
      request.command,

    result: null,

    error: {
      name:
        error?.name ||
        "Error",

      message:
        error?.message ||
        String(error)
    }
  };
}


async function handle(
  request
) {
  switch (
    request.command
  ) {

    case "status":
      return await status();

    case "search_nodes":
      return await searchNodes(
        request.query || ""
      );

    default:
      throw new Error(
        `Unknown MCP bridge command: ${request.command}`
      );
  }
}


async function main() {
  const command =
    process.argv[2];

  let request;

  if (
    command === "status"
  ) {
    request = {
      id: "cli",
      command: "status"
    };

  } else if (
    command === "search"
  ) {
    request = {
      id: "cli",
      command:
        "search_nodes",

      query:
        process.argv
          .slice(3)
          .join(" ") ||
        "Schedule Trigger"
    };

  } else {
    throw new Error(
      "Usage: node mcp_bridge.js status | search <query>"
    );
  }

  try {
    const result =
      await handle(
        request
      );

    console.log(
      JSON.stringify(
        success(
          request,
          result
        ),
        null,
        2
      )
    );

  } catch (error) {
    console.log(
      JSON.stringify(
        failure(
          request,
          error
        ),
        null,
        2
      )
    );

    process.exitCode = 1;
  }
}


main().catch(
  error => {
    console.error(
      error
    );

    process.exit(1);
  }
);
