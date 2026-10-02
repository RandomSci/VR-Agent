const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = require("@modelcontextprotocol/sdk/client/streamableHttp.js");


async function main(){

const client = new Client({
    name:"tools-test",
    version:"1.0"
});


await client.connect(
new StreamableHTTPClientTransport(
new URL(
"https://myaisupport.app.n8n.cloud/mcp-server/http"
),
{
requestInit:{
headers:{
Authorization:process.env.N8N_MCP_TOKEN
}
}
}
)
);


const result =
await client.listTools();


const tool =
result.tools.find(
x=>x.name==="update_workflow"
);


console.log(
JSON.stringify(tool,null,2)
);


}


main();
