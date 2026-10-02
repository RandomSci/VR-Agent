"use strict";

const { chromium } = require("playwright");

const {
  N8nController
} = require("./reference/N8N_FUNCTIONS.js");

const WORKFLOW_ID =
  "7n6xjYXVFZ9FcYtr";

const WORKFLOW_URL =
  `https://myaisupport.app.n8n.cloud/workflow/${WORKFLOW_ID}`;

const OVERLAY_URL =
  "http://127.0.0.1:12399/vr-agent/teaching-overlay.html?teacher=mika";


async function main() {
  console.log("");
  console.log("==========================================");
  console.log("      FINISHING EXISTING DEMO");
  console.log("==========================================");
  console.log("");

  // --------------------------------------------------
  // CONNECT TO THE EXISTING :9222 CHROME
  // --------------------------------------------------

  const browser =
    await chromium.connectOverCDP(
      "http://127.0.0.1:9222"
    );

  const context =
    browser.contexts()[0];

  if (!context) {
    throw new Error(
      "Chrome :9222 has no browser context."
    );
  }

  let page =
    context.pages().find(
      p =>
        p.url().includes(
          "myaisupport.app.n8n.cloud"
        )
    );

  if (!page) {
    throw new Error(
      "Could not find the n8n window."
    );
  }

  console.log("✓ Connected to n8n");


  // --------------------------------------------------
  // ALLOW OUR LOCAL DEV OVERLAY
  // --------------------------------------------------

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


  // --------------------------------------------------
  // OPEN THE WORKFLOW WE ALREADY CREATED
  // --------------------------------------------------

  if (
    !page.url().includes(
      WORKFLOW_ID
    )
  ) {
    await page.goto(
      WORKFLOW_URL,
      {
        waitUntil:
          "domcontentloaded",

        timeout:
          60000
      }
    );
  }

  console.log(
    "✓ Existing workflow opened"
  );


  // --------------------------------------------------
  // IMPORTANT FIX:
  //
  // Don't call waitForCanvasReady().
  // Wait using Playwright directly.
  // --------------------------------------------------

  console.log(
    "Waiting for actual n8n nodes..."
  );

  await page.waitForSelector(
    '[data-test-id="canvas-node"]',
    {
      state:
        "visible",

      timeout:
        30000
    }
  );

  await page.waitForTimeout(
    1500
  );

  console.log(
    "✓ n8n canvas loaded"
  );


  // --------------------------------------------------
  // VERIFY ACTUAL UI
  // --------------------------------------------------

  const nodes =
    await page
      .locator(
        '[data-test-id="canvas-node"]'
      )
      .evaluateAll(
        elements =>
          elements.map(
            el =>
              el.getAttribute(
                "data-node-name"
              )
          )
      );

  const connections =
    await page
      .locator(
        '[data-test-id="edge"]'
      )
      .count();


  console.log("");
  console.log(
    "===== ACTUAL N8N UI ====="
  );

  console.log(
    "Nodes:",
    nodes
  );

  console.log(
    "Connections:",
    connections
  );


  if (
    !nodes.includes(
      "Schedule Trigger"
    )
  ) {
    throw new Error(
      "Schedule Trigger is not visible."
    );
  }

  if (
    !nodes.includes(
      "Code"
    )
  ) {
    throw new Error(
      "Code is not visible."
    );
  }

  if (
    connections < 1
  ) {
    throw new Error(
      "Connection is not visible."
    );
  }


  console.log("");
  console.log(
    "✓ WORKFLOW VISUALLY VERIFIED"
  );


  // --------------------------------------------------
  // USE THE KNOWN-GOOD CONTROLLER
  // --------------------------------------------------

  const controller =
    new N8nController(
      page,
      {
        teachingDelay:
          350
      }
    );


  try {
    await controller.perform(
      "zoomToFit"
    );
  } catch (_) {}


  // --------------------------------------------------
  // INJECT MIKA OVER REAL N8N
  // --------------------------------------------------

  console.log("");
  console.log(
    "Adding Mika overlay..."
  );


  await page.evaluate(
    overlayUrl => {

      document
        .getElementById(
          "vr-teacher-overlay"
        )
        ?.remove();


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


      iframe.allow =
        "autoplay";


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
            "none",

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


  let overlayReady =
    false;


  try {

    await page.waitForFunction(
      () =>
        window.__vrTeacherReady ===
        true,
      {
        timeout:
          30000
      }
    );

    overlayReady =
      true;

    console.log(
      "✓ Mika reported READY"
    );

  } catch (_) {

    console.log(
      "⚠ Mika overlay did not report ready."
    );

  }


  // --------------------------------------------------
  // SHOW AN ACTUAL TEACHING ACTION
  // --------------------------------------------------

  console.log("");
  console.log(
    "Opening Code node..."
  );


  await controller.perform(
    "openNode",
    {
      name:
        "Code"
    }
  );


  console.log(
    "✓ Code node opened"
  );


  await page.waitForTimeout(
    2500
  );


  console.log(
    "Closing Code node..."
  );


  await controller.perform(
    "closeNode"
  );


  console.log(
    "✓ Code node closed"
  );


  try {
    await controller.perform(
      "zoomToFit"
    );
  } catch (_) {}


  // --------------------------------------------------
  // FINAL
  // --------------------------------------------------

  console.log("");
  console.log(
    "=========================================="
  );

  console.log(
    "             DEMO FINISHED"
  );

  console.log(
    "=========================================="
  );

  console.log(
    "Workflow: VERIFIED"
  );

  console.log(
    "Schedule Trigger: VERIFIED"
  );

  console.log(
    "Code: VERIFIED"
  );

  console.log(
    "Connection: VERIFIED"
  );

  console.log(
    "Playwright control: VERIFIED"
  );

  console.log(
    `Mika overlay: ${
      overlayReady
        ? "VERIFIED"
        : "NOT YET VERIFIED"
    }`
  );

  console.log("");
  console.log(
    "LOOK AT THE n8n WINDOW NOW."
  );

  console.log("");

  // DO NOT browser.close()
  // We are attached to the user's Chrome.
}


main().catch(
  error => {

    console.error("");
    console.error(
      "❌ FINISH FAILED"
    );

    console.error(
      error?.stack ||
      error
    );

    process.exit(1);

  }
);
