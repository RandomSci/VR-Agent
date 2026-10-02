const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = require("@modelcontextprotocol/sdk/client/streamableHttp.js");


async function main(){


const client = new Client({
    name:"clean-test",
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


console.log("MCP CONNECTED");


// CREATE ONLY SCHEDULE

const create =
await client.callTool({

name:"create_workflow_from_code",

arguments:{

code:`

const schedule = trigger({

type:"n8n-nodes-base.scheduleTrigger",

version:1.3,

config:{

name:"Schedule Trigger",

parameters:{}

},

output:[{}]

});


export default workflow(
"clean-test-001",
"Clean Mika Test"
)

.add(schedule);

`

}

});


const created =
JSON.parse(
create.content[0].text
);


console.log("CREATED:");
console.log(created);


const id =
created.workflowId;



// ADD CODE NODE USING OPERATIONS

const update =
await client.callTool({

name:"update_workflow",

arguments:{

workflowId:id,

versionName:"Add Code Node",

operations:[

{

type:"addNode",

node:{

name:"Code",

type:"n8n-nodes-base.code",

typeVersion:2,

position:[600,300],

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


console.log("UPDATED:");
console.log(
JSON.stringify(update,null,2)
);



// VERIFY

const verify =
await client.callTool({

name:"get_workflow_details",

arguments:{
workflowId:id
}

});


console.log("VERIFY:");

console.log(
JSON.stringify(
verify,null,2
)
);


}


main()
.catch(console.error);
