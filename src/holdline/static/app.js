/* HoldLine customer page (index.html).
 *
 * Two jobs:
 *   1. "The call": push-to-talk (browser speech recognition), a typed fallback,
 *      a transcript, and spoken replies through one speak() function.
 *   2. "The glass box": polls GET /api/dashboard every 2 s and shows stock,
 *      holds and the event log. It reads the EVENT LOG, not the chat response,
 *      so tool calls made during phone calls show up here too.
 *
 * Plain browser JavaScript. No framework, no build step. Wrapped in an IIFE so
 * nothing leaks onto window.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------
  // Constants and page state
  // ------------------------------------------------------------------
  const SESSION_ID = crypto.randomUUID(); // one conversation per page load
  const POLL_MS = 2000;                   // dashboard poll interval
  const MAX_EVENTS = 200;                 // cap on the in-memory event list

  // Grab every element the script touches once, up front.
  const $ = (id) => document.getElementById(id);
  const els = {
    ptt: $("ptt-button"),
    voiceWarning: $("voice-warning"),
    voiceStatus: $("voice-status"),
    muteToggle: $("mute-toggle"),
    transcript: $("transcript"),
    chatForm: $("chat-form"),
    chatInput: $("chat-input"),
    sendButton: $("send-button"),
    offline: $("offline-indicator"),
    locationName: $("location-name"),
    locationHours: $("location-hours"),
    staleBanner: $("stale-banner"),
    stockBody: $("stock-body"),
    holdsList: $("holds-list"),
    eventsList: $("events-list"),
    btnRush: $("btn-rush"),
    btnStale: $("btn-stale"),
    btnResync: $("btn-resync"),
    btnReset: $("btn-reset"),
    demoStatus: $("demo-status"),
  };

  let muted = false;          // true = do not speak replies aloud
  let sending = false;        // true while a /api/chat request is in flight
  let lastEventId = 0;        // highest event id we have seen (for ?after_id=)
  const seenEventIds = new Set();
  let holds = [];             // latest holds from the dashboard

  // ------------------------------------------------------------------
  // Small DOM / formatting helpers
  // ------------------------------------------------------------------

  /** Create an element with a class and optional text content. */
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /** One-line JSON for the glass box. Never throws. */
  function compactJson(value) {
    try { return JSON.stringify(value); } catch (err) { return String(value); }
  }

  /** "14:03:27" in the viewer's local time zone, from an ISO 8601 UTC string. */
  function formatTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleTimeString([], { hour12: false });
  }

  /** "mm:ss" remaining until an ISO timestamp, or "expired". */
  function countdown(iso) {
    const ms = new Date(iso) - Date.now();
    if (isNaN(ms)) return "";
    if (ms <= 0) return "expired";
    const total = Math.floor(ms / 1000);
    const m = String(Math.floor(total / 60)).padStart(2, "0");
    const s = String(total % 60).padStart(2, "0");
    return m + ":" + s;
  }

  function setOffline(isOffline) { els.offline.hidden = !isOffline; }

  /** Replace all children of a list with the given nodes (or an "empty" row). */
  function fill(list, nodes, emptyText) {
    list.replaceChildren();
    if (nodes.length === 0) list.append(el("li", "empty", emptyText));
    else nodes.forEach((n) => list.append(n));
  }

  /** POST JSON and return the parsed response. Throws on HTTP errors. */
  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error("HTTP " + res.status + " from " + url);
    return res.json();
  }

  // ------------------------------------------------------------------
  // Speech output. ONE function so a better TTS can be swapped in later.
  // ------------------------------------------------------------------
  function speak(text) {
    if (muted || !text) return;
    // No-op when the browser has no speech synthesis.
    if (!("speechSynthesis" in window) || typeof SpeechSynthesisUtterance === "undefined") return;
    try {
      window.speechSynthesis.cancel(); // stop any earlier reply still playing
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "en-US";
      window.speechSynthesis.speak(utterance);
    } catch (err) {
      console.warn("speak() failed", err);
    }
  }

  els.muteToggle.addEventListener("click", () => {
    muted = !muted;
    els.muteToggle.setAttribute("aria-pressed", String(muted));
    els.muteToggle.textContent = muted ? "Unmute spoken replies" : "Mute spoken replies";
    if (muted && "speechSynthesis" in window) window.speechSynthesis.cancel();
  });

  // ------------------------------------------------------------------
  // Transcript bubbles
  // ------------------------------------------------------------------

  /**
   * Append a bubble. role is "caller" or "assistant". meta is the /api/chat
   * response (optional) and drives the guardrail chips.
   */
  function addBubble(role, text, meta) {
    const li = el("li", "bubble bubble-" + role);
    li.append(el("div", "bubble-who", role === "caller" ? "Caller" : "Assistant"));
    li.append(el("div", "bubble-text", text));

    if (meta) {
      const row = el("div", "bubble-meta");
      if (meta.blocked_tier === 2) row.append(el("span", "chip chip-red", "Escalated to staff"));
      else if (meta.blocked_tier === 1) row.append(el("span", "chip chip-amber", "Rewritten by guardrail"));
      if (meta.retried) row.append(el("span", "note", "retried"));
      if (row.childNodes.length) li.append(row);
    }

    els.transcript.append(li);
    els.transcript.scrollTop = els.transcript.scrollHeight; // keep newest visible
    return li;
  }

  // ------------------------------------------------------------------
  // Sending a message to the assistant
  // ------------------------------------------------------------------
  async function sendMessage(rawText) {
    const message = (rawText || "").trim();
    if (!message || sending) return;
    sending = true;
    els.sendButton.disabled = true;

    addBubble("caller", message);
    const pending = addBubble("assistant", "Thinking...");
    pending.classList.add("pending");

    try {
      const data = await postJson("/api/chat", { session_id: SESSION_ID, message, channel: "browser" });
      pending.remove();
      addBubble("assistant", data.reply, data);
      speak(data.reply);
      pollDashboard(); // show the tool calls right away instead of waiting 2 s
    } catch (err) {
      pending.remove();
      const bubble = addBubble("assistant", "Sorry, the assistant could not be reached. Please try again.");
      bubble.classList.add("error");
      console.error("/api/chat failed", err);
    } finally {
      sending = false;
      els.sendButton.disabled = false;
    }
  }

  els.chatForm.addEventListener("submit", (event) => {
    event.preventDefault(); // stay on the page; no full form post
    const text = els.chatInput.value;
    els.chatInput.value = "";
    sendMessage(text);
  });

  // ------------------------------------------------------------------
  // Speech input: push-to-talk
  // ------------------------------------------------------------------
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  let listening = false;
  let heardText = "";   // final transcript collected during this press

  function setVoiceStatus(text) { els.voiceStatus.textContent = text || ""; }

  if (!Recognition) {
    // Firefox and Safari: keep the typed fallback, say why the button is off.
    els.voiceWarning.hidden = false;
    els.ptt.disabled = true;
  } else {
    recognition = new Recognition();
    recognition.lang = "en-US";
    recognition.continuous = true;      // keep listening until we call stop()
    recognition.interimResults = true;  // show words as they are recognised

    recognition.addEventListener("result", (event) => {
      let finals = "";
      let interim = "";
      for (const result of event.results) {
        if (result.isFinal) finals += result[0].transcript + " ";
        else interim += result[0].transcript + " ";
      }
      heardText = (finals + interim).trim();
      setVoiceStatus("Heard: " + heardText);
    });

    recognition.addEventListener("error", (event) => {
      // "no-speech" and "aborted" are normal when the button is released early.
      if (event.error !== "no-speech" && event.error !== "aborted") {
        setVoiceStatus("Voice error: " + event.error);
      }
    });

    // "end" fires after stop() once the engine has flushed its last words.
    recognition.addEventListener("end", () => {
      listening = false;
      els.ptt.classList.remove("listening");
      els.ptt.setAttribute("aria-pressed", "false");
      els.ptt.textContent = "Hold to speak";
      if (heardText) {
        sendMessage(heardText);
        setVoiceStatus("");
      } else {
        setVoiceStatus("Nothing heard. Try again or type below.");
      }
      heardText = "";
    });
  }

  function startListening() {
    if (!recognition || listening) return;
    heardText = "";
    try {
      recognition.start();
      listening = true;
      els.ptt.classList.add("listening");
      els.ptt.setAttribute("aria-pressed", "true");
      els.ptt.textContent = "Listening... release to send";
      setVoiceStatus("Listening...");
    } catch (err) {
      setVoiceStatus("Could not start the microphone.");
    }
  }

  function stopListening() {
    if (!recognition || !listening) return;
    recognition.stop(); // the "end" handler above does the sending
  }

  // Mouse: press on the button, release anywhere on the page.
  els.ptt.addEventListener("mousedown", (e) => { if (e.button === 0) startListening(); });
  window.addEventListener("mouseup", stopListening);
  // Touch: preventDefault so the browser does not also fire mouse events.
  els.ptt.addEventListener("touchstart", (e) => { e.preventDefault(); startListening(); }, { passive: false });
  els.ptt.addEventListener("touchend", (e) => { e.preventDefault(); stopListening(); }, { passive: false });
  els.ptt.addEventListener("touchcancel", stopListening);
  // Keyboard: hold Space while the button is focused.
  els.ptt.addEventListener("keydown", (e) => {
    if (e.code === "Space") { e.preventDefault(); if (!e.repeat) startListening(); }
  });
  els.ptt.addEventListener("keyup", (e) => {
    if (e.code === "Space") { e.preventDefault(); stopListening(); }
  });
  els.ptt.addEventListener("blur", stopListening);

  // ------------------------------------------------------------------
  // Glass box: dashboard polling
  // ------------------------------------------------------------------
  async function pollDashboard() {
    const url = lastEventId ? "/api/dashboard?after_id=" + lastEventId : "/api/dashboard";
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      setOffline(false);
      renderLocation(data.location);
      renderStock(data.stock || [], Boolean(data.stale));
      holds = data.holds || [];
      renderHolds();
      addEvents(data.events || []);
    } catch (err) {
      setOffline(true);
    }
  }

  function renderLocation(location) {
    if (!location) return;
    els.locationName.textContent = location.name || "";
    els.locationHours.textContent = location.hours ? "· " + location.hours : "";
  }

  function renderStock(stock, stale) {
    els.staleBanner.hidden = !stale;
    const rows = stock.map((item) => {
      const tr = document.createElement("tr");
      tr.append(el("td", "", item.name));
      const sellable = el("td", "num");
      if (item.status === "unknown" || item.sellable === null || item.sellable === undefined) {
        sellable.append(el("span", "amber-text", "unknown"));
      } else {
        sellable.textContent = item.sellable;
      }
      tr.append(sellable, el("td", "num", item.on_hand));
      return tr;
    });
    els.stockBody.replaceChildren(...rows);
  }

  function renderHolds() {
    const visible = holds.filter((h) => h.status === "active" || h.status === "pending_approval");
    const nodes = visible.map((h) => {
      const li = el("li", "card");
      const head = el("div", "card-head");
      head.append(el("span", "card-title", h.quantity + " x " + h.item_name));
      const badge = h.status === "pending_approval" ? "chip chip-amber" : "chip chip-green";
      head.append(el("span", badge, h.status.replace("_", " ")));
      li.append(head);
      const sub = el("div", "card-sub");
      sub.append(document.createTextNode((h.customer_name || "unnamed caller") + " · " + (h.channel || "") + " · expires in "));
      const cd = el("span", "countdown", countdown(h.expires_at));
      cd.dataset.expires = h.expires_at; // the 1 s ticker updates these
      sub.append(cd);
      li.append(sub);
      if (h.dietary_note) li.append(el("div", "dietary", "DIETARY: " + h.dietary_note));
      return li;
    });
    fill(els.holdsList, nodes, "No holds yet.");
  }

  // Update every countdown once a second without re-rendering the list.
  setInterval(() => {
    document.querySelectorAll(".countdown[data-expires]").forEach((node) => {
      const text = countdown(node.dataset.expires);
      node.textContent = text;
      node.classList.toggle("expired", text === "expired");
    });
  }, 1000);

  // ------------------------------------------------------------------
  // Glass box: event feed (newest at top, capped at MAX_EVENTS)
  // ------------------------------------------------------------------
  function addEvents(incoming) {
    // Oldest first, so that after prepending each one the newest ends on top.
    const fresh = incoming.filter((ev) => !seenEventIds.has(ev.id)).sort((a, b) => a.id - b.id);
    if (fresh.length === 0) return;
    const placeholder = els.eventsList.querySelector(".empty");
    if (placeholder) placeholder.remove();
    for (const ev of fresh) {
      seenEventIds.add(ev.id);
      if (ev.id > lastEventId) lastEventId = ev.id;
      els.eventsList.prepend(renderEvent(ev));
    }
    while (els.eventsList.children.length > MAX_EVENTS) els.eventsList.lastElementChild.remove();
  }

  /** Build one <li> for an event. tool_call and guardrail_violation get special treatment. */
  function renderEvent(ev) {
    const d = ev.detail || {};
    const li = el("li", "event event-" + ev.kind);
    const head = el("div", "event-head");
    head.append(el("time", "", formatTime(ev.at)), el("span", "chip chip-channel", ev.channel || "system"));
    if (ev.call_id) head.append(el("span", "", "call " + String(ev.call_id).slice(0, 8)));
    li.append(head);

    if (ev.kind === "tool_call") {
      // Prominent: name, then arguments and result as compact JSON. Open by default
      // so a viewer can see e.g. check_availability -> {"quantity": 11} before the reply.
      li.classList.add("event-tool");
      const details = document.createElement("details");
      details.open = true;
      const summary = document.createElement("summary");
      summary.append(el("span", "event-kind", "tool call"), el("code", "tool-name", d.name || "?"),
        el("span", d.ok ? "chip chip-green" : "chip chip-red", d.ok ? "ok" : "failed"));
      details.append(summary);
      for (const [label, value] of [["args", d.arguments], ["result", d.result]]) {
        const row = el("div", "tool-row");
        row.append(el("span", "tool-label", label), el("code", "tool-json", compactJson(value)));
        details.append(row);
      }
      li.append(details);
    } else if (ev.kind === "guardrail_violation") {
      li.classList.add("event-violation");
      const line = el("div", "event-line");
      line.append(el("span", "chip chip-red", "Tier " + d.tier), el("span", "event-kind", "guardrail violation"),
        el("code", "", d.rule || ""));
      if (d.enforced === false) line.append(el("span", "note", "logged, not enforced"));
      li.append(line);
      if (d.excerpt) li.append(el("blockquote", "event-excerpt", d.excerpt));
    } else {
      const line = el("div", "event-line");
      line.append(el("span", "event-kind", ev.kind.replace(/_/g, " ")), el("span", "", describeDetail(ev.kind, d)));
      li.append(line);
    }
    return li;
  }

  /** Short human text for the "other" event kinds, falling back to compact JSON. */
  function describeDetail(kind, d) {
    const parts = [];
    if (d.quantity !== undefined && d.item_name) parts.push(d.quantity + " x " + d.item_name);
    else if (d.item_name) parts.push(d.item_name);
    if (d.customer_name) parts.push("for " + d.customer_name);
    if (d.reason) parts.push("(" + d.reason + ")");
    if (d.summary) parts.push(d.summary);
    if (parts.length) return parts.join(" ");
    const keys = Object.keys(d);
    return keys.length ? compactJson(d) : "";
  }

  // ------------------------------------------------------------------
  // Demo controls
  // ------------------------------------------------------------------
  async function demoAction(label, url, body) {
    els.demoStatus.textContent = label + "...";
    try {
      const data = await postJson(url, body);
      els.demoStatus.textContent = label + ": done" + (data.sold !== undefined ? " (" + data.sold + " sold)" : "");
      pollDashboard();
    } catch (err) {
      els.demoStatus.textContent = label + " failed: " + err.message;
    }
  }

  els.btnRush.addEventListener("click", () => demoAction("Simulate a rush", "/api/simulate/sales", { minutes: 20 }));
  els.btnStale.addEventListener("click", () => demoAction("Break the POS sync", "/api/simulate/stale"));
  els.btnResync.addEventListener("click", () => demoAction("Restore sync", "/api/simulate/resync"));
  els.btnReset.addEventListener("click", () => {
    if (!confirm("Reset the demo? This clears holds, counters and the event log.")) return;
    // The server restarts event ids, so forget everything we have seen.
    lastEventId = 0;
    seenEventIds.clear();
    fill(els.eventsList, [], "Waiting for events...");
    demoAction("Reset demo", "/api/reset");
  });

  // ------------------------------------------------------------------
  // Start
  // ------------------------------------------------------------------
  pollDashboard();
  setInterval(pollDashboard, POLL_MS);
})();
