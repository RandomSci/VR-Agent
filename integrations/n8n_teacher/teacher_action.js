"use strict";

const { chromium } = require("playwright");

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

const {
  N8nController
} = require(
  "./reference/N8N_FUNCTIONS.js"
);


const MCP_URL =
  process.env.N8N_MCP_URL ||
  "https://myaisupport.app.n8n.cloud/mcp-server/http";

const CDP_URL =
  process.env.N8N_CDP_URL ||
  "http://127.0.0.1:9222";

const N8N_HOST =
  "myaisupport.app.n8n.cloud";


function bearerToken() {
  const token =
    process.env.N8N_MCP_TOKEN;

  if (!token) {
    throw new Error(
      "N8N_MCP_TOKEN is not available to the n8n teacher."
    );
  }

  return token.startsWith("Bearer ")
    ? token
    : `Bearer ${token}`;
}


function textFromMCP(result) {
  if (!result) {
    return "";
  }

  if (!Array.isArray(result.content)) {
    return "";
  }

  return result.content
    .filter(item => item && item.type === "text")
    .map(item => String(item.text || ""))
    .join("\n");
}


function assertMCP(result, action) {
  if (result && result.isError) {
    throw new Error(
      `${action} failed: ${textFromMCP(result)}`
    );
  }

  return result;
}


function extractWorkflowId(result) {
  const text =
    textFromMCP(result);

  if (text) {
    try {
      const parsed =
        JSON.parse(text);

      const id =
        parsed?.data?.workflowId ||
        parsed?.workflowId ||
        parsed?.workflow_id ||
        parsed?.data?.id ||
        parsed?.id;

      if (id) {
        return String(id);
      }
    } catch (_) {
      const match =
        text.match(
          /"workflowId"\s*:\s*"([^"]+)"/
        );

      if (match) {
        return match[1];
      }
    }
  }

  const structured =
    result?.structuredContent;

  const id =
    structured?.workflowId ||
    structured?.workflow_id ||
    structured?.data?.workflowId ||
    structured?.data?.id;

  return id
    ? String(id)
    : "";
}


async function connectMCP() {
  const client =
    new Client({
      name:
        "vr-agent-n8n-teacher-action",

      version:
        "0.2.0"
    });

  const transport =
    new StreamableHTTPClientTransport(
      new URL(MCP_URL),
      {
        requestInit: {
          headers: {
            Authorization:
              bearerToken()
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


async function connectBrowser() {
  const browser =
    await chromium.connectOverCDP(
      CDP_URL
    );

  const contexts =
    browser.contexts();

  if (!contexts.length) {
    throw new Error(
      "n8n Chrome has no browser context."
    );
  }

  let page = null;

  for (const context of contexts) {
    for (const candidate of context.pages()) {
      if (
        candidate
          .url()
          .includes(N8N_HOST)
      ) {
        page = candidate;
        break;
      }
    }

    if (page) {
      break;
    }
  }

  if (!page) {
    throw new Error(
      "No n8n page is open in Chrome :9222."
    );
  }

  return {
    browser,
    page
  };
}


async function createBeginnerWorkflow() {
  const {
    client
  } = await connectMCP();

  try {
    /*
     * This exact style of SDK workflow creation
     * has already been proven against this MCP.
     *
     * Start with one safe Schedule Trigger.
     */
    const code = `
import { workflow, trigger } from '@n8n/workflow-sdk';

const schedule = trigger({
  type: 'n8n-nodes-base.scheduleTrigger',
  version: 1.3,
  config: {
    name: 'Schedule Trigger',
    parameters: {},
    position: [300, 300]
  },
  output: [{}]
});

export default workflow(
  'vr-n8n-teaching-demo',
  'VR n8n Teaching Demo'
).add(schedule);
`;

    const created =
      assertMCP(
        await client.callTool({
          name:
            "create_workflow_from_code",

          arguments: {
            code
          }
        }),
        "create_workflow_from_code"
      );

    const workflowId =
      extractWorkflowId(
        created
      );

    if (!workflowId) {
      throw new Error(
        "MCP created a workflow but no workflow ID could be read."
      );
    }

    /*
     * Add the second node through the proven
     * update_workflow operation API.
     */
    const updated =
      assertMCP(
        await client.callTool({
          name:
            "update_workflow",

          arguments: {
            workflowId,

            operations: [
              {
                type:
                  "addNode",

                node: {
                  name:
                    "Code",

                  type:
                    "n8n-nodes-base.code",

                  typeVersion:
                    2,

                  position: [
                    600,
                    300
                  ],

                  parameters: {
                    jsCode:
                      "return [{json:{message:'Hello from your n8n lesson'}}];"
                  }
                }
              },

              {
                type:
                  "addConnection",

                source:
                  "Schedule Trigger",

                target:
                  "Code",

                sourceIndex:
                  0,

                targetIndex:
                  0,

                connectionType:
                  "main"
              }
            ]
          }
        }),
        "update_workflow"
      );

    return {
      workflowId,
      created,
      updated
    };

  } finally {
    try {
      await client.close();
    } catch (_) {
      // MCP cleanup only.
    }
  }
}


async function openAndVerifyWorkflow(
  workflowId,
  requireDemoNodes = false
) {
  const {
    page
  } = await connectBrowser();

  const url =
    `https://${N8N_HOST}/workflow/${workflowId}`;

  if (
    !page
      .url()
      .includes(`/workflow/${workflowId}`)
  ) {
    await page.goto(
      url,
      {
        waitUntil:
          "domcontentloaded",

        timeout:
          60000
      }
    );
  }

  await page.waitForSelector(
    '[data-test-id="canvas-node"]',
    {
      state:
        "visible",

      timeout:
        60000
    }
  );

  await page.waitForTimeout(
    1500
  );

  const controller =
    new N8nController(
      page,
      {
        teachingDelay:
          0
      }
    );

  try {
    await controller.perform(
      "zoomToFit"
    );
  } catch (_) {
    // Presentation improvement only.
  }

  await page.waitForTimeout(
    500
  );

  const nodes =
    await page
      .locator(
        '[data-test-id="canvas-node"]'
      )
      .evaluateAll(
        elements =>
          elements
            .map(
              element =>
                element.getAttribute(
                  "data-node-name"
                )
            )
            .filter(Boolean)
      );

  const connections =
    await page
      .locator(
        '[data-test-id="edge"]'
      )
      .count();

  let verified =
    nodes.length > 0;

  if (requireDemoNodes) {
    verified =
      nodes.includes(
        "Schedule Trigger"
      ) &&
      nodes.includes(
        "Code"
      ) &&
      connections >= 1;
  }

  if (!verified) {
    throw new Error(
      "The n8n canvas did not visually verify the expected workflow."
    );
  }

  return {
    url:
      page.url(),

    nodes,

    connections,

    verified
  };
}


async function demonstrate(
  requestedWorkflowId
) {
  let workflowId =
    String(
      requestedWorkflowId ||
      ""
    ).trim();

  let created =
    false;

  if (!workflowId) {
    const result =
      await createBeginnerWorkflow();

    workflowId =
      result.workflowId;

    created =
      true;
  }

  const visual =
    await openAndVerifyWorkflow(
      workflowId,
      created
    );

  return {
    ok:
      true,

    action:
      "demonstrate",

    workflow_id:
      workflowId,

    workflow_url:
      visual.url,

    ui_verified:
      visual.verified,

    verified_scope:
      "visible_workflow",

    nodes:
      visual.nodes,

    connections:
      visual.connections,

    summary:
      created
        ? (
          "Created a safe beginner n8n workflow and visibly verified " +
          "Schedule Trigger connected to Code."
        )
        : (
          "Opened the current lesson workflow and visibly verified " +
          "its n8n canvas."
        )
  };
}


async function testWorkflow(
  workflowId
) {
  workflowId =
    String(
      workflowId ||
      ""
    ).trim();

  if (!workflowId) {
    throw new Error(
      "There is no lesson workflow to test yet."
    );
  }

  const {
    page
  } = await connectBrowser();

  const url =
    `https://${N8N_HOST}/workflow/${workflowId}`;

  if (
    !page
      .url()
      .includes(`/workflow/${workflowId}`)
  ) {
    await page.goto(
      url,
      {
        waitUntil:
          "domcontentloaded",

        timeout:
          60000
      }
    );
  }

  await page.waitForSelector(
    '[data-test-id="canvas-node"]',
    {
      state:
        "visible",

      timeout:
        60000
    }
  );

  const controller =
    new N8nController(
      page,
      {
        teachingDelay:
          0
      }
    );

  /*
   * Important:
   * executeWorkflow verifies the visible click/start,
   * not final workflow success.
   */
  await controller.perform(
    "executeWorkflow"
  );

  await page.waitForTimeout(
    750
  );

  return {
    ok:
      true,

    action:
      "test_workflow",

    workflow_id:
      workflowId,

    workflow_url:
      page.url(),

    ui_verified:
      true,

    verified_scope:
      "execution_start",

    execution_started:
      true,

    execution_succeeded:
      null,

    summary:
      "Started the workflow execution visibly. Final execution success has not been verified yet."
  };
}


async function handle(request) {
  const action =
    String(
      request?.action ||
      ""
    );

  if (
    action ===
    "demonstrate"
  ) {
    return await demonstrate(
      request.workflow_id
    );
  }

  if (
    action ===
    "test_workflow"
  ) {
    return await testWorkflow(
      request.workflow_id
    );
  }

  throw new Error(
    `Unsupported N8nTeacher action: ${action}`
  );
}


async function main() {
  let request = {};

  try {
    request =
      JSON.parse(
        process.argv[2] ||
        "{}"
      );

    const result =
      await handle(
        request
      );

    process.stdout.write(
      JSON.stringify(
        result
      ) + "\n"
    );

    /*
     * Do not browser.close().
     * Chrome :9222 belongs to the teaching presentation.
     */
    setTimeout(
      () => process.exit(0),
      10
    );

  } catch (error) {
    process.stdout.write(
      JSON.stringify({
        ok:
          false,

        action:
          request?.action ||
          "",

        ui_verified:
          false,

        error:
          error?.message ||
          String(error),

        summary:
          "The requested n8n teaching action could not be verified."
      }) + "\n"
    );

    setTimeout(
      () => process.exit(1),
      10
    );
  }
}


main();
