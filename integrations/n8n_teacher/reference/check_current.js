const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = require("@modelcontextprotocol/sdk/client/streamableHttp.js");

async function main(){

const client = new Client({
name:"check",
version:"1"
});

await client.connect(
new StreamableHTTPClientTransport(
new URL("https://myaisupport.app.n8n.cloud/mcp-server/http"),
{
requestInit:{
headers:{
Authorization:process.env.N8N_MCP_TOKEN
}
}
}
)
);

const result = await client.callTool({

name:"get_workflow_details",

arguments:{
workflowId:"JIASmp6zT1i6Nke"
}

});


console.log(JSON.stringify(result,null,2));

}

main();
