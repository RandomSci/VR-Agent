const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = require("@modelcontextprotocol/sdk/client/streamableHttp.js");


const TOKEN = process.env.N8N_MCP_TOKEN;


async function main(){

    const client = new Client({
        name:"sdk-reference-test",
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
                        Authorization:TOKEN
                    }
                }
            }
        )
    );


    console.log("MCP CONNECTED");


    const result =
    await client.callTool({

        name:"get_workflow_sdk_reference",

        arguments:{}

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
