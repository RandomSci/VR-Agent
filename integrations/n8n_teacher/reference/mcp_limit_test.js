const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = require("@modelcontextprotocol/sdk/client/streamableHttp.js");


const TOKEN = process.env.N8N_MCP_TOKEN;

const MCP_URL =
"https://myaisupport.app.n8n.cloud/mcp-server/http";


async function main(){

    const client = new Client({
        name:"mika-mcp-test",
        version:"1.0"
    });


    const transport =
    new StreamableHTTPClientTransport(
        new URL(MCP_URL),
        {
            requestInit:{
                headers:{
                    Authorization:TOKEN
                }
            }
        }
    );


    await client.connect(transport);


    console.log("MCP CONNECTED");


    const result =
    await client.callTool({

        name:"create_workflow_from_code",

        arguments:{

            code:`

const wf = workflow("mika-test-001", "Mika MCP Test");


const schedule = trigger(
    "n8n-nodes-base.scheduleTrigger",
    {
        parameters:{}
    }
);


wf.add(schedule);


wf;

`

        }

    });


    console.log(
        JSON.stringify(
            result,
            null,
            2
        )
    );

}


main().catch(console.error);
