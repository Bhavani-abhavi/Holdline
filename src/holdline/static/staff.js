/* HoldLine staff page (staff.html).
 *
 * Polls two endpoints every 2 s:
 *   GET /api/staff/alerts  -> toasts for new alerts + the "Open alerts" list
 *   GET /api/dashboard     -> approval queue, active holds, counters, order feed
 * and posts staff actions (ack / approve / decline / collect / delivery).
 *
 * Plain browser JavaScript, no framework. Wrapped in an IIFE so nothing leaks.
 * NOTE: there is no authentication on this page; it is a demo.
 */
(function () {
  "use strict";

  const POLL_MS = 2000;
  const TOAST_MS = 8000;      // how long a toast stays on screen
  const MAX_TOASTS = 5;       // oldest toast is dropped beyond this
  const MAX_FEED = 50;        // rows kept in the live order feed

  const $ = (id) => document.getElementById(id);
  const els = {
    offline: $("offline-indicator"),
    toastRegion: $("toast-region"),
    alertsList: $("alerts-list"),
    approvalList: $("approval-list"),
    activeHolds: $("active-holds-list"),
    deliveryForm: $("delivery-form"),
    deliveryItem: $("delivery-item"),
    deliveryQty: $("delivery-qty"),
    deliveryNote: $("delivery-note"),
    deliverySubmit: $("delivery-submit"),
    deliveryStatus: $("delivery-status"),
    countersBody: $("counters-body"),
    orderFeed: $("order-feed"),
  };

  // Page state
  const seenAlertIds = new Set();   // alert ids we have already toasted
  const seenEventIds = new Set();   // dashboard event ids already in the feed
  let lastEventId = 0;              // for ?after_id=
  let alerts = [];                  // latest alert list from the server
  const itemNames = new Map();      // item_id -> name (from menu and stock)
  const busy = new Set();           // ids with a POST in flight (avoid double clicks)

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function compactJson(value) {
    try { return JSON.stringify(value); } catch (err) { return String(value); }
  }

  /** Local clock time "14:03" for an ISO string. */
  function formatTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? String(iso || "") : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function setOffline(isOffline) { els.offline.hidden = !isOffline; }

  function fill(list, nodes, emptyText) {
    list.replaceChildren();
    if (nodes.length === 0) list.append(el("li", "empty", emptyText));
    else nodes.forEach((n) => list.append(n));
  }

  async function getJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error("HTTP " + res.status + " from " + url);
    return res.json();
  }

  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error("HTTP " + res.status + " from " + url);
    return res.json();
  }

  /** A small action button that POSTs, then refreshes. Disabled while in flight. */
  function actionButton(label, className, key, url) {
    const btn = el("button", "btn btn-small " + className, label);
    btn.type = "button";
    btn.addEventListener("click", async () => {
      if (busy.has(key)) return;
      busy.add(key);
      btn.disabled = true;
      try {
        await postJson(url);
        await refreshAll();
      } catch (err) {
        console.error(label + " failed", err);
        btn.disabled = false;
        btn.textContent = label + " (failed, retry)";
      } finally {
        busy.delete(key);
      }
    });
    return btn;
  }

  /** The red "DIETARY: ..." label, or null when there is no note. */
  function dietaryLabel(note) {
    return note ? el("div", "dietary", "DIETARY: " + note) : null;
  }

  // ------------------------------------------------------------------
  // Toasts
  // ------------------------------------------------------------------
  function showToast(alert) {
    const toast = el("div", "toast toast-" + (alert.urgency || "info"));
    toast.setAttribute("role", alert.urgency === "urgent" ? "alert" : "status");
    toast.append(el("div", "toast-kind", (alert.urgency || "info") + " · " + (alert.kind || "").replace(/_/g, " ")));
    toast.append(el("div", "", alert.summary || ""));
    const note = alert.payload && alert.payload.dietary_note;
    if (note) toast.append(dietaryLabel(note));
    els.toastRegion.append(toast);
    while (els.toastRegion.children.length > MAX_TOASTS) els.toastRegion.firstElementChild.remove();
    setTimeout(() => toast.remove(), TOAST_MS);
  }

  // ------------------------------------------------------------------
  // Alerts
  // ------------------------------------------------------------------
  async function pollAlerts() {
    const data = await getJson("/api/staff/alerts");
    alerts = data.alerts || [];
    for (const alert of alerts) {
      if (seenAlertIds.has(alert.id)) continue;
      seenAlertIds.add(alert.id);
      if (!alert.acknowledged) showToast(alert); // toast only alerts nobody has handled
    }
    renderAlerts();
  }

  function renderAlerts() {
    const open = alerts.filter((a) => !a.acknowledged).sort((a, b) => (a.at < b.at ? 1 : -1));
    const nodes = open.map((a) => {
      const li = el("li", "card");
      const head = el("div", "card-head");
      const chip = { info: "chip chip-grey", action: "chip chip-blue", urgent: "chip chip-red" }[a.urgency] || "chip chip-grey";
      head.append(el("span", chip, a.urgency), el("span", "card-sub", formatTime(a.at)));
      li.append(head, el("div", "card-title", a.summary || a.kind));
      const note = a.payload && a.payload.dietary_note;
      if (note) li.append(dietaryLabel(note));
      const actions = el("div", "card-actions");
      actions.append(actionButton("Acknowledge", "btn-secondary", "ack-" + a.id, "/api/staff/alerts/" + a.id + "/ack"));
      li.append(actions);
      return li;
    });
    fill(els.alertsList, nodes, "No open alerts.");
  }

  // ------------------------------------------------------------------
  // Dashboard: approval queue, active holds, counters, order feed
  // ------------------------------------------------------------------
  async function pollDashboard() {
    const url = lastEventId ? "/api/dashboard?after_id=" + lastEventId : "/api/dashboard";
    const data = await getJson(url);
    for (const s of data.stock || []) itemNames.set(s.item_id, s.name);
    const holds = data.holds || [];
    renderApprovalQueue(holds.filter((h) => h.status === "pending_approval"));
    renderActiveHolds(holds.filter((h) => h.status === "active"));
    renderCounters(data.counters || {});
    addFeedEvents(data.events || []);
  }

  /** Look up the alert that asked staff to approve this hold, for its approval_reasons. */
  function reasonsForHold(holdId) {
    const alert = alerts.find((a) => a.payload && a.payload.hold_id === holdId && Array.isArray(a.payload.approval_reasons));
    return alert ? alert.payload.approval_reasons : [];
  }

  function holdCard(h) {
    const li = el("li", "card");
    const head = el("div", "card-head");
    head.append(el("span", "card-title", h.quantity + " x " + h.item_name));
    head.append(el("span", "card-sub", "pickup " + formatTime(h.pickup_at)));
    li.append(head);
    const who = (h.customer_name || "unnamed caller") + (h.customer_phone ? " · " + h.customer_phone : "") +
      (h.channel ? " · via " + h.channel : "");
    li.append(el("div", "card-sub", who));
    // Dietary notes are the thing staff must not miss, so they get the red label.
    if (h.dietary_note) li.append(dietaryLabel(h.dietary_note));
    return li;
  }

  function renderApprovalQueue(pending) {
    const nodes = pending.map((h) => {
      const li = holdCard(h);
      const reasons = reasonsForHold(h.id);
      if (reasons.length) li.append(el("div", "card-sub", "Needs approval: " + reasons.join("; ")));
      const actions = el("div", "card-actions");
      actions.append(actionButton("Approve", "", "approve-" + h.id, "/api/staff/holds/" + h.id + "/approve"));
      actions.append(actionButton("Decline", "btn-danger", "decline-" + h.id, "/api/staff/holds/" + h.id + "/decline"));
      li.append(actions);
      return li;
    });
    fill(els.approvalList, nodes, "Nothing waiting for approval.");
  }

  function renderActiveHolds(active) {
    const nodes = active.map((h) => {
      const li = holdCard(h);
      li.append(el("div", "card-sub", "expires " + formatTime(h.expires_at)));
      const actions = el("div", "card-actions");
      actions.append(actionButton("Collected", "", "collect-" + h.id, "/api/staff/holds/" + h.id + "/collect"));
      li.append(actions);
      return li;
    });
    fill(els.activeHolds, nodes, "No active holds.");
  }

  function renderCounters(counters) {
    const rows = Object.entries(counters).map(([itemId, c]) => {
      const tr = document.createElement("tr");
      tr.append(el("td", "", itemNames.get(itemId) || itemId));
      for (const key of ["received", "sold", "held", "sellable"]) {
        const value = c[key];
        tr.append(el("td", "num", value === null || value === undefined ? "unknown" : value));
      }
      return tr;
    });
    els.countersBody.replaceChildren(...rows);
  }

  /** Keep only hold_created events; newest on top; capped. */
  function addFeedEvents(incoming) {
    const fresh = incoming.filter((ev) => !seenEventIds.has(ev.id)).sort((a, b) => a.id - b.id);
    for (const ev of fresh) {
      seenEventIds.add(ev.id);
      if (ev.id > lastEventId) lastEventId = ev.id;
      if (ev.kind !== "hold_created") continue;
      const placeholder = els.orderFeed.querySelector(".empty");
      if (placeholder) placeholder.remove();
      els.orderFeed.prepend(feedRow(ev));
    }
    while (els.orderFeed.children.length > MAX_FEED) els.orderFeed.lastElementChild.remove();
  }

  function feedRow(ev) {
    const d = ev.detail || {};
    const li = el("li", "card");
    const head = el("div", "card-head");
    head.append(el("span", "card-sub", formatTime(ev.at)), el("span", "chip chip-channel", ev.channel || "system"));
    li.append(head);
    const what = d.item_name ? (d.quantity !== undefined ? d.quantity + " x " : "") + d.item_name : compactJson(d);
    li.append(el("div", "card-title", (d.customer_name || "Unnamed caller") + " asked for " + what));
    if (d.dietary_note) li.append(dietaryLabel(d.dietary_note));
    return li;
  }

  // ------------------------------------------------------------------
  // Delivery form
  // ------------------------------------------------------------------
  async function loadMenu() {
    try {
      const data = await getJson("/api/menu");
      els.deliveryItem.replaceChildren();
      for (const item of data.items || []) {
        itemNames.set(item.item_id, item.name);
        const opt = el("option", "", item.name + " (" + item.category + ")");
        opt.value = item.item_id;
        els.deliveryItem.append(opt);
      }
    } catch (err) {
      els.deliveryStatus.textContent = "Could not load the menu.";
    }
  }

  els.deliveryForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      item_id: els.deliveryItem.value,
      quantity: parseInt(els.deliveryQty.value, 10),
      note: els.deliveryNote.value.trim(),
    };
    if (!body.item_id || !(body.quantity > 0)) {
      els.deliveryStatus.textContent = "Pick an item and a quantity of at least 1.";
      return;
    }
    els.deliverySubmit.disabled = true;
    els.deliveryStatus.textContent = "Recording...";
    try {
      const data = await postJson("/api/staff/delivery", body);
      els.deliveryStatus.textContent = "Recorded delivery " + (data.delivery_id || "") + ".";
      els.deliveryNote.value = "";
      await refreshAll();
    } catch (err) {
      els.deliveryStatus.textContent = "Delivery failed: " + err.message;
    } finally {
      els.deliverySubmit.disabled = false;
    }
  });

  // ------------------------------------------------------------------
  // Polling loop
  // ------------------------------------------------------------------
  async function refreshAll() {
    try {
      await pollAlerts();      // alerts first so approval reasons are available
      await pollDashboard();
      setOffline(false);
    } catch (err) {
      setOffline(true);
    }
  }

  loadMenu();
  refreshAll();
  setInterval(refreshAll, POLL_MS);
})();
