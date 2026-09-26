// VR Agent: YouTube live chat observer.
//
// Injected by youtube_playwright_chat.py into https://www.youtube.com/live_chat.
// Selectors come from a real captured live chat DOM:
//
//   yt-live-chat-item-list-renderer > #item-offset > #items
//     yt-live-chat-placeholder-item-renderer        (skipped, no id, no text)
//     yt-live-chat-viewer-engagement-message-renderer (skipped, system notice)
//     yt-live-chat-text-message-renderer#<unique id>
//       #timestamp, yt-live-chat-author-chip #author-name, #message
//       #message contains text nodes and <img class="emoji" alt="😂">
//
// New renderers are picked up with a MutationObserver on #items. Nothing is
// scraped repeatedly. Messages already present when the observer attaches are
// marked as seen and not emitted, so old chat history is never answered.
// Extracted values are plain strings only; no HTML leaves the page.
//
// Calling this script again is safe: it returns a heartbeat and re-attaches if
// YouTube replaced the #items node (mode switch, reconnect).
(() => {
  if (window.__vrAgentChat) {
    return window.__vrAgentChat.heartbeat();
  }

  const SUPPORTED = {
    "YT-LIVE-CHAT-TEXT-MESSAGE-RENDERER": "text",
    "YT-LIVE-CHAT-PAID-MESSAGE-RENDERER": "paid",
  };
  const LIST_SELECTORS = [
    "yt-live-chat-item-list-renderer #items",
    "#item-list #items",
    "#items.yt-live-chat-item-list-renderer",
    "#chat #items",
  ];
  const MAX_SEEN = 4000;
  const MAX_TEXT = 500;
  const EMIT_DELAY_MS = 40;
  const RETRY_DELAYS_MS = [60, 200, 600];

  const seen = new Set();
  const seenOrder = [];
  let listEl = null;
  let listSelector = null;
  let observer = null;
  let pending = [];
  let emitTimer = null;
  let emitted = 0;
  let lastEmitAt = 0;
  let attachCount = 0;

  function remember(id) {
    if (seen.has(id)) return false;
    seen.add(id);
    seenOrder.push(id);
    if (seenOrder.length > MAX_SEEN) {
      seen.delete(seenOrder.shift());
    }
    return true;
  }

  function collapse(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  // Text with emoji preserved: Unicode emoji use their alt (the emoji
  // itself); channel custom emoji use their :shortcode:.
  function textOf(node) {
    if (!node) return "";
    let out = "";
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        out += child.data;
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        if (child.tagName === "IMG") {
          const alt = child.getAttribute("alt") || "";
          const tip = child.getAttribute("shared-tooltip-text") || "";
          const isCustom = child.hasAttribute("data-emoji-id") || /^[\w-]+$/.test(alt);
          out += isCustom ? (tip || (alt ? `:${alt}:` : "")) : alt || tip;
        } else {
          out += textOf(child);
        }
      }
    }
    return out;
  }

  function authorName(el) {
    const nameEl = el.querySelector("#author-name");
    if (!nameEl) return "";
    // #author-name holds the handle text plus a nested #chip-badges span.
    let own = "";
    for (const child of nameEl.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) own += child.data;
    }
    return collapse(own) || collapse(nameEl.textContent);
  }

  function isDeleted(el) {
    if (el.hasAttribute("is-deleted")) return true;
    const deleted = el.querySelector("#deleted-state");
    return !!(deleted && collapse(deleted.textContent));
  }

  function extract(el) {
    const kind = SUPPORTED[el.tagName];
    if (!kind) return null;
    const id = el.id || el.getAttribute("id") || "";
    if (!id) return null;
    if (isDeleted(el)) return { skip: true, id };
    const text = collapse(textOf(el.querySelector("#message"))).slice(0, MAX_TEXT);
    const author = authorName(el);
    const amountEl = el.querySelector("#purchase-amount, #purchase-amount-chip");
    return {
      id,
      kind,
      author,
      author_type: el.getAttribute("author-type") || "",
      text,
      timestamp_text: collapse((el.querySelector("#timestamp") || {}).textContent || ""),
      amount: amountEl ? collapse(amountEl.textContent) : "",
    };
  }

  function queue(item) {
    pending.push(item);
    if (!emitTimer) {
      emitTimer = setTimeout(flush, EMIT_DELAY_MS);
    }
  }

  function flush() {
    emitTimer = null;
    if (!pending.length) return;
    const batch = pending;
    pending = [];
    emitted += batch.length;
    lastEmitAt = Date.now();
    try {
      window.__vrAgentEmit(batch);
    } catch (err) {
      // The binding disappears if the page is being torn down.
    }
  }

  // Polymer may stamp the message text a tick after the node is inserted.
  function handleRenderer(el, attempt) {
    const data = extract(el);
    if (!data) return;
    if (seen.has(data.id)) return;
    if (data.skip) {
      remember(data.id);
      return;
    }
    if (!data.text && !data.amount && attempt < RETRY_DELAYS_MS.length) {
      setTimeout(() => handleRenderer(el, attempt + 1), RETRY_DELAYS_MS[attempt]);
      return;
    }
    if (!remember(data.id)) return;
    if (data.text || data.amount) queue(data);
  }

  function onMutations(mutations) {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        if (SUPPORTED[node.tagName]) {
          handleRenderer(node, 0);
        }
      }
    }
  }

  function findList() {
    for (const selector of LIST_SELECTORS) {
      const el = document.querySelector(selector);
      if (el) return [el, selector];
    }
    return [null, null];
  }

  function attach() {
    const [el, selector] = findList();
    if (!el) return false;
    if (observer) observer.disconnect();
    listEl = el;
    listSelector = selector;
    // Backlog present at attach time is history, not new chat.
    for (const child of el.children) {
      const id = SUPPORTED[child.tagName] && child.id;
      if (id) remember(id);
    }
    observer = new MutationObserver(onMutations);
    observer.observe(el, { childList: true });
    attachCount += 1;
    return true;
  }

  function endedHint() {
    if (document.querySelector("yt-live-chat-renderer") && listEl && listEl.isConnected) {
      return "";
    }
    const text = collapse((document.body && document.body.innerText) || "").slice(0, 2000);
    const match = text.match(
      /(chat is disabled|live chat is disabled|chat was turned off|this live stream (?:has )?ended|live chat replay|chat is unavailable)/i,
    );
    return match ? match[1] : "";
  }

  function heartbeat() {
    let reattached = false;
    if (!listEl || !listEl.isConnected) {
      reattached = attach();
    }
    return {
      attached: !!(listEl && listEl.isConnected),
      reattached,
      selector: listSelector,
      attach_count: attachCount,
      seen: seen.size,
      emitted,
      last_emit_at: lastEmitAt,
      has_chat_renderer: !!document.querySelector("yt-live-chat-renderer"),
      ended_hint: endedHint(),
      url: location.href,
    };
  }

  window.__vrAgentChat = { heartbeat, attach, extract, textOf };
  attach();
  return heartbeat();
})();
