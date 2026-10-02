"use strict";

const { chromium } = require("playwright");
const { N8nController } =
  require("./reference/N8N_FUNCTIONS.js");
const { N8nMCP } =
  require("./reference/n8n_mcp.js");

const payload = JSON.parse(process.argv[2] || "{}");

const SAFE_ACTIONS = new Set([
  "demonstrate",
  "build_workflow",
  "modify_workflow",
  "open_node",
  "close_node",
  "focus_node",
  "execute_node",
  "execute_workflow",
  "test_workflow",
  "open_editor",
  "open_executions",
  "zoom_to_fit"
]);

const BLOCKED_NODE_FRAGMENTS = [
  "executecommand",
  "ssh",
  "readwritefile",
  "localfiletrigger"
];

const NODE_TYPE_ALIASES = new Map([
  ["n8n-nodes-base.watchfornewfiles", "n8n-nodes-base.googleDriveTrigger"]
]);

function finish(data, code = 0) {
  process.stdout.write(JSON.stringify(data) + "\\n");
  setTimeout(() => process.exit(code), 10);
}

function mcpText(result) {
  if (!result) return "";

  if (result.structuredContent) {
    try {
      return JSON.stringify(result.structuredContent);
    } catch (_) {}
  }

  if (Array.isArray(result.content)) {
    return result.content
      .filter(x => x && x.type === "text")
      .map(x => x.text || "")
      .join("\n");
  }

  try {
    return JSON.stringify(result);
  } catch (_) {
    return String(result);
  }
}

function findWorkflowId(value) {
  if (!value) return "";

  if (typeof value === "object") {
    if (typeof value.workflowId === "string") {
      return value.workflowId;
    }

    if (typeof value.workflow_id === "string") {
      return value.workflow_id;
    }

    if (typeof value.url === "string") {
      const m = value.url.match(/\/workflow\/([^/?#]+)/);
      if (m) return m[1];
    }

    for (const child of Object.values(value)) {
      const found = findWorkflowId(child);
      if (found) return found;
    }

    return "";
  }

  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      const found = findWorkflowId(parsed);
      if (found) return found;
    } catch (_) {}

    const m = value.match(/\/workflow\/([^/?#]+)/);
    if (m) return m[1];

    const id =
      value.match(/"workflowId"\s*:\s*"([^"]+)"/);

    if (id) return id[1];
  }

  return "";
}

function cleanNode(raw, index) {
  const node = raw && typeof raw === "object" ? raw : {};

  const name =
    String(node.name || `Step ${index + 1}`).trim();

  const requestedType =
    String(node.type || "").trim();
  const type =
    NODE_TYPE_ALIASES.get(requestedType.toLowerCase()) || requestedType;

  if (!type) {
    throw new Error(
      `The teacher did not provide an n8n node type for "${name}".`
    );
  }

  const lowered = type.toLowerCase();

  if (
    BLOCKED_NODE_FRAGMENTS.some(
      fragment => lowered.includes(fragment)
    )
  ) {
    throw new Error(
      `Blocked unsafe teaching node type: ${type}`
    );
  }

  if (
    !type.startsWith("n8n-nodes-base.") &&
    !type.startsWith("@n8n/")
  ) {
    throw new Error(
      `Unsupported teaching node type: ${type}`
    );
  }

  let position = node.position;

  if (
    !Array.isArray(position) ||
    position.length !== 2
  ) {
    position = [
      300 + index * 320,
      300
    ];
  }

  return {
    name,
    type,
    typeVersion:
      Number(node.type_version || node.typeVersion || 1),
    parameters:
      node.parameters &&
      typeof node.parameters === "object"
        ? node.parameters
        : {},
    position: [
      Number(position[0]),
      Number(position[1])
    ]
  };
}

function cleanSpec(raw) {
  const spec =
    raw && typeof raw === "object" ? raw : {};

  const nodes = Array.isArray(spec.nodes)
    ? spec.nodes.slice(0, 10).map(cleanNode)
    : [];

  if (!nodes.length) {
    throw new Error(
      "The teacher requested a workflow build but supplied no node plan."
    );
  }

  const names = new Set(nodes.map(n => n.name));

  const connections =
    Array.isArray(spec.connections)
      ? spec.connections
          .slice(0, 15)
          .map(c => ({
            source: String(c.source || "").trim(),
            target: String(c.target || "").trim()
          }))
          .filter(
            c =>
              c.source &&
              c.target &&
              names.has(c.source) &&
              names.has(c.target)
          )
      : [];

  // If the teacher supplied nodes but omitted connections,
  // use a simple teaching-friendly linear chain.
  if (!connections.length && nodes.length > 1) {
    for (let i = 0; i < nodes.length - 1; i++) {
      connections.push({
        source: nodes[i].name,
        target: nodes[i + 1].name
      });
    }
  }

  return {
    name:
      String(
        spec.name ||
        payload.goal ||
        "VR Agent Teaching Workflow"
      ).slice(0, 100),

    nodes,
    connections,

    credentials_required:
      Boolean(spec.credentials_required)
  };
}

async function connectBrowser(workflowId = "") {
  const browser =
    await chromium.connectOverCDP(
      "http://127.0.0.1:9222"
    );

  const context = browser.contexts()[0];

  if (!context) {
    throw new Error(
      "Chrome :9222 has no browser context."
    );
  }

  const pages = context.pages();
  let page = workflowId
    ? pages.find(p => p.url().includes(`/workflow/${workflowId}`))
    : null;

  if (!page) {
    page = pages.find(p => p.url().includes("myaisupport.app.n8n.cloud"));
  }

  if (!page) {
    throw new Error(
      "The n8n browser page was not found."
    );
  }

  if (workflowId) {
    const wanted =
      `https://myaisupport.app.n8n.cloud/workflow/${workflowId}`;

    if (
      !page.url().includes(
        `/workflow/${workflowId}`
      )
    ) {
      await page.goto(wanted, {
        waitUntil: "domcontentloaded",
        timeout: 60000
      });
    }
  }

  const controller =
    new N8nController(page, {
      teachingDelay: 250
    });

  if (workflowId) {
    await page.waitForTimeout(1500);
  }

  return {
    browser,
    page,
    controller
  };
}

function resolveNode(names, requested) {
  const raw =
    String(requested || "")
      .replace(/\s+node$/i, "")
      .trim()
      .toLowerCase();

  if (!raw) return "";

  return (
    names.find(
      n => n.toLowerCase() === raw
    ) ||
    names.find(
      n => n.toLowerCase().includes(raw)
    ) ||
    names.find(
      n => raw.includes(n.toLowerCase())
    ) ||
    ""
  );
}

async function createFromSpec(spec) {
  const mcp = new N8nMCP();
  await mcp.connect();

  const first = spec.nodes[0];

  const constructor =
    first.type.toLowerCase().includes("trigger")
      ? "trigger"
      : "node";

  const firstConfig = {
    name: first.name,
    parameters: first.parameters,
    position: first.position
  };

  // Generated here from validated structured data.
  // The LLM does NOT provide arbitrary SDK code.
  const sdk = `
import { workflow, trigger, node } from '@n8n/workflow-sdk';

const first = ${constructor}({
  type: ${JSON.stringify(first.type)},
  version: ${JSON.stringify(first.typeVersion)},
  config: ${JSON.stringify(firstConfig)},
  output: [{}]
});

export default workflow(
  ${JSON.stringify(
    "vr-teaching-" + Date.now()
  )},
  ${JSON.stringify(spec.name)}
).add(first);
`;

  const created =
    await mcp.createWorkflowSDK(sdk);

  if (created?.isError) {
    throw new Error(
      "MCP could not create the teaching workflow: " +
      mcpText(created)
    );
  }

  const workflowId =
    findWorkflowId(created) ||
    findWorkflowId(mcpText(created));

  if (!workflowId) {
    throw new Error(
      "The workflow was created but its ID could not be resolved."
    );
  }

  const operations = [];

  for (const n of spec.nodes.slice(1)) {
    operations.push({
      type: "addNode",
      node: n
    });
  }

  for (const c of spec.connections) {
    operations.push({
      type: "addConnection",
      source: c.source,
      target: c.target,
      sourceIndex: 0,
      targetIndex: 0,
      connectionType: "main"
    });
  }

  operations.push({
    type: "setWorkflowMetadata",
    name: spec.name,
    description:
      "Interactive teaching workflow created by the VR n8n teacher."
  });

  if (operations.length) {
    const updated =
      await mcp.callTool(
        "update_workflow",
        {
          workflowId,
          versionName:
            "Built interactive teaching workflow",
          operations
        }
      );

    if (updated?.isError) {
      throw new Error(
        "MCP created the workflow but could not finish it: " +
        mcpText(updated)
      );
    }
  }

  return workflowId;
}

async function modifyFromSpec(workflowId, spec) {
  if (!workflowId) {
    return createFromSpec(spec);
  }

  const mcp = new N8nMCP();
  await mcp.connect();

  const operations = [];

  for (const n of spec.nodes) {
    operations.push({
      type: "addNode",
      node: n
    });
  }

  for (const c of spec.connections) {
    operations.push({
      type: "addConnection",
      source: c.source,
      target: c.target,
      sourceIndex: 0,
      targetIndex: 0,
      connectionType: "main"
    });
  }

  if (!operations.length) {
    throw new Error(
      "No safe workflow modifications were supplied."
    );
  }

  const result =
    await mcp.callTool(
      "update_workflow",
      {
        workflowId,
        versionName:
          "Teacher modified lesson workflow",
        operations
      }
    );

  if (result?.isError) {
    throw new Error(
      "MCP rejected the workflow modification: " +
      mcpText(result)
    );
  }

  return workflowId;
}

async function verifyWorkflow(
  workflowId,
  expectedNodes
) {
  const {
    page,
    controller
  } = await connectBrowser(workflowId);

  await page.reload({
    waitUntil: "domcontentloaded",
    timeout: 60000
  });

  await controller.waitForCanvasReady();

  try {
    await controller.zoomToFit();
  } catch (_) {}

  const visibleNodes =
    await controller.nodeNames();

  const missing =
    expectedNodes.filter(
      name => !visibleNodes.includes(name)
    );

  if (missing.length) {
    throw new Error(
      `Workflow changed on the backend, but these nodes were not visibly verified: ${missing.join(", ")}`
    );
  }

  return {
    page,
    controller,
    visibleNodes
  };
}

async function buildOrModify(action) {
  const spec =
    cleanSpec(payload.workflow_spec);

  let workflowId;

  if (action === "build_workflow") {
    // "Create" means create a clean workflow rather
    // than repeatedly modifying the old demo.
    workflowId =
      await createFromSpec(spec);
  } else {
    workflowId =
      await modifyFromSpec(
        String(payload.workflow_id || ""),
        spec
      );
  }

  const verified =
    await verifyWorkflow(
      workflowId,
      spec.nodes.map(n => n.name)
    );

  finish({
    ok: true,
    action,
    workflow_id: workflowId,
    workflow_url:
      `https://myaisupport.app.n8n.cloud/workflow/${workflowId}`,
    ui_verified: true,
    verified_scope: "workflow_structure",
    nodes: verified.visibleNodes,
    credentials_required:
      spec.credentials_required,
    summary:
      `Built and visibly verified "${spec.name}" with ${spec.nodes.length} planned node(s).`
  });
}

async function visualAction(action) {
  const workflowId =
    String(payload.workflow_id || "");

  if (!workflowId) {
    throw new Error(
      "There is no active lesson workflow yet."
    );
  }

  const {
    page,
    controller
  } = await connectBrowser(workflowId);

  const names =
    await controller.nodeNames();

  const target =
    resolveNode(
      names,
      payload.target
    );

  if (
    [
      "open_node",
      "focus_node",
      "execute_node"
    ].includes(action) &&
    !target
  ) {
    throw new Error(
      `I could not resolve that node. Visible nodes: ${names.join(", ")}`
    );
  }

  if (action === "open_node") {
    try {
      await controller.closeNode();
    } catch (_) {}

    await controller.openNode(target);

    finish({
      ok: true,
      action,
      target,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope: "node_editor_open",
      nodes: names,
      summary:
        `Opened the ${target} node.`
    });

    return;
  }

  if (action === "close_node") {
    await controller.closeNode();

    finish({
      ok: true,
      action,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope: "node_editor_closed",
      nodes: names,
      summary:
        "Closed the node editor."
    });

    return;
  }

  if (action === "focus_node") {
    await controller.focusNode(target);

    finish({
      ok: true,
      action,
      target,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope: "node_focused",
      nodes: names,
      summary:
        `Focused the ${target} node.`
    });

    return;
  }

  if (action === "execute_node") {
    await controller.executeNode(target);
    await page.waitForTimeout(1000);

    finish({
      ok: true,
      action,
      target,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope:
        "node_execution_started_visible",
      nodes: names,
      summary:
        `Started execution of the ${target} node visibly. Final success is not claimed unless n8n reports it.`
    });

    return;
  }

  if (
    action === "execute_workflow" ||
    action === "test_workflow"
  ) {
    await controller.executeWorkflow();
    await page.waitForTimeout(1000);

    finish({
      ok: true,
      action,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope:
        "workflow_execution_started_visible",
      nodes: names,
      summary:
        "Started the workflow execution visibly. Final success is not claimed unless n8n reports it."
    });

    return;
  }

  if (action === "open_editor") {
    await controller.openEditor();

    finish({
      ok: true,
      action,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope: "editor_tab",
      nodes: names,
      summary: "Opened the Editor."
    });

    return;
  }

  if (action === "open_executions") {
    await controller.openExecutions();

    finish({
      ok: true,
      action,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope: "executions_tab",
      nodes: names,
      summary: "Opened Executions."
    });

    return;
  }

  if (action === "zoom_to_fit") {
    await controller.zoomToFit();

    finish({
      ok: true,
      action,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope: "canvas",
      nodes: names,
      summary:
        "Fit the workflow into the visible canvas."
    });

    return;
  }

  if (action === "demonstrate") {
    await controller.zoomToFit();

    finish({
      ok: true,
      action,
      workflow_id: workflowId,
      ui_verified: true,
      verified_scope: "visible_workflow",
      nodes: names,
      summary:
        "Displayed the current lesson workflow."
    });

    return;
  }

  throw new Error(
    `Unhandled visual action: ${action}`
  );
}

async function main() {
  const action =
    String(payload.action || "")
      .trim()
      .toLowerCase();

  if (!SAFE_ACTIONS.has(action)) {
    throw new Error(
      `Blocked teaching action: ${action}`
    );
  }

  if (
    action === "build_workflow" ||
    action === "modify_workflow"
  ) {
    await buildOrModify(action);
    return;
  }

  // "Show me" at the beginning of a lesson should not
  // fail merely because a lesson workflow does not exist.
  // Create a tiny safe visual example first.
  if (
    action === "demonstrate" &&
    !String(payload.workflow_id || "")
  ) {
    payload.workflow_spec = {
      name: "n8n Beginner Demo",
      nodes: [
        {
          name: "Schedule Trigger",
          type: "n8n-nodes-base.scheduleTrigger",
          type_version: 1.3,
          parameters: {},
          position: [300, 300]
        },
        {
          name: "Code",
          type: "n8n-nodes-base.code",
          type_version: 2,
          parameters: {
            jsCode:
              "return [{json:{message:'Hello from n8n'}}];"
          },
          position: [650, 300]
        }
      ],
      connections: [
        {
          source: "Schedule Trigger",
          target: "Code"
        }
      ],
      credentials_required: false
    };

    await buildOrModify("build_workflow");
    return;
  }

  await visualAction(action);
}

main().catch(error => {
  finish({
    ok: false,
    action:
      String(payload.action || ""),
    target:
      String(payload.target || ""),
    workflow_id:
      String(payload.workflow_id || ""),
    ui_verified: false,
    error:
      error?.message || String(error),
    summary:
      "The requested n8n action could not be verified."
  }, 1);
});
