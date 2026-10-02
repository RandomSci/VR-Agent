"use strict";

const {
  chromium
} = require("playwright");

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


const TEACHER =
  process.argv[2] === "luna"
    ? "luna"
    : "mika";


const N8N_HOST =
  "myaisupport.app.n8n.cloud";

const WORKFLOWS_URL =
  "https://myaisupport.app.n8n.cloud/home/workflows";

const MCP_URL =
  "https://myaisupport.app.n8n.cloud/mcp-server/http";

const OVERLAY_URL =
  `http://127.0.0.1:12399/vr-agent/teaching-overlay.html?teacher=${TEACHER}`;


function token() {
  const value =
    process.env.N8N_MCP_TOKEN;

  if (!value) {
    throw new Error(
      "N8N_MCP_TOKEN is missing."
    );
  }

  return value.startsWith("Bearer ")
    ? value
    : `Bearer ${value}`;
}


function mcpText(result) {
  if (!result) {
    return "";
  }

  if (!Array.isArray(result.content)) {
    return JSON.stringify(result);
  }

  return result.content
    .filter(
      item =>
        item.type === "text"
    )
    .map(
      item =>
        item.text
    )
    .join("\n");
}


function assertMcpSuccess(
  result,
  action
) {
  if (
    !result ||
    result.isError
  ) {
    throw new Error(
      `${action} failed:\n${mcpText(result)}`
    );
  }

  /*
   * Some MCP servers may encode an
   * error inside text while still
   * completing the transport call.
   */
  const text =
    mcpText(result);

  if (
    /input validation error/i.test(text)
  ) {
    throw new Error(
      `${action} failed:\n${text}`
    );
  }

  return result;
}


async function connectPage() {
  const browser =
    await chromium.connectOverCDP(
      "http://127.0.0.1:9222"
    );

  const contexts =
    browser.contexts();

  if (!contexts.length) {
    throw new Error(
      "No Chrome context on :9222"
    );
  }

  const context =
    contexts[0];

  let page =
    context.pages().find(
      p =>
        p.url().includes(
          N8N_HOST
        )
    );

  if (!page) {
    page =
      await context.newPage();
  }

  /*
   * We are deliberately injecting our
   * LOCAL transparent teacher overlay
   * into the n8n page.
   */
  const cdp =
    await context.newCDPSession(
      page
    );

  await cdp.send(
    "Page.setBypassCSP",
    {
      enabled: true
    }
  );

  return {
    browser,
    page
  };
}


async function connectMcp() {
  const client =
    new Client({
      name:
        "vr-agent-n8n-teacher-demo",

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
              token()
          }
        }
      }
    );

  await client.connect(
    transport
  );

  return client;
}


async function removeOverlay(
  page
) {
  await page.evaluate(() => {
    document
      .getElementById(
        "vr-teacher-overlay"
      )
      ?.remove();

    delete window
      .__vrTeacherReady;
  });
}


async function injectOverlay(
  page
) {
  await removeOverlay(
    page
  );

  await page.evaluate(
    overlayUrl => {
      window.__vrTeacherReady =
        false;

      window.addEventListener(
        "message",
        event => {
          if (
            event.data?.type ===
            "vr-teacher-ready"
          ) {
            window.__vrTeacherReady =
              true;

            window.__vrTeacher =
              event.data.teacher;
          }
        }
      );

      const iframe =
        document.createElement(
          "iframe"
        );

      iframe.id =
        "vr-teacher-overlay";

      iframe.src =
        overlayUrl;

      iframe.setAttribute(
        "allow",
        "autoplay"
      );

      Object.assign(
        iframe.style,
        {
          position:
            "fixed",

          inset:
            "0",

          width:
            "100vw",

          height:
            "100vh",

          border:
            "0",

          margin:
            "0",

          padding:
            "0",

          background:
            "transparent",

          pointerEvents:
            "none",

          zIndex:
            "2147483647"
        }
      );

      document.body.appendChild(
        iframe
      );
    },
    OVERLAY_URL
  );

  try {
    await page.waitForFunction(
      () =>
        window.__vrTeacherReady ===
        true,
      {
        timeout: 30000
      }
    );

    console.log(
      `✓ ${TEACHER} overlay ready`
    );

    return true;

  } catch (_) {
    console.log(
      "⚠ Overlay iframe was injected but did not report ready."
    );

    return false;
  }
}


async function main() {
  let mcp = null;

  console.log("");
  console.log(
    "=========================================="
  );

  console.log(
    "       VR AGENT n8n TEACHING DEMO"
  );

  console.log(
    "=========================================="
  );

  console.log(
    `Teacher: ${TEACHER}`
  );

  console.log("");

  const {
    page
  } =
    await connectPage();

  console.log(
    "✓ Playwright connected to real n8n"
  );


  // ----------------------------------------------------------
  // MCP CREATES THE WORKFLOW FIRST
  //
  // IMPORTANT:
  // The previous version created a blank workflow visually,
  // then immediately asked MCP to mutate it.
  //
  // That workflow had not been persisted for MCP yet.
  //
  // Correct architecture:
  //
  // MCP CREATE
  //      ↓
  // MCP UPDATE
  //      ↓
  // PLAYWRIGHT OPENS REAL WORKFLOW
  //      ↓
  // VERIFY UI
  //      ↓
  // MIKA/LUNA OVERLAY
  // ----------------------------------------------------------

  console.log("");
  console.log(
    "Connecting n8n MCP..."
  );

  mcp =
    await connectMcp();

  console.log(
    "✓ MCP connected"
  );


  // ----------------------------------------------------------
  // PROVEN SDK WORKFLOW CREATION
  // ----------------------------------------------------------

  console.log("");
  console.log(
    "MCP creating lesson workflow..."
  );

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
'vr-agent-teaching-demo',
'VR Agent Teaching Demo'
)
.add(schedule);
`;


  const created =
    await mcp.callTool({
      name:
        "create_workflow_from_code",

      arguments: {
        code:
          scheduleCode
      }
    });


  assertMcpSuccess(
    created,
    "create_workflow_from_code"
  );


  let createdData = null;

  /*
   * Known-good MCP response:
   * content[0].text is JSON containing workflowId.
   *
   * Also support structuredContent in case
   * the MCP server changes representation.
   */
  if (
    created.structuredContent &&
    typeof created.structuredContent === "object"
  ) {
    createdData =
      created.structuredContent;
  }

  if (
    !createdData ||
    !(
      createdData.workflowId ||
      createdData.id
    )
  ) {
    try {
      createdData =
        JSON.parse(
          mcpText(created)
        );
    } catch (_) {}
  }


  const workflowId =
    createdData?.workflowId ||
    createdData?.id;


  if (!workflowId) {
    throw new Error(
      "MCP created a workflow but returned no workflowId:\n" +
      mcpText(created)
    );
  }


  console.log(
    "✓ MCP created workflow:",
    workflowId
  );


  // ----------------------------------------------------------
  // MCP ADDS CODE + CONNECTION
  // ----------------------------------------------------------

  console.log("");
  console.log(
    "MCP adding Code node..."
  );

  const update =
    await mcp.callTool({
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
                650,
                300
              ],

              parameters: {
                jsCode:
                  "return [{ json: { message: 'Hello from Mika and Luna!' } }];"
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
    });


  assertMcpSuccess(
    update,
    "update_workflow"
  );


  console.log(
    "✓ MCP added Code + connection"
  );


  // ----------------------------------------------------------
  // PLAYWRIGHT OPENS THE MCP-CREATED WORKFLOW
  // ----------------------------------------------------------

  const workflowUrl =
    `https://${N8N_HOST}/workflow/${workflowId}`;


  console.log("");
  console.log(
    "Opening MCP workflow in real n8n..."
  );

  await page.goto(
    workflowUrl,
    {
      waitUntil:
        "domcontentloaded",

      timeout:
        60000
    }
  );


  console.log(
    "✓ Real n8n editor opened"
  );


  // ----------------------------------------------------------
  // RELOAD REAL N8N UI
  // ----------------------------------------------------------

  console.log("");
  console.log(
    "Refreshing real n8n canvas..."
  );

  await page.reload({
    waitUntil:
      "domcontentloaded",

    timeout:
      60000
  });

  const controller =
    new N8nController(
      page,
      {
        teachingDelay:
          350
      }
    );

  await controller
    .waitForCanvasReady(
      30000
    );

  await page.waitForTimeout(
    1500
  );

  await controller
    .zoomToFit()
    .catch(() => {});


  // ----------------------------------------------------------
  // VERIFY WHAT IS ACTUALLY ON SCREEN
  // ----------------------------------------------------------

  const state =
    await controller
      .dumpCanvasState();

  console.log("");
  console.log(
    "===== VERIFIED CANVAS ====="
  );

  console.log(
    JSON.stringify(
      state,
      null,
      2
    )
  );

  const required =
    [
      "Schedule Trigger",
      "Code"
    ];

  for (
    const name
    of required
  ) {
    if (
      !state.nodes.includes(
        name
      )
    ) {
      throw new Error(
        `UI verification failed: ${name} is not visible`
      );
    }
  }

  if (
    state.connectionCount < 1
  ) {
    throw new Error(
      "UI verification failed: connection is missing"
    );
  }

  console.log(
    "✓ n8n UI verification passed"
  );


  // ----------------------------------------------------------
  // PUT MIKA/LUNA OVER THE REAL N8N PAGE
  // ----------------------------------------------------------

  console.log("");
  console.log(
    `Putting ${TEACHER} over n8n...`
  );

  const overlayReady =
    await injectOverlay(
      page
    );


  // ----------------------------------------------------------
  // VISIBLE TEACHING ACTION
  // ----------------------------------------------------------

  console.log("");
  console.log(
    "Opening Code node visibly..."
  );

  await controller.perform(
    "openNode",
    {
      name:
        "Code"
    }
  );

  await page.waitForTimeout(
    2200
  );

  await controller.perform(
    "closeNode"
  );

  await page.waitForTimeout(
    700
  );

  await controller
    .zoomToFit()
    .catch(() => {});


  console.log("");
  console.log(
    "=========================================="
  );

  console.log(
    "              RESULT READY"
  );

  console.log(
    "=========================================="
  );

  console.log(
    `Teacher: ${TEACHER}`
  );

  console.log(
    "Workflow: Schedule Trigger -> Code"
  );

  console.log(
    "MCP build: VERIFIED"
  );

  console.log(
    "Playwright UI: VERIFIED"
  );

  console.log(
    `VR overlay: ${
      overlayReady
        ? "VERIFIED"
        : "CHECK WINDOW"
    }`
  );

  console.log("");
  console.log(
    "LOOK AT THE n8n CHROME WINDOW."
  );

  console.log(
    "Do not close it."
  );

  console.log("");

  /*
   * IMPORTANT:
   * We connected to existing Chrome.
   * Do NOT close the browser.
   */

  if (mcp) {
    try {
      await mcp.close();
    } catch (_) {}
  }
}


main().catch(
  error => {
    console.error("");
    console.error(
      "❌ DEMO FAILED"
    );

    console.error(
      error?.stack ||
      error
    );

    process.exit(1);
  }
);
