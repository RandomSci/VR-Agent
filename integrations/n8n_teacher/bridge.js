"use strict";

const { chromium } = require("playwright");

const {
  N8nController
} = require("./reference/N8N_FUNCTIONS.js");


const CDP_URL =
  process.env.N8N_CDP_URL ||
  "http://127.0.0.1:9222";

const N8N_HOST =
  "myaisupport.app.n8n.cloud";


async function connect() {
  const browser =
    await chromium.connectOverCDP(
      CDP_URL
    );

  const contexts =
    browser.contexts();

  if (!contexts.length) {
    throw new Error(
      "Chrome has no browser context."
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

    if (page) break;
  }

  if (!page) {
    throw new Error(
      "No open n8n page found in Chrome :9222."
    );
  }

  const controller =
    new N8nController(
      page,
      {
        teachingDelay: 0
      }
    );

  return {
    browser,
    page,
    controller
  };
}


async function safeCanvasState(controller) {
  try {
    return await controller.dumpCanvasState();
  } catch (_) {
    return null;
  }
}


async function status() {
  const {
    page,
    controller
  } = await connect();

  return {
    connected: true,

    title:
      await page.title(),

    url:
      page.url(),

    canvas:
      await safeCanvasState(
        controller
      )
  };
}


/*
 * IMPORTANT:
 *
 * Only visual/presentation operations are exposed here.
 *
 * Workflow construction/configuration will belong to MCP.
 * Playwright is the teacher's "eyes + visual hands".
 */
const VISUAL_ACTIONS =
  new Set([
    "waitForCanvas",

    "openEditor",
    "openExecutions",
    "openEvaluations",

    "openNode",
    "closeNode",
    "focusNode",

    "executeNode",
    "executeWorkflow",
    "stopExecution",
    "clearExecutionData",

    "zoomToFit",
    "zoomIn",
    "zoomOut",
    "focusPresentation",

    "dumpState",
    "dumpTestIds",
    "dumpButtons",
    "dumpInputs"
  ]);


async function visualAction(
  action,
  args = {}
) {
  if (
    !VISUAL_ACTIONS.has(action)
  ) {
    throw new Error(
      `Playwright action "${action}" is not allowed ` +
      "through the teaching bridge. " +
      "Workflow mutations belong to MCP."
    );
  }

  const {
    page,
    controller
  } = await connect();

  const before = {
    title:
      await page.title(),

    url:
      page.url(),

    canvas:
      await safeCanvasState(
        controller
      )
  };

  const result =
    await controller.perform(
      action,
      args
    );

  const after = {
    title:
      await page.title(),

    url:
      page.url(),

    canvas:
      await safeCanvasState(
        controller
      )
  };

  return {
    action,
    args,
    result:
      result ?? null,
    before,
    after
  };
}


function success(
  request,
  result
) {
  return {
    id:
      request.id ?? null,

    ok: true,

    source:
      "playwright",

    action:
      request.action || request.command,

    result,

    error: null
  };
}


function failure(
  request,
  error
) {
  return {
    id:
      request.id ?? null,

    ok: false,

    source:
      "playwright",

    action:
      request.action || request.command,

    result: null,

    error: {
      name:
        error?.name ||
        "Error",

      message:
        error?.message ||
        String(error)
    }
  };
}


async function handle(request) {
  switch (request.command) {

    case "ping":
      return {
        pong: true
      };

    case "status":
      return await status();

    case "visual":
      return await visualAction(
        request.action,
        request.args || {}
      );

    default:
      throw new Error(
        `Unknown bridge command: ${request.command}`
      );
  }
}


/*
 * ONE-SHOT MODE
 *
 * node bridge.js status
 */
async function oneShot() {
  const command =
    process.argv[2];

  if (!command) {
    return false;
  }

  let request;

  if (command === "status") {
    request = {
      id: "cli",
      command: "status"
    };

  } else if (
    command === "ping"
  ) {
    request = {
      id: "cli",
      command: "ping"
    };

  } else {
    throw new Error(
      `Unknown CLI command: ${command}`
    );
  }

  try {
    console.log(
      JSON.stringify(
        success(
          request,
          await handle(request)
        ),
        null,
        2
      )
    );

  } catch (error) {
    console.log(
      JSON.stringify(
        failure(
          request,
          error
        ),
        null,
        2
      )
    );

    process.exitCode = 1;
  }

  return true;
}


/*
 * PERSISTENT JSON-LINES MODE
 *
 * Later Python TeachingDirector can keep this process alive:
 *
 * {"id":"1","command":"status"}
 * {"id":"2","command":"visual","action":"openNode","args":{"name":"Code"}}
 *
 * One JSON result is returned for every request.
 */
async function persistent() {
  process.stdin.setEncoding(
    "utf8"
  );

  let buffer = "";

  process.stdin.on(
    "data",
    async chunk => {
      buffer += chunk;

      while (
        buffer.includes("\n")
      ) {
        const index =
          buffer.indexOf("\n");

        const line =
          buffer
            .slice(0, index)
            .trim();

        buffer =
          buffer.slice(
            index + 1
          );

        if (!line) {
          continue;
        }

        let request = {};

        try {
          request =
            JSON.parse(line);

          const result =
            await handle(
              request
            );

          process.stdout.write(
            JSON.stringify(
              success(
                request,
                result
              )
            ) + "\n"
          );

        } catch (error) {
          process.stdout.write(
            JSON.stringify(
              failure(
                request,
                error
              )
            ) + "\n"
          );
        }
      }
    }
  );
}


(async () => {
  const used =
    await oneShot();

  if (!used) {
    await persistent();
  }
})().catch(error => {
  console.error(
    JSON.stringify(
      {
        ok: false,
        source: "bridge",
        error: {
          name:
            error?.name ||
            "Error",

          message:
            error?.message ||
            String(error)
        }
      },
      null,
      2
    )
  );

  process.exit(1);
});
