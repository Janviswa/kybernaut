(() => {
  "use strict";

  const apiBase = new URL(document.documentElement.dataset.apiBase || "/", window.location.origin);
  let operatorToken = "";
  let incidents = [];
  let demos = [];

  const byId = (id) => document.getElementById(id);
  const node = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  };
  const date = (value) => {
    if (!value) return "Time unavailable";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
  };
  const percent = (value) => Number.isFinite(value) ? `${Math.round(value * 100)}%` : "—";
  const currency = (value) => Number.isFinite(value)
    ? new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(value)
    : "Not estimated";
  const stateMessage = (id, message, kind = "") => {
    const target = byId(id);
    target.textContent = message;
    target.className = `state-box${kind ? ` ${kind}` : ""}`;
    target.hidden = false;
  };
  const clearState = (id) => {
    byId(id).hidden = true;
  };
  const setPill = (element, label, kind) => {
    element.className = `status-pill status-${kind}`;
    element.replaceChildren(node("i"), document.createTextNode(label));
  };
  const apiUrl = (path) => new URL(path.replace(/^\//, ""), apiBase).toString();

  async function request(path, { authenticated = true } = {}) {
    const headers = { Accept: "application/json" };
    if (authenticated && operatorToken) headers.Authorization = `Bearer ${operatorToken}`;
    const response = await fetch(apiUrl(path), { method: "GET", headers, cache: "no-store" });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") message = body.detail;
      } catch (_) {
        // Keep a safe status-based message when the server response is not JSON.
      }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  function renderIncidentList(items) {
    const list = byId("incident-list");
    list.replaceChildren();
    incidents = Array.isArray(items) ? items : [];
    byId("incident-count").textContent = String(incidents.length);
    if (!incidents.length) {
      stateMessage("incidents-state", "No persisted incidents are available. This is an empty history, not proof of current service health.");
      return;
    }
    clearState("incidents-state");
    for (const incident of incidents.slice(0, 20)) {
      const row = node("button", "record-row");
      row.type = "button";
      row.dataset.incidentId = incident.incident_id || "";
      const main = node("span", "record-main");
      main.append(
        node("strong", "", incident.incident_id || "Incident reference unavailable"),
        node("small", "", `${incident.service || "Service unavailable"} · ${incident.incident_type || "type unavailable"}${incident.message ? ` · ${incident.message}` : ""}`)
      );
      const side = node("span", "record-side");
      side.append(node("span", `tag ${incident.status === "RESOLVED" ? "good" : "warn"}`, incident.status || "unknown"));
      side.append(node("span", `tag ${incident.severity === "critical" ? "bad" : "warn"}`, incident.severity || "severity unavailable"));
      side.append(node("small", "", date(incident.updated_at || incident.opened_at)));
      row.append(main, side);
      row.addEventListener("click", () => loadTimeline(incident.incident_id, row));
      list.append(row);
    }
  }

  function showJsonDetails(parent, value, label) {
    if (!value || (typeof value === "object" && Object.keys(value).length === 0)) return;
    const details = node("details", "timeline-details");
    details.append(node("summary", "", label));
    const pre = node("pre");
    pre.textContent = JSON.stringify(value, null, 2);
    details.append(pre);
    parent.append(details);
  }

  async function loadTimeline(incidentId, selectedRow) {
    if (!incidentId) return;
    document.querySelectorAll("#incident-list .record-row").forEach((row) => row.classList.remove("selected"));
    selectedRow.classList.add("selected");
    byId("detail-title").textContent = incidentId;
    const incident = incidents.find((item) => item.incident_id === incidentId);
    byId("detail-classification").textContent = incident
      ? `Classification: ${incident.incident_type || "unavailable"} · Severity: ${incident.severity || "unavailable"} · Status: ${incident.status || "unavailable"}${incident.message ? ` · ${incident.message}` : ""}`
      : "Classification is unavailable in the persisted incident list; timeline evidence is shown below when present.";
    setPill(byId("detail-status"), "Loading timeline", "pending");
    byId("timeline-list").replaceChildren();
    stateMessage("timeline-state", "Loading persisted audit events…");
    try {
      const payload = await request(`/incidents/${encodeURIComponent(incidentId)}/timeline`);
      const events = Array.isArray(payload.events) ? payload.events : [];
      clearState("timeline-state");
      if (!events.length) {
        stateMessage("timeline-state", "No audit events are recorded for this incident.");
        setPill(byId("detail-status"), "No audit events", "warn");
        return;
      }
      const list = byId("timeline-list");
      for (const event of events) {
        const item = node("article", "timeline-item");
        item.append(
          node("h3", "", event.event_type || "Event"),
          node("time", "", date(event.timestamp)),
          node("p", "", event.summary || "No event summary recorded.")
        );
        if (event.outcome) item.append(node("p", "", `Outcome: ${event.outcome}`));
        showJsonDetails(item, event.policy_decision, "Deterministic policy result");
        showJsonDetails(item, event.details, "Evidence and recorded details");
        list.append(item);
      }
      setPill(byId("detail-status"), `${events.length} audit events`, "good");
    } catch (error) {
      stateMessage("timeline-state", error.message, "error");
      setPill(byId("detail-status"), "Timeline unavailable", "bad");
    }
  }

  function renderApprovals(items) {
    const list = byId("approval-list");
    list.replaceChildren();
    const approvals = Array.isArray(items) ? items : [];
    byId("approval-count").textContent = String(approvals.length);
    byId("approval-panel-count").textContent = String(approvals.length);
    if (!approvals.length) {
      stateMessage("approvals-state", "No pending approval requests are recorded.");
      return;
    }
    clearState("approvals-state");
    for (const approval of approvals.slice(0, 30)) {
      const row = node("article", "record-row approval-row");
      const main = node("span", "record-main");
      main.append(
        node("strong", "", approval.approval_id || "Approval reference unavailable"),
        node("small", "", `${approval.incident_ref || "Incident unavailable"} · requested by ${approval.requested_by || "unknown operator"}`)
      );
      const side = node("span", "record-side");
      side.append(node("span", "tag warn", approval.status || "unknown"));
      row.append(main, side);
      const action = node("div", "approval-action");
      const pre = node("pre");
      pre.textContent = JSON.stringify({
        proposed_action: approval.proposed_action,
        context: approval.context,
        expires_at: approval.expires_at,
      }, null, 2);
      action.append(pre);
      row.append(action);
      list.append(row);
    }
  }

  function renderAudit(items) {
    const list = byId("audit-list");
    list.replaceChildren();
    const events = Array.isArray(items) ? items : [];
    byId("audit-count").textContent = String(events.length);
    if (!events.length) {
      stateMessage("audit-state", "No audit events are recorded.");
      return;
    }
    clearState("audit-state");
    for (const event of events.slice(0, 12)) {
      const row = node("button", "record-row");
      row.type = "button";
      const main = node("span", "record-main");
      main.append(
        node("strong", "", event.event_type || "Audit event"),
        node("small", "", `${event.incident_ref || "No incident reference"} · ${event.summary || "No summary"}`)
      );
      row.append(main, node("span", "record-side", date(event.timestamp)));
      row.addEventListener("click", () => loadTimeline(
        event.incident_ref,
        byId("incident-list").querySelector(
          `[data-incident-id="${CSS.escape(event.incident_ref || "")}"]`
        ) || row
      ));
      list.append(row);
    }
  }

  function renderDemoDetail(demo) {
    const container = byId("demo-detail");
    container.replaceChildren();
    clearState("demo-detail-state");
    const incident = demo.incident;
    const incidentCard = node("section", "demo-detail-card");
    incidentCard.append(node("h3", "", "Synthetic incident"));
    incidentCard.append(node("p", "", incident
      ? `${incident.incident_id} · ${incident.service} · ${incident.incident_type} · ${incident.severity} · ${incident.status}. ${incident.message}`
      : `${demo.incident_id} · No incident-history row was recorded for this demo.`));
    container.append(incidentCard);

    const approvalCard = node("section", "demo-detail-card");
    approvalCard.append(node("h3", "", `Approval records (${demo.approvals.length})`));
    if (!demo.approvals.length) {
      approvalCard.append(node("p", "", "No approval record was stored."));
    } else {
      for (const approval of demo.approvals) {
        const details = node("details", "timeline-details");
        details.append(node("summary", "", `${approval.approval_id} · ${approval.status} · requester ${approval.requested_by || "unknown"}${approval.decided_by ? ` · approver ${approval.decided_by}` : ""}`));
        const pre = node("pre");
        pre.textContent = JSON.stringify({
          proposed_action: approval.proposed_action,
          context: approval.context,
          requested_at: approval.requested_at,
          expires_at: approval.expires_at,
          decided_at: approval.decided_at,
          decision_reason: approval.decision_reason,
        }, null, 2);
        details.append(pre);
        approvalCard.append(details);
      }
    }
    container.append(approvalCard);

    const auditCard = node("section", "demo-detail-card");
    auditCard.append(node("h3", "", `Audit timeline (${demo.audit_events.length})`));
    if (!demo.audit_events.length) {
      auditCard.append(node("p", "", "No audit events were stored."));
    } else {
      for (const event of demo.audit_events) {
        const entry = node("article", "demo-event");
        entry.append(
          node("strong", "", `${event.event_type} · ${event.outcome || "outcome not recorded"}`),
          node("time", "", date(event.timestamp)),
          node("p", "", event.summary || "No summary recorded.")
        );
        showJsonDetails(entry, event.policy_decision, "Policy result");
        showJsonDetails(entry, event.details, "Recorded evidence");
        auditCard.append(entry);
      }
    }
    container.append(auditCard);
  }

  function renderDemoResults(items, unavailableCount = 0) {
    const list = byId("demo-list");
    list.replaceChildren();
    demos = Array.isArray(items) ? items : [];
    if (!demos.length) {
      stateMessage(
        "demo-state",
        unavailableCount
          ? `No readable demo results are available; ${unavailableCount} database(s) were malformed or inaccessible.`
          : "No isolated synthetic demo results are available. Run the local demo to create one.",
        unavailableCount ? "error" : ""
      );
      return;
    }
    if (unavailableCount) {
      stateMessage(
        "demo-state",
        `${unavailableCount} malformed or inaccessible demo database(s) were omitted.`,
        "error"
      );
    } else {
      clearState("demo-state");
    }
    for (const demo of demos) {
      const row = node("button", "record-row");
      row.type = "button";
      const main = node("span", "record-main");
      main.append(
        node("strong", "", demo.incident_id),
        node("small", "", `${demo.incident?.service || "synthetic workflow"} · ${demo.audit_events.length} audit events · ${demo.approvals.length} approval records`)
      );
      const side = node("span", "record-side");
      side.append(node("span", "tag demo-tag", "SYNTHETIC"));
      side.append(node("small", "", date(demo.latest_at)));
      row.append(main, side);
      row.addEventListener("click", () => {
        list.querySelectorAll(".record-row").forEach((item) => item.classList.remove("selected"));
        row.classList.add("selected");
        renderDemoDetail(demo);
      });
      list.append(row);
    }
    renderDemoDetail(demos[0]);
    list.querySelector(".record-row")?.classList.add("selected");
  }

  function renderCost(report) {
    if (!report || !Array.isArray(report.recommendations) || !report.reporting_period) {
      stateMessage("cost-state", "A legacy or unsupported report format was found; weekly recommendations are not available from it.", "error");
      return;
    }
    clearState("cost-state");
    byId("cost-report").hidden = false;
    const synthetic = /synthetic/i.test(report.provenance || "");
    const pricingLabel = report.pricing?.cost_model_status === "unavailable"
      ? "PRICING UNAVAILABLE"
      : report.pricing?.model === "kubernetes_requests"
        ? report.pricing?.is_example ? "EXAMPLE REQUEST PRICING" : "REQUEST PRICING"
      : report.pricing?.is_example ? "EXAMPLE PRICING" : "CATALOG PRICING";
    byId("pricing-badge").textContent = `${synthetic ? "SYNTHETIC DATA" : "OBSERVED DATA"} · ${pricingLabel}`;
    byId("report-period").textContent = `${date(report.reporting_period.start)} — ${date(report.reporting_period.end_exclusive)}`;
    byId("report-provenance").textContent = report.provenance || "Not provided";
    const delivery = report.delivery || { status: "not_recorded", sent: false };
    byId("report-delivery").textContent = delivery.status === "sent"
      ? `Sent · ${date(delivery.updated_at)}`
      : `${delivery.status || "not_recorded"} · not sent`;
    byId("cost-value").textContent = delivery.status === "sent"
      ? "Delivered"
      : delivery.status === "preview_only" ? "Preview" : (delivery.status || "Available");
    byId("cost-detail").textContent = delivery.status === "sent"
      ? "Ledger confirms delivery"
      : delivery.status === "preview_only" ? "Report available; not emailed" : "Delivery is not confirmed";
    byId("report-realized").textContent = currency(report.summary?.realized_savings);
    if (report.pricing?.model === "kubernetes_requests") {
      byId("cost-disclaimer").textContent =
        "Requested-resource allocation estimates are not provider invoice costs. " +
        "Request reductions may improve bin packing without reducing a fixed-size node bill.";
    } else {
      byId("cost-disclaimer").textContent =
        "Recommendations are not infrastructure changes. Potential savings use catalog assumptions; realized savings are reported separately.";
    }
    const summary = byId("report-summary");
    summary.replaceChildren();
    const chips = [
      ["Resources", report.summary?.resources_analyzed],
      ["Potential monthly savings", currency(report.summary?.potential_monthly_savings)],
      ["Annualized estimate", currency(report.summary?.estimated_annual_savings)],
      ["Request-cost savings", report.summary?.potential_request_cost_savings == null
        ? "Not estimated"
        : `${report.pricing?.currency || report.summary?.request_cost_savings_currency || ""} ${Number(report.summary.potential_request_cost_savings).toFixed(2)} for period`],
      ["Pressure findings", report.summary?.capacity_pressure_resources],
    ];
    for (const [label, value] of chips) summary.append(node("span", "summary-chip", `${label}: ${value ?? "—"}`));

    const rows = byId("recommendation-rows");
    rows.replaceChildren();
    for (const item of report.recommendations) {
      const row = node("tr");
      const values = [
        item.resource || "Unknown resource",
        item.status || "unknown",
        item.cpu_avg === undefined ? "Unavailable" : `${percent(item.cpu_avg)} / ${percent(item.cpu_peak)}`,
        item.memory_avg === undefined ? "Unavailable" : `${percent(item.memory_avg)} / ${percent(item.memory_peak)}`,
        `${item.sample_count ?? 0} / ${item.expected_samples ?? 0} samples (${percent(item.coverage)})`,
        currency(item.potential_monthly_savings),
      ];
      values.forEach((value, index) => {
        const cell = node("td");
        if (index === 0) cell.append(node("strong", "", value));
        else cell.textContent = value;
        row.append(cell);
      });
      const evidence = node("tr", "recommendation-evidence");
      const detail = node("td");
      detail.colSpan = 6;
      const rationale = item.rationale || "No rationale provided.";
      const next = item.suggested_next_step ? ` Next: ${item.suggested_next_step}` : "";
      const risks = item.operational_risk ? ` Risk: ${item.operational_risk}` : "";
      const recommendation = item.recommendation ? ` Recommendation: ${item.recommendation}` : "";
      const sizing = item.recommended_type
        ? ` Proposed capacity: ${item.current_type || "current"} → ${item.recommended_type}.`
        : "";
      const requestSizing = item.cpu_request_recommended_cores == null
        ? ""
        : ` Aggregate requests: CPU ${item.cpu_request_current_cores} → ${item.cpu_request_recommended_cores} cores; memory ${item.memory_request_current_bytes} → ${item.memory_request_recommended_bytes} bytes.`;
      const costEstimate = item.current_estimated_request_cost
        ? ` Request allocation estimate: ${item.current_estimated_request_cost.currency} ${Number(item.current_estimated_request_cost.total).toFixed(2)} current for period`
          + (item.proposed_estimated_request_cost
            ? `, ${item.proposed_estimated_request_cost.currency} ${Number(item.proposed_estimated_request_cost.total).toFixed(2)} proposed.`
            : "; proposed cost unavailable.")
          + " Not an invoice amount."
        : "";
      detail.textContent = `${rationale}${recommendation}${sizing}${requestSizing}${costEstimate}${next}${risks}`;
      evidence.append(detail);
      rows.append(row, evidence);
    }
  }

  function renderResizeRequests(requests) {
    const list = byId("resize-list");
    list.replaceChildren();
    if (!Array.isArray(requests) || requests.length === 0) {
      stateMessage("resize-state", "No resize requests are recorded.");
      return;
    }
    clearState("resize-state");
    for (const request of requests) {
      const plan = request.plan || {};
      const execution = request.execution || {};
      const state = request.status || "unknown";
      const executionState = state === "EXECUTING"
        ? "Execution may be interrupted; owner reconciliation required"
        : state === "RECONCILING"
          ? "Read-only reconciliation in progress"
          : execution.status || "not started";
      const item = node("article", "record-row");
      item.append(
        node("strong", "", `${plan.namespace || "namespace unavailable"} / ${plan.workload || "workload unavailable"}`),
        node("span", "status-pill", state),
        node("small", "", `Requested by ${request.requested_by || "unknown"} · Approved by ${request.approved_by || "not approved"} · ${request.approved_at ? date(request.approved_at) : "approval pending"}`),
        node("small", "", `Execution: ${executionState} · stage: ${execution.execution_stage || request.execution_stage || "not attempted"} · rollout verified: ${execution.rollout_verified === true ? "yes" : execution.rollout_verified === false ? "no" : "not established"} · requests verified: ${execution.requests_verified === true ? "yes" : execution.requests_verified === false ? "no" : "not established"}`),
      );
      list.append(item);
    }
  }

  async function loadDashboard() {
    let authorizationRejected = false;
    let apiResponded = false;
    byId("refresh-button").disabled = true;
    stateMessage("incidents-state", "Loading persisted incident history…");
    stateMessage("approvals-state", "Loading pending approvals…");
    stateMessage("audit-state", "Loading audit events…");
    stateMessage("cost-state", "Checking for a weekly report…");
    stateMessage("resize-state", "Loading lab resize request status…");
    stateMessage("demo-state", "Loading isolated synthetic demo results…");
    const jobs = [
      ["incidents", () => request("/incidents/history")],
      ["approvals", () => request("/approvals?status=PENDING&limit=30")],
      ["audit", () => request("/audit?limit=30")],
      ["cost", () => request("/cost/report")],
      ["resizes", () => request("/cost/resizes?limit=100")],
      ["demos", () => request("/demo/results")],
    ];
    const results = await Promise.allSettled(jobs.map(([, load]) => load()));
    results.forEach((result, index) => {
      const [name] = jobs[index];
      if (result.status === "rejected") {
        if (Number.isInteger(result.reason.status)) apiResponded = true;
        const target = {
          incidents: "incidents-state",
          approvals: "approvals-state",
          audit: "audit-state",
          cost: "cost-state",
          resizes: "resize-state",
          demos: "demo-state",
        }[name];
        const absent = result.reason.status === 404 && name === "cost";
        stateMessage(target, absent
          ? (name === "cost" ? "No weekly cost report has been generated yet." : "No matching records were found.")
          : `Could not load ${name}: ${result.reason.message}`, absent ? "" : "error");
        if (name === "incidents") byId("incident-count").textContent = "—";
        if (name === "approvals") {
          byId("approval-count").textContent = "—";
          byId("approval-panel-count").textContent = "—";
        }
        if (name === "audit") byId("audit-count").textContent = "—";
        if (name === "cost") {
          byId("cost-value").textContent = "Unavailable";
          byId("cost-detail").textContent = "No report status loaded";
          byId("cost-report").hidden = true;
        }
        if (name === "resizes") byId("resize-list").replaceChildren();
        if (name === "demos") {
          byId("demo-list").replaceChildren();
          byId("demo-detail").replaceChildren();
        }
        if (result.reason.status === 401 || result.reason.status === 403) {
          authorizationRejected = true;
          byId("auth-message").textContent = "The operator token is invalid, expired, or lacks read permission.";
          byId("auth-message").className = "auth-message error";
        }
        return;
      }
      apiResponded = true;
      const value = result.value;
      if (name === "incidents") renderIncidentList(value.incidents);
      if (name === "approvals") renderApprovals(value.approvals);
      if (name === "audit") renderAudit(value.events);
      if (name === "cost") renderCost(value);
      if (name === "resizes") renderResizeRequests(value.requests);
      if (name === "demos") renderDemoResults(value.demos, value.unavailable_count);
    });
    byId("refresh-button").disabled = false;
    if (authorizationRejected) {
      operatorToken = "";
      byId("disconnect-button").hidden = true;
      return false;
    }
    if (!apiResponded) {
      byId("auth-message").textContent = "The API could not be reached. Check the backend status and try again.";
      byId("auth-message").className = "auth-message error";
      return false;
    }
    return true;
  }

  async function checkHealth() {
    try {
      const health = await request("/health", { authenticated: false });
      byId("service-value").textContent = health.status === "healthy" ? "Healthy" : "Degraded";
      byId("service-detail").textContent = `${health.service || "API"} · v${health.version || "unknown"} · telemetry not queried`;
      setPill(byId("api-status"), health.status === "healthy" ? "API healthy" : "API degraded", health.status === "healthy" ? "good" : "warn");
    } catch (error) {
      byId("service-value").textContent = "Unavailable";
      byId("service-detail").textContent = error.message;
      setPill(byId("api-status"), "API unavailable", "bad");
    }
  }

  byId("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = byId("operator-token");
    const candidate = input.value.trim();
    input.value = "";
    if (!candidate) {
      byId("auth-message").textContent = "Paste a short-lived operator token to connect.";
      byId("auth-message").className = "auth-message error";
      return;
    }
    operatorToken = candidate;
    byId("auth-message").textContent = "Checking read access…";
    byId("auth-message").className = "auth-message";
    byId("connect-button").disabled = true;
    const connected = await loadDashboard();
    byId("connect-button").disabled = false;
    if (!connected) return;
    byId("disconnect-button").hidden = false;
    byId("auth-message").textContent = "Connected. Token is held in memory and is not persisted.";
    byId("auth-message").className = "auth-message success";
  });

  byId("disconnect-button").addEventListener("click", () => {
    operatorToken = "";
    incidents = [];
    document.querySelectorAll(".record-list, .timeline").forEach((list) => list.replaceChildren());
    ["incidents-state", "approvals-state", "audit-state", "cost-state", "demo-state", "demo-detail-state"].forEach((id) => {
      stateMessage(id, "Connect with a read-permission token to load operational records.");
    });
    ["incident-count", "approval-count", "approval-panel-count", "audit-count", "cost-value"].forEach((id) => {
      byId(id).textContent = "—";
    });
    byId("cost-detail").textContent = "No delivery status loaded";
    byId("cost-report").hidden = true;
    byId("demo-list").replaceChildren();
    byId("demo-detail").replaceChildren();
    demos = [];
    byId("detail-title").textContent = "Incident details";
    setPill(byId("detail-status"), "Select an incident", "pending");
    byId("disconnect-button").hidden = true;
    byId("auth-message").textContent = "Disconnected. No token was retained.";
    byId("auth-message").className = "auth-message";
  });

  byId("refresh-button").addEventListener("click", () => {
    if (operatorToken) loadDashboard();
  });
  checkHealth();
})();
