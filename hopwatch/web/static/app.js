"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const state = { wants: [], candidates: [], approvals: [], onlyOpen: false, showAll: false };

// A couple of wants over a fortnight easily yields a hundred candidates —
// every departure on every viable path. Showing them all at once buries the
// handful that matter, which are always the ones nearest their unlock.
const CANDIDATE_PAGE = 25;

// --- formatting -------------------------------------------------------------

function countdown(iso) {
  if (!iso) return "—";
  let secs = Math.round((new Date(iso) - new Date()) / 1000);
  if (secs <= 0) return "now";
  const d = Math.floor(secs / 86400); secs -= d * 86400;
  const h = Math.floor(secs / 3600); secs -= h * 3600;
  const m = Math.floor(secs / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function duration(mins) {
  const sign = mins < 0 ? "-" : "";
  mins = Math.abs(mins);
  return `${sign}${Math.floor(mins / 60)}h${String(mins % 60).padStart(2, "0")}`;
}

function localTime(iso) {
  if (!iso) return "";
  return iso.slice(0, 16).replace("T", " ");
}

function clockTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}

// Sweeps run on a worker, not in the request. Poll the job until a worker has
// finished with it; give up quietly after a while (the result still lands).
async function waitForJob(key, timeoutMs = 120000) {
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    const job = await api(`/api/jobs/${encodeURIComponent(key)}`);
    if (["done", "failed", "dead"].includes(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  return null;
}

// --- status strip -----------------------------------------------------------

function renderStatus(data) {
  const strip = $("#statusstrip");
  strip.innerHTML = "";
  const w = (data && data.watcher) || {};

  const add = (label, value, cls) => {
    const pill = el("span", `pill ${cls || ""}`);
    pill.append(document.createTextNode(label + " "));
    pill.append(el("b", null, value));
    strip.append(pill);
  };

  if (w.session_ok === false) add("Wizz session", "not logged in", "bad");
  else if (w.session_ok === true) add("Wizz session", "ok", "ok");
  else add("Wizz session", "unknown", "warn");

  add("Watcher", w.running ? "running" : "stopped", w.running ? "ok" : "bad");
  add("Checks left/hr", String(w.checks_remaining_this_hour ?? "?"));
  add("Open bookings", String(w.open_bookings ?? 0));
  if (w.last_search_at) add("Last sweep", clockTime(w.last_search_at));
  if (w.pipeline) {
    const p = w.pipeline;
    add("Search queue", `${p.waiting} waiting · ${p.in_progress} running`);
    if (p.dead) add("Dead-lettered", String(p.dead), "bad");
  }
  if (w.passenger_configured === false) add("Passenger details", "unset", "warn");
  if (w.last_problem) add("Problem", String(w.last_problem).slice(0, 60), "bad");
}

// --- wants ------------------------------------------------------------------

function renderWants() {
  const host = $("#wants");
  host.innerHTML = "";
  if (!state.wants.length) {
    host.append(el("p", "empty", "No watches yet. Add one to start tracking windows."));
    return;
  }

  for (const want of state.wants) {
    const row = el("div", "row");
    const main = el("div", "row-main");
    main.append(el("div", "route", `${want.origin} → ${want.destination}`));
    main.append(el("div", "meta",
      `${want.name} · ${want.date_from} to ${want.date_to} · ` +
      `≤${want.max_stops} stop(s) · ≥${duration(want.min_layover_min)} layover` +
      (want.last_searched_at ? ` · swept ${clockTime(want.last_searched_at)}` : "")));
    row.append(main);

    const side = el("div", "row-side");
    if (want.auto_request_booking) side.append(el("span", "tag warn", "auto-prepare"));
    side.append(el("span", `tag ${want.active ? "ok" : ""}`, want.active ? "active" : "paused"));

    const sweep = el("button", "btn tiny", "Sweep now");
    sweep.onclick = async () => {
      sweep.disabled = true; sweep.textContent = "Queued…";
      try {
        const { job } = await api(`/api/wants/${want.id}/search`, { method: "POST" });
        sweep.textContent = "Sweeping…";
        const done = await waitForJob(job);
        if (done && done.status !== "done") alert(`Sweep ${done.status}: ${done.last_error || ""}`);
        await refresh();
      }
      catch (err) { alert(err.message); }
      finally { sweep.disabled = false; sweep.textContent = "Sweep now"; }
    };
    side.append(sweep);

    const toggle = el("button", "btn tiny", want.active ? "Pause" : "Resume");
    toggle.onclick = async () => {
      await api(`/api/wants/${want.id}`, {
        method: "PATCH", body: JSON.stringify({ active: !want.active }),
      });
      await refresh();
    };
    side.append(toggle);

    const remove = el("button", "btn tiny danger", "Delete");
    remove.onclick = async () => {
      if (!confirm(`Delete watch "${want.name}"? Its candidates go too.`)) return;
      await api(`/api/wants/${want.id}`, { method: "DELETE" });
      await refresh();
    };
    side.append(remove);

    row.append(side);
    host.append(row);
  }
}

// --- candidates -------------------------------------------------------------

const AVAILABILITY_TAGS = {
  available: ["ok", "seat available"],
  sold_out: ["bad", "sold out"],
  check_failed: ["warn", "check failed"],
  unknown: ["", "unchecked"],
};

function renderCandidates() {
  const host = $("#candidates");
  host.innerHTML = "";
  let list = state.candidates;
  if (state.onlyOpen) list = list.filter((c) => c.window_status === "open");

  if (!list.length) {
    host.append(el("p", "empty",
      state.onlyOpen ? "Nothing is inside its 72-hour window right now."
                     : "No candidates yet. They appear after the first sweep."));
    return;
  }

  const total = list.length;
  const hidden = state.showAll ? 0 : Math.max(0, total - CANDIDATE_PAGE);
  if (!state.showAll) list = list.slice(0, CANDIDATE_PAGE);

  const summary = el("p", "hint",
    hidden
      ? `Showing the ${list.length} nearest their unlock, of ${total}.`
      : `${total} candidate${total === 1 ? "" : "s"}.`);
  host.append(summary);

  for (const c of list) {
    const row = el("div", "row");
    const main = el("div", "row-main");
    main.append(el("div", "route", c.path.join(" → ")));

    const bits = [
      c.stops === 0 ? "direct" : `${c.stops} stop`,
      duration(c.total_minutes),
      `€${10 * c.trip_credits} · ${c.trip_credits} credit(s)`,
      c.want_name,
    ];
    main.append(el("div", "meta", bits.join("  ·  ")));

    const legs = el("div", "legs");
    for (const leg of c.legs) {
      const line = el("div", "leg");
      line.append(el("b", null, `${leg.origin}→${leg.destination}`));
      line.append(document.createTextNode(`  ${localTime(leg.departs_local)}`));
      legs.append(line);
    }
    main.append(legs);

    if (c.staggered_hours > 0) {
      main.append(el("div", "warnline",
        `Leg 1 unlocks ${duration(Math.round(c.staggered_hours * 60))} before the last leg. ` +
        `Its seats can sell out while you wait — do not book leg 1 early.`));
    }
    if (c.ground_transfer) {
      main.append(el("div", "warnline", "Requires changing airports on the ground."));
    }
    row.append(main);

    const side = el("div", "row-side");
    const [cls, label] = AVAILABILITY_TAGS[c.availability] || ["", c.availability];
    side.append(el("span", `tag ${cls}`, label));

    const timer = el("span", "tag");
    timer.dataset.open = c.window_opens_utc;
    timer.dataset.close = c.window_closes_utc;
    timer.classList.add("countdown-tag");
    side.append(timer);

    row.append(side);
    host.append(row);
  }

  if (hidden) {
    const more = el("button", "btn", `Show all ${total}`);
    more.style.marginTop = "14px";
    more.onclick = () => { state.showAll = true; renderCandidates(); };
    host.append(more);
  } else if (state.showAll && total > CANDIDATE_PAGE) {
    const less = el("button", "btn ghost", "Show fewer");
    less.style.marginTop = "14px";
    less.onclick = () => { state.showAll = false; renderCandidates(); };
    host.append(less);
  }

  tickCountdowns();
}

function tickCountdowns() {
  const now = new Date();
  for (const node of document.querySelectorAll(".countdown-tag")) {
    const opens = new Date(node.dataset.open);
    const closes = new Date(node.dataset.close);
    node.classList.remove("ok", "warn", "bad");
    if (now >= closes) {
      node.textContent = "window closed";
      node.classList.add("bad");
    } else if (now >= opens) {
      node.textContent = `bookable · ${countdown(node.dataset.close)} left`;
      node.classList.add("ok");
    } else {
      node.textContent = `unlocks in ${countdown(node.dataset.open)}`;
      node.classList.add("warn");
    }
  }
  for (const node of document.querySelectorAll(".hold-countdown")) {
    node.textContent = countdown(node.dataset.until);
  }
}

// --- approvals --------------------------------------------------------------

function renderApprovals() {
  const section = $("#approvals-section");
  const host = $("#approvals");
  host.innerHTML = "";
  section.classList.toggle("hidden", state.approvals.length === 0);

  for (const booking of state.approvals) {
    const card = el("div", "approval");
    const candidate = booking.candidate;

    card.append(el("div", "route",
      candidate ? candidate.path.join(" → ") : booking.summary));
    card.append(el("div", "meta", booking.summary));

    const credits = booking.detail.trip_credits || (candidate ? candidate.trip_credits : 1);
    card.append(el("div", "cost", `€${(booking.detail.cost_eur ?? credits * 10).toFixed(0)} · ${credits} trip credit(s)`));

    if (candidate && candidate.stops > 0) {
      card.append(el("div", "warnline",
        `${credits} separate bookings will be confirmed back to back. ` +
        `If a later one fails you will be holding only the earlier legs.`));
    }
    for (const note of booking.detail.notes || []) {
      card.append(el("div", "warnline", note));
    }

    if (booking.hold_expires_at) {
      const hold = el("div", "meta");
      hold.append(document.createTextNode("Decide within "));
      const span = el("span", "countdown hold-countdown");
      span.dataset.until = booking.hold_expires_at;
      hold.append(span);
      card.append(hold);
    }

    if (booking.has_screenshot) {
      const details = el("details", "shot");
      details.append(el("summary", null, "Show the page it is about to confirm"));
      const img = el("img");
      img.src = `/api/bookings/${booking.id}/screenshot`;
      img.alt = "Confirm step screenshot";
      img.loading = "lazy";
      details.append(img);
      card.append(details);
    }

    const actions = el("div", "approval-actions");
    const err = el("span", "formerror");

    const approve = el("button", "btn primary", "Approve & book");
    const skip = el("button", "btn ghost", "Skip");

    const decide = async (approved) => {
      approve.disabled = skip.disabled = true;
      err.textContent = "";
      try {
        await api(`/api/bookings/${booking.id}/decision`, {
          method: "POST", body: JSON.stringify({ approved }),
        });
        await refresh();
      } catch (e) {
        err.textContent = e.message;
        approve.disabled = skip.disabled = false;
      }
    };
    approve.onclick = () => {
      if (confirm(`Confirm ${credits} booking(s) for €${credits * 10}?`)) decide(true);
    };
    skip.onclick = () => decide(false);

    actions.append(approve, skip, err);
    card.append(actions);
    host.append(card);
  }
}

// --- activity ---------------------------------------------------------------

function renderEvents(events) {
  const host = $("#activity");
  host.innerHTML = "";
  if (!events.length) {
    host.append(el("p", "empty", "Nothing has happened yet."));
    return;
  }
  for (const event of events) host.append(eventNode(event));
}

function eventNode(event) {
  const node = el("div", `event ${event.level}`);
  node.append(el("time", null, clockTime(event.ts)));
  node.append(el("span", "msg", event.message));
  return node;
}

function prependEvent(event) {
  const host = $("#activity");
  const empty = host.querySelector(".empty");
  if (empty) empty.remove();
  host.prepend(eventNode(event));
  while (host.children.length > 120) host.lastChild.remove();
}

// --- wiring -----------------------------------------------------------------

async function refresh() {
  const [wants, candidates, approvals] = await Promise.all([
    api("/api/wants"),
    api("/api/candidates?status=watching&limit=200"),
    api("/api/bookings/pending"),
  ]);
  state.wants = wants;
  state.candidates = candidates;
  state.approvals = approvals;
  renderWants();
  renderCandidates();
  renderApprovals();
}

function wireForm() {
  const form = $("#want-form");
  $("#toggle-want-form").onclick = () => form.classList.toggle("hidden");
  $("#cancel-want").onclick = () => form.classList.add("hidden");

  const today = new Date().toISOString().slice(0, 10);
  const plus = new Date(Date.now() + 14 * 86400000).toISOString().slice(0, 10);
  form.elements.date_from.value = today;
  form.elements.date_to.value = plus;

  form.onsubmit = async (e) => {
    e.preventDefault();
    const data = new FormData(form);
    const num = (k) => (data.get(k) === "" || data.get(k) === null ? null : Number(data.get(k)));
    const payload = {
      name: data.get("name"),
      origin: data.get("origin"),
      destination: data.get("destination"),
      date_from: data.get("date_from"),
      date_to: data.get("date_to"),
      max_stops: num("max_stops") ?? 1,
      min_layover_min: num("min_layover_min") ?? 180,
      max_trip_hours: num("max_trip_hours") ?? 30,
      after_hour: num("after_hour"),
      before_hour: num("before_hour"),
      allow_ground_transfer: data.get("allow_ground_transfer") === "on",
      auto_request_booking: data.get("auto_request_booking") === "on",
    };
    $("#want-error").textContent = "";
    try {
      await api("/api/wants", { method: "POST", body: JSON.stringify(payload) });
      form.reset();
      form.elements.date_from.value = today;
      form.elements.date_to.value = plus;
      form.classList.add("hidden");
      await refresh();
    } catch (err) {
      $("#want-error").textContent = err.message;
    }
  };
}

function wireStream() {
  const source = new EventSource("/api/stream");
  source.addEventListener("status", (e) => {
    const data = JSON.parse(e.data);
    renderStatus(data);
  });
  source.addEventListener("activity", (e) => {
    const event = JSON.parse(e.data);
    prependEvent(event);
    if (["available", "approval_needed", "booking_finished"].includes(event.kind)) {
      refresh().catch(() => {});
    }
  });
  source.onerror = () => { /* EventSource reconnects on its own */ };
}

async function init() {
  wireForm();
  $("#only-open").onchange = (e) => { state.onlyOpen = e.target.checked; renderCandidates(); };

  renderStatus(await api("/api/status"));
  renderEvents(await api("/api/events?limit=60"));
  await refresh();

  wireStream();
  setInterval(tickCountdowns, 1000);
  setInterval(() => refresh().catch(() => {}), 60000);
}

init().catch((err) => {
  document.body.prepend(el("p", "formerror", `Failed to start: ${err.message}`));
});
