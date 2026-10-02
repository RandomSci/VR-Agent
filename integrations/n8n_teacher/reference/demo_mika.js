const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const readline = require("readline/promises");
const { stdin: input, stdout: output } = require("process");

const { N8nMCP } = require("./n8n_mcp");

const PROJECT_URL =
    "https://myaisupport.app.n8n.cloud/projects/SQVrZFuPWfa7zE1N/workflows";


// ============================================================
// MCP RESPONSE HELPERS
// ============================================================

function unpack(result) {

    if (!result) return result;

    if (result.structuredContent) {
        return result.structuredContent;
    }

    const text =
        result.content?.find(x => x.type === "text")?.text;

    if (text) {
        try {
            return JSON.parse(text);
        } catch {
            return text;
        }
    }

    return result;
}


function printResult(result) {

    const data = unpack(result);

    if (typeof data === "string") {
        console.log(data);
        return;
    }

    console.log(
        JSON.stringify(data, null, 2)
    );
}


// ============================================================
// BROWSER
// ============================================================

function getChrome() {

    return new Promise((resolve, reject) => {

        http.get(
            "http://127.0.0.1:9222/json/version",
            res => {

                let data = "";

                res.on(
                    "data",
                    chunk => data += chunk
                );

                res.on(
                    "end",
                    () => {
                        try {
                            resolve(
                                JSON.parse(data)
                                    .webSocketDebuggerUrl
                            );
                        } catch (err) {
                            reject(err);
                        }
                    }
                );
            }
        ).on("error", reject);
    });
}


async function connectBrowser() {

    const ws = await getChrome();

    const browser =
        await chromium.connectOverCDP(ws);

    const context =
        browser.contexts()[0];

    let page =
        context.pages()[0];

    if (!page) {
        page = await context.newPage();
    }

    await page.goto(
        PROJECT_URL,
        {
            waitUntil: "domcontentloaded"
        }
    );

    return {
        browser,
        page
    };
}


// ============================================================
// FRONTEND HELPERS
// ============================================================

async function closeNodePanel(page, announce = false) {

    const floatingNodes =
        page.locator(
            '[data-test-id="floating-nodes"]'
        );

    const panelOpen =
        await floatingNodes.count() > 0 &&
        await floatingNodes.first()
            .isVisible()
            .catch(() => false);


    if (!panelOpen) {

        if (announce) {
            console.log(
                "ℹ No node is currently open."
            );
        }

        return false;
    }


    console.log(
        "👁 Closing open node..."
    );


    // First try the visible X/Close button inside n8n's modal layer.
    const closeButton =
        page.locator(
            '#app-modals button[aria-label*="close" i]:visible'
        ).first();


    if (await closeButton.count()) {

        await closeButton.click();

    } else {

        // Reliable fallback when n8n doesn't expose
        // a stable selector for the X button.
        await page.keyboard.press(
            "Escape"
        );
    }


    // Verify that the node view actually disappeared.
    try {

        await floatingNodes.first().waitFor({
            state: "hidden",
            timeout: 2500
        });

    } catch {

        // One more fallback in case the first Escape
        // only dismissed something inside the node.
        await page.keyboard.press(
            "Escape"
        );

        await floatingNodes.first().waitFor({
            state: "hidden",
            timeout: 3000
        });
    }


    console.log(
        "👁 Node closed."
    );

    return true;
}



async function ensureEditor(page) {

    await closeNodePanel(page);

    const editor =
        page.locator(
            '[data-test-id="radio-button-workflow"]'
        );

    if (await editor.count()) {

        const checked =
            await editor.getAttribute(
                "aria-checked"
            );

        if (checked !== "true") {
            await editor.click();
        }
    }

    await page.locator(
        '[data-test-id="canvas"]'
    ).waitFor({
        state: "visible",
        timeout: 15000
    });
}


async function findNode(page, name) {

    const nodes =
        page.locator(
            '[data-test-id="canvas-node"]'
        );

    const count =
        await nodes.count();

    for (
        let i = 0;
        i < count;
        i++
    ) {

        const node =
            nodes.nth(i);

        const nodeName =
            await node.getAttribute(
                "data-node-name"
            );

        if (nodeName === name) {
            return node;
        }
    }

    return null;
}


async function waitForNode(
    page,
    name,
    shouldExist = true,
    timeout = 5000
) {

    const start =
        Date.now();

    while (
        Date.now() - start < timeout
    ) {

        const node =
            await findNode(
                page,
                name
            );

        if (
            shouldExist &&
            node
        ) {
            return node;
        }

        if (
            !shouldExist &&
            !node
        ) {
            return null;
        }

        await page.waitForTimeout(
            200
        );
    }

    return await findNode(
        page,
        name
    );
}


async function visibleNodes(page) {

    const nodes =
        page.locator(
            '[data-test-id="canvas-node"]'
        );

    const count =
        await nodes.count();

    const result = [];

    for (
        let i = 0;
        i < count;
        i++
    ) {

        const node =
            nodes.nth(i);

        result.push({
            name:
                await node.getAttribute(
                    "data-node-name"
                ),

            type:
                await node.getAttribute(
                    "data-node-type"
                ),

            id:
                await node.getAttribute(
                    "data-id"
                )
        });
    }

    return result;
}


async function openNode(
    page,
    name
) {

    await ensureEditor(page);

    const node =
        await findNode(
            page,
            name
        );

    if (!node) {
        throw new Error(
            `Node not found on canvas: ${name}`
        );
    }

    await node.scrollIntoViewIfNeeded();

    await node.dblclick();

    console.log(
        `👁 Opened node: ${name}`
    );
}


async function selectNode(
    page,
    name
) {

    await ensureEditor(page);

    const node =
        await findNode(
            page,
            name
        );

    if (!node) {
        throw new Error(
            `Node not found: ${name}`
        );
    }

    await node.click();

    console.log(
        `👁 Selected: ${name}`
    );
}


async function executeNodeUI(
    page,
    name
) {

    await ensureEditor(page);

    const node =
        await findNode(
            page,
            name
        );

    if (!node) {
        throw new Error(
            `Node not found: ${name}`
        );
    }

    await node.click();

    const button =
        node.locator(
            '[data-test-id="execute-node-button"]'
        );

    await button.waitFor({
        state: "visible",
        timeout: 5000
    });

    await button.click();

    console.log(
        `▶ Executing node visually: ${name}`
    );
}


async function executeWorkflowUI(
    page
) {

    await ensureEditor(page);

    const button =
        page.locator(
            '[data-test-id="execute-workflow-button"]'
        ).first();

    await button.waitFor({
        state: "visible",
        timeout: 10000
    });

    await button.click();

    console.log(
        "▶ Workflow execution started through UI."
    );
}


async function openExecutions(page) {

    await closeNodePanel(page);

    const button =
        page.locator(
            '[data-test-id="radio-button-executions"]'
        );

    await button.click();

    console.log(
        "👁 Executions tab opened."
    );
}


async function zoomToFit(page) {

    await ensureEditor(page);

    const button =
        page.locator(
            '[data-test-id="zoom-to-fit"]'
        );

    if (await button.count()) {
        await button.click();
        console.log(
            "👁 Canvas fitted to screen."
        );
    }
}


// ============================================================
// INITIAL WORKFLOW
// ============================================================

const scheduleCode = `

const schedule = trigger({

    type:'n8n-nodes-base.scheduleTrigger',

    version:1.3,

    config:{
        name:'Schedule Trigger',
        parameters:{},
        position:[300,300]
    },

    output:[{}]

});


export default workflow(
    'mika-terminal-lab',
    'Mika Terminal Lab'
)
.add(schedule);

`;


// ============================================================
// HELP
// ============================================================

function help() {

    console.log(`

============================================================
 MIKA TERMINAL LAB
============================================================

FRONTEND / PLAYWRIGHT

  nodes
      Show nodes currently visible on canvas.

  open NODE NAME
      Open a node visually.

  close
      Close the currently open node panel.
      Example:
      open Code

  select NODE NAME
      Select a node visually.

  step NODE NAME
      Execute one node through the UI.

  execute
      Execute the whole workflow through the UI.

  editor
      Open Editor tab.

  executions
      Open Executions tab.

  fit
      Zoom canvas to fit.

  reload
      Reload the current n8n page.


MCP READ / DISCOVERY

  get
      Read current workflow through MCP.

  search QUERY
      Search n8n nodes.
      Example:
      search gmail

  type NODE_TYPE
      Get exact node type definition.
      Example:
      type n8n-nodes-base.code


MCP CREATE / UPDATE / DELETE

  add NODE_JSON

      Generic addNode. NO node-specific helper.

      Example:

      add {"name":"Code","type":"n8n-nodes-base.code","typeVersion":2,"position":[600,300],"parameters":{"jsCode":"return [{json:{message:'hello'}}];"}}


  remove NODE NAME

      Example:
      remove Code


  rename OLD NAME | NEW NAME

      Example:
      rename Code | My Code


  move NODE NAME | X | Y

      Example:
      move My Code | 800 | 300


  connect SOURCE | TARGET

      Example:
      connect Schedule Trigger | My Code


  disconnect SOURCE | TARGET

      Example:
      disconnect Schedule Trigger | My Code


  params NODE NAME | JSON

      Deep merge node parameters.

      Example:
      params My Code | {"jsCode":"return [{json:{changed:true}}];"}


  set NODE NAME | JSON_POINTER | JSON_VALUE

      Example:
      set My Code | /jsCode | "return [{json:{version:2}}];"


  disable NODE NAME

  enable NODE NAME


  op JSON

      Send ANY update_workflow operation or array
      of operations.

      Example:

      op {"type":"setWorkflowMetadata","name":"Terminal Test"}

      Example:

      op [{"type":"setNodePosition","nodeName":"My Code","position":[900,400]},{"type":"setNodeDisabled","nodeName":"My Code","disabled":false}]


WORKFLOW CRUD

  create FILE

      Create a completely new workflow from SDK code.

      Example:
      create ./workflow.js


  archive

      Archive current workflow.


RAW MCP

  call TOOL_NAME JSON

      Call ANY MCP tool directly.

      Example:
      call list_credentials {}

      Example:
      call search_workflows {"limit":5}


OTHER

  status
  help
  clear
  quit

============================================================

`);
}


// ============================================================
// MAIN
// ============================================================

async function main() {

    if (
        !process.env.N8N_MCP_TOKEN
    ) {
        console.warn(
            "⚠ N8N_MCP_TOKEN is not set."
        );
    }


    console.log(
        "Connecting to Chrome..."
    );

    const {
        page
    } =
        await connectBrowser();


    console.log(
        "Connecting to n8n MCP..."
    );

    const mcp =
        new N8nMCP();

    await mcp.connect();


    console.log(
        "Validating starter workflow..."
    );

    const validation =
        await mcp.callTool(
            "validate_workflow",
            {
                code: scheduleCode
            }
        );

    const validationData =
        unpack(validation);

    if (
        validationData &&
        validationData.valid === false
    ) {

        console.error(
            "Starter workflow validation failed:"
        );

        printResult(validation);

        return;
    }


    console.log(
        "Creating Mika Terminal Lab..."
    );

    const created =
        await mcp.createWorkflowSDK(
            scheduleCode
        );

    const createdData =
        unpack(created);

    if (
        !createdData ||
        !createdData.workflowId
    ) {

        console.error(
            "Could not create workflow:"
        );

        printResult(created);

        return;
    }


    let workflowId =
        createdData.workflowId;

    let workflowUrl =
        createdData.url;


    console.log(
        `Workflow: ${workflowId}`
    );


    await page.goto(
        workflowUrl,
        {
            waitUntil:
                "domcontentloaded"
        }
    );

    await page.locator(
        '[data-test-id="canvas"]'
    ).waitFor({
        state: "visible",
        timeout: 15000
    });


    await zoomToFit(page);


    // --------------------------------------------------------
    // MCP UPDATE HELPER
    // --------------------------------------------------------

    async function update(
        operations
    ) {

        if (
            !Array.isArray(
                operations
            )
        ) {
            operations = [
                operations
            ];
        }

        const result =
            await mcp.updateWorkflow(
                workflowId,
                operations
            );

        printResult(result);

        // Let the n8n frontend receive/render its live update.
        await page.waitForTimeout(
            700
        );

        return result;
    }


    // --------------------------------------------------------
    // TERMINAL
    // --------------------------------------------------------

    const rl =
        readline.createInterface({
            input,
            output
        });


    rl.on(
        "SIGINT",
        () => {

            console.log(
                "\nLeaving Mika Terminal Lab."
            );

            rl.close();

            process.exit(0);
        }
    );


    help();


    while (true) {

        let line;

        try {

            line =
                await rl.question(
                    "\nMika > "
                );

        } catch {

            break;
        }


        line =
            line.trim();


        if (!line) {
            continue;
        }


        const firstSpace =
            line.indexOf(" ");

        const command =
            (
                firstSpace === -1
                    ? line
                    : line.slice(
                        0,
                        firstSpace
                    )
            )
            .toLowerCase();

        const rest =
            firstSpace === -1
                ? ""
                : line
                    .slice(
                        firstSpace + 1
                    )
                    .trim();


        try {

            // ==================================================
            // BASIC
            // ==================================================

            if (
                command === "quit" ||
                command === "exit"
            ) {

                console.log(
                    "Bye."
                );

                rl.close();

                process.exit(0);
            }


            else if (
                command === "help"
            ) {

                help();
            }


            else if (
                command === "clear"
            ) {

                console.clear();
            }


            else if (
                command === "status"
            ) {

                console.log(
                    "Workflow:",
                    workflowId
                );

                console.log(
                    "URL:",
                    page.url()
                );

                console.log(
                    "Visible nodes:"
                );

                console.table(
                    await visibleNodes(
                        page
                    )
                );
            }


            // ==================================================
            // PLAYWRIGHT / FRONTEND
            // ==================================================

            else if (
                command === "nodes"
            ) {

                console.table(
                    await visibleNodes(
                        page
                    )
                );
            }


            else if (
                command === "close"
            ) {

                await closeNodePanel(
                    page,
                    true
                );
            }


            else if (
                command === "open"
            ) {

                await openNode(
                    page,
                    rest
                );
            }


            else if (
                command === "select"
            ) {

                await selectNode(
                    page,
                    rest
                );
            }


            else if (
                command === "step"
            ) {

                await executeNodeUI(
                    page,
                    rest
                );
            }


            else if (
                command === "execute"
            ) {

                await executeWorkflowUI(
                    page
                );
            }


            else if (
                command === "editor"
            ) {

                await ensureEditor(
                    page
                );

                console.log(
                    "👁 Editor opened."
                );
            }


            else if (
                command === "executions"
            ) {

                await openExecutions(
                    page
                );
            }


            else if (
                command === "fit"
            ) {

                await zoomToFit(
                    page
                );
            }


            else if (
                command === "reload"
            ) {

                await page.reload({
                    waitUntil:
                        "domcontentloaded"
                });

                console.log(
                    "🔄 Frontend reloaded."
                );
            }


            // ==================================================
            // MCP READ / DISCOVERY
            // ==================================================

            else if (
                command === "get"
            ) {

                const result =
                    await mcp.getWorkflow(
                        workflowId
                    );

                printResult(result);
            }


            else if (
                command === "search"
            ) {

                const result =
                    await mcp.callTool(
                        "search_nodes",
                        {
                            queries: [
                                rest
                            ]
                        }
                    );

                printResult(result);
            }


            else if (
                command === "type"
            ) {

                const result =
                    await mcp.callTool(
                        "get_node_types",
                        {
                            nodeIds: [
                                {
                                    nodeId:
                                        rest
                                }
                            ]
                        }
                    );

                printResult(result);
            }


            // ==================================================
            // GENERIC NODE CRUD
            // ==================================================

            else if (
                command === "add"
            ) {

                const node =
                    JSON.parse(
                        rest
                    );

                if (
                    !node.name ||
                    !node.type ||
                    node.typeVersion === undefined
                ) {

                    throw new Error(
                        "add requires node.name, node.type and node.typeVersion"
                    );
                }


                await update({
                    type:
                        "addNode",

                    node
                });


                const visible =
                    await waitForNode(
                        page,
                        node.name,
                        true,
                        5000
                    );


                if (visible) {

                    console.log(
                        `👁 Frontend now shows: ${node.name}`
                    );

                } else {

                    console.log(
                        "⚠ MCP update succeeded but frontend did not render it within 5s. Type: reload"
                    );
                }
            }


            else if (
                command === "remove"
            ) {

                const nodeName =
                    rest;

                await update({
                    type:
                        "removeNode",

                    nodeName
                });


                const stillThere =
                    await waitForNode(
                        page,
                        nodeName,
                        false,
                        5000
                    );


                if (!stillThere) {

                    console.log(
                        `👁 Frontend removed: ${nodeName}`
                    );
                }
            }


            else if (
                command === "rename"
            ) {

                const parts =
                    rest
                    .split("|")
                    .map(
                        x => x.trim()
                    );

                if (
                    parts.length !== 2
                ) {

                    throw new Error(
                        "Usage: rename OLD NAME | NEW NAME"
                    );
                }

                const [
                    oldName,
                    newName
                ] = parts;


                await update({
                    type:
                        "renameNode",

                    oldName,
                    newName
                });


                const visible =
                    await waitForNode(
                        page,
                        newName,
                        true,
                        5000
                    );


                if (visible) {

                    console.log(
                        `👁 Frontend renamed ${oldName} -> ${newName}`
                    );
                }
            }


            else if (
                command === "move"
            ) {

                const parts =
                    rest
                    .split("|")
                    .map(
                        x => x.trim()
                    );

                if (
                    parts.length !== 3
                ) {

                    throw new Error(
                        "Usage: move NODE NAME | X | Y"
                    );
                }

                const nodeName =
                    parts[0];

                const x =
                    Number(
                        parts[1]
                    );

                const y =
                    Number(
                        parts[2]
                    );

                if (
                    !Number.isFinite(x) ||
                    !Number.isFinite(y)
                ) {

                    throw new Error(
                        "X and Y must be numbers."
                    );
                }


                await update({
                    type:
                        "setNodePosition",

                    nodeName,

                    position: [
                        x,
                        y
                    ]
                });


                console.log(
                    `👁 Moved ${nodeName} -> [${x}, ${y}]`
                );
            }


            else if (
                command === "connect" ||
                command === "disconnect"
            ) {

                const parts =
                    rest
                    .split("|")
                    .map(
                        x => x.trim()
                    );

                if (
                    parts.length !== 2
                ) {

                    throw new Error(
                        `Usage: ${command} SOURCE | TARGET`
                    );
                }


                await update({

                    type:
                        command ===
                        "connect"
                            ? "addConnection"
                            : "removeConnection",

                    source:
                        parts[0],

                    target:
                        parts[1]
                });


                console.log(
                    `👁 ${command}: ${parts[0]} -> ${parts[1]}`
                );
            }


            else if (
                command === "params"
            ) {

                const separator =
                    rest.indexOf("|");

                if (
                    separator === -1
                ) {

                    throw new Error(
                        "Usage: params NODE NAME | JSON"
                    );
                }


                const nodeName =
                    rest
                    .slice(
                        0,
                        separator
                    )
                    .trim();

                const json =
                    rest
                    .slice(
                        separator + 1
                    )
                    .trim();


                const parameters =
                    JSON.parse(
                        json
                    );


                await update({
                    type:
                        "updateNodeParameters",

                    nodeName,

                    parameters
                });


                console.log(
                    `👁 Parameters updated: ${nodeName}`
                );
            }


            else if (
                command === "set"
            ) {

                const parts =
                    rest.split("|");

                if (
                    parts.length < 3
                ) {

                    throw new Error(
                        "Usage: set NODE NAME | /json/pointer | JSON_VALUE"
                    );
                }


                const nodeName =
                    parts
                    .shift()
                    .trim();

                const path =
                    parts
                    .shift()
                    .trim();

                const valueText =
                    parts
                    .join("|")
                    .trim();

                const value =
                    JSON.parse(
                        valueText
                    );


                await update({
                    type:
                        "setNodeParameter",

                    nodeName,
                    path,
                    value
                });


                console.log(
                    `👁 Parameter changed: ${nodeName} ${path}`
                );
            }


            else if (
                command === "disable" ||
                command === "enable"
            ) {

                await update({
                    type:
                        "setNodeDisabled",

                    nodeName:
                        rest,

                    disabled:
                        command ===
                        "disable"
                });


                console.log(
                    `👁 ${rest} ${
                        command ===
                        "disable"
                            ? "disabled"
                            : "enabled"
                    }`
                );
            }


            // ==================================================
            // RAW UPDATE OPERATIONS
            // ==================================================

            else if (
                command === "op"
            ) {

                const operations =
                    JSON.parse(
                        rest
                    );

                await update(
                    operations
                );
            }


            // ==================================================
            // WORKFLOW CREATE / ARCHIVE
            // ==================================================

            else if (
                command === "create"
            ) {

                if (
                    !fs.existsSync(
                        rest
                    )
                ) {

                    throw new Error(
                        `File not found: ${rest}`
                    );
                }


                const code =
                    fs.readFileSync(
                        rest,
                        "utf8"
                    );


                console.log(
                    "Validating workflow..."
                );


                const check =
                    await mcp.callTool(
                        "validate_workflow",
                        {
                            code
                        }
                    );


                const checkData =
                    unpack(check);


                printResult(check);


                if (
                    checkData &&
                    checkData.valid === false
                ) {

                    console.log(
                        "❌ Not creating invalid workflow."
                    );

                    continue;
                }


                const result =
                    await mcp.createWorkflowSDK(
                        code
                    );


                const data =
                    unpack(result);


                printResult(result);


                if (
                    data &&
                    data.workflowId &&
                    data.url
                ) {

                    workflowId =
                        data.workflowId;

                    workflowUrl =
                        data.url;


                    await page.goto(
                        workflowUrl,
                        {
                            waitUntil:
                                "domcontentloaded"
                        }
                    );


                    await page.locator(
                        '[data-test-id="canvas"]'
                    ).waitFor({
                        state:
                            "visible",

                        timeout:
                            15000
                    });


                    await zoomToFit(
                        page
                    );


                    console.log(
                        `👁 Now controlling workflow: ${workflowId}`
                    );
                }
            }


            else if (
                command === "archive"
            ) {

                const result =
                    await mcp.callTool(
                        "archive_workflow",
                        {
                            workflowId
                        }
                    );

                printResult(result);
            }


            // ==================================================
            // RAW MCP
            // ==================================================

            else if (
                command === "call"
            ) {

                const separator =
                    rest.indexOf(" ");

                let toolName;
                let json;


                if (
                    separator === -1
                ) {

                    toolName =
                        rest;

                    json =
                        {};

                } else {

                    toolName =
                        rest
                        .slice(
                            0,
                            separator
                        )
                        .trim();

                    const jsonText =
                        rest
                        .slice(
                            separator + 1
                        )
                        .trim();

                    json =
                        jsonText
                            ? JSON.parse(
                                jsonText
                            )
                            : {};
                }


                const result =
                    await mcp.callTool(
                        toolName,
                        json
                    );


                printResult(result);
            }


            else {

                console.log(
                    `Unknown command: ${command}`
                );

                console.log(
                    "Type: help"
                );
            }

        } catch (err) {

            console.error(
                "\n❌",
                err.message
            );
        }
    }
}


main()
.catch(
    err => {

        console.error(
            "\nFATAL ERROR:"
        );

        console.error(
            err
        );

        process.exit(1);
    }
);
