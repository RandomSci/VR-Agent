const { chromium } = require("playwright");
const http = require("http");

const CDP_URL = "http://127.0.0.1:9222";
const N8N_URL = "https://myaisupport.app.n8n.cloud/home/workflows";
const PORT = 9230;
const FPS = 6;

(async () => {
  console.log("Connecting to Chromium...");

  const browser = await chromium.connectOverCDP(CDP_URL);

  if (!browser.contexts().length) {
    throw new Error("No Chromium context found.");
  }

  const context = browser.contexts()[0];

  let page = context.pages().find((p) =>
    p.url().includes("myaisupport.app.n8n.cloud")
  );

  if (!page) {
    console.log("Opening n8n...");
    page = await context.newPage();

    await page.goto(N8N_URL, {
      waitUntil: "domcontentloaded",
      timeout: 60000,
    });
  }

  console.log("✓ Connected to:", page.url());

  const cdp = await context.newCDPSession(page);

  await cdp.send("Page.enable");

  let latestFrame = null;
  let capturing = false;
  const viewers = new Set();

  function sendFrame(res, frame) {
    if (!frame || res.destroyed || res.writableEnded) {
      return;
    }

    try {
      res.write("--frame\r\n");
      res.write("Content-Type: image/jpeg\r\n");
      res.write(`Content-Length: ${frame.length}\r\n\r\n`);
      res.write(frame);
      res.write("\r\n");
    } catch (_) {}
  }

  async function capture() {
    if (capturing) return;
    capturing = true;

    try {
      const shot = await cdp.send("Page.captureScreenshot", {
        format: "jpeg",
        quality: 82,
        fromSurface: true,
        captureBeyondViewport: false,
        optimizeForSpeed: true,
      });

      latestFrame = Buffer.from(shot.data, "base64");

      for (const viewer of viewers) {
        sendFrame(viewer, latestFrame);
      }
    } catch (error) {
      console.error("Capture error:", error.message);
    } finally {
      capturing = false;
    }
  }

  // Get a frame BEFORE declaring the server ready.
  await capture();

  if (!latestFrame || !latestFrame.length) {
    throw new Error("Chrome returned no n8n screenshot.");
  }

  console.log(
    `✓ First n8n frame captured (${latestFrame.length} bytes)`
  );

  setInterval(capture, Math.round(1000 / FPS));

  const server = http.createServer((req, res) => {
    if (req.url.startsWith("/frame.jpg")) {
      if (!latestFrame) {
        res.writeHead(503);
        res.end("Frame not ready");
        return;
      }

      res.writeHead(200, {
        "Content-Type": "image/jpeg",
        "Content-Length": latestFrame.length,
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Access-Control-Allow-Origin": "*",
      });

      res.end(latestFrame);
      return;
    }

    if (req.url === "/health") {
      res.writeHead(200, {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
      });

      res.end(JSON.stringify({
        ok: true,
        source: "cdp-screenshot",
        page: page.url(),
        frameReady: !!latestFrame,
        frameBytes: latestFrame ? latestFrame.length : 0,
      }));

      return;
    }

    res.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    });

    res.end(`<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>n8n CDP Feed Test</title>
  <style>
    html, body {
      margin: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #080b10;
    }

    #feed {
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
    }
  </style>
</head>
<body>
  <img id="feed">

  <script>
    const feed = document.getElementById("feed");

    function loadNext() {
      const next = new Image();

      next.onload = () => {
        feed.src = next.src;
        setTimeout(loadNext, 150);
      };

      next.onerror = () => {
        setTimeout(loadNext, 300);
      };

      next.src = "/frame.jpg?t=" + Date.now();
    }

    loadNext();
  </script>
</body>
</html>`);
  });

  server.listen(PORT, "127.0.0.1", () => {
    console.log("");
    console.log("========================================");
    console.log(" N8N CDP SCREENSHOT FEED READY");
    console.log("========================================");
    console.log(` http://127.0.0.1:${PORT}`);
    console.log(` ${FPS} FPS`);
    console.log("========================================");
  });

  process.on("SIGINT", () => process.exit(0));

})().catch((error) => {
  console.error("");
  console.error("CDP FEED FAILED:");
  console.error(error);
  process.exit(1);
});
