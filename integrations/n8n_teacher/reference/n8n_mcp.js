const { Client } = require("@modelcontextprotocol/sdk/client/index.js");
const { StreamableHTTPClientTransport } = require("@modelcontextprotocol/sdk/client/streamableHttp.js");

class N8nMCP {

    constructor() {
        this.client = null;

        this.url =
            "https://myaisupport.app.n8n.cloud/mcp-server/http";

        this.token =
            process.env.N8N_MCP_TOKEN;
    }


    async connect() {

        this.client = new Client({
            name: "mika-builder",
            version: "1.0"
        });

        await this.client.connect(
            new StreamableHTTPClientTransport(
                new URL(this.url),
                {
                    requestInit: {
                        headers: {
                            Authorization: this.token
                        }
                    }
                }
            )
        );

        console.log("[MCP] Connected");
    }


    async callTool(name, args = {}) {

        if (!this.client) {
            throw new Error(
                "MCP client is not connected. Call connect() first."
            );
        }

        return await this.client.callTool({
            name,
            arguments: args
        });
    }


    // --------------------------------------------------
    // THIN GENERIC WRAPPERS
    // --------------------------------------------------

    async createWorkflowSDK(code) {

        return await this.callTool(
            "create_workflow_from_code",
            {
                code
            }
        );
    }


    async updateWorkflow(workflowId, operations) {

        return await this.callTool(
            "update_workflow",
            {
                workflowId,
                operations
            }
        );
    }


    async executeWorkflow(
        workflowId,
        executionMode = "manual"
    ) {

        return await this.callTool(
            "execute_workflow",
            {
                workflowId,
                executionMode
            }
        );
    }


    async getWorkflow(workflowId) {

        return await this.callTool(
            "get_workflow_details",
            {
                workflowId
            }
        );
    }


    async searchNodes(query) {

        return await this.callTool(
            "search_nodes",
            {
                query
            }
        );
    }


    async getNodeTypes(args = {}) {

        return await this.callTool(
            "get_node_types",
            args
        );
    }


    async exploreNodeResources(args = {}) {

        return await this.callTool(
            "explore_node_resources",
            args
        );
    }


    async validateNodeConfig(args = {}) {

        return await this.callTool(
            "validate_node_config",
            args
        );
    }


    async validateWorkflow(args = {}) {

        return await this.callTool(
            "validate_workflow",
            args
        );
    }
}


module.exports = {
    N8nMCP
};
