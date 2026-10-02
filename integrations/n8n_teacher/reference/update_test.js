const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = require("@modelcontextprotocol/sdk/client/streamableHttp.js");


async function main(){

const client = new Client({
    name:"update-test",
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
await client.callTool({

name:"update_workflow",

arguments:{

workflowId:"Mud6wgszjvOgLlnT",

versionName:"Added Code node",

operations:[

{
type:"addNode",

node:{

name:"Code",

type:"n8n-nodes-base.code",

typeVersion:2,

position:[
600,
300
],

parameters:{

jsCode:
"return [{json:{message:'automation successful'}}];"

}

}

},

{

type:"addConnection",

source:"Schedule Trigger",

target:"Code"

}

]

}

});


console.log(
JSON.stringify(result,null,2)
);


}


main();
