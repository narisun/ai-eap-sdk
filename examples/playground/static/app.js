// EAP-Core Playground frontend. No framework — vanilla DOM + fetch.

const agentSelect = document.getElementById("agent-select");
const toolList = document.getElementById("tool-list");
const toolSelect = document.getElementById("tool-select");
const messages = document.getElementById("messages");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const tracePanel = document.getElementById("trace-panel");
const traceList = document.getElementById("trace-list");
const toolForm = document.getElementById("tool-form");
const toolArgs = document.getElementById("tool-args");
const toolResult = document.getElementById("tool-result");

let agents = [];

async function loadAgents() {
  const resp = await fetch("/api/agents");
  agents = await resp.json();
  agentSelect.innerHTML = "";
  for (const a of agents) {
    const opt = document.createElement("option");
    opt.value = a.name;
    const label = a.error
      ? `${a.name} — (load error)`
      : `${a.name} — ${a.description || "(no description)"}`;
    opt.textContent = label;
    agentSelect.appendChild(opt);
  }
  agentSelect.addEventListener("change", refreshSidebar);
  refreshSidebar();
}

function refreshSidebar() {
  const a = agents.find((x) => x.name === agentSelect.value);
  toolList.innerHTML = "";
  toolSelect.innerHTML = "";
  if (!a) return;
  const tools = a.tool_names || [];
  if (tools.length === 0) {
    const li = document.createElement("li");
    li.textContent = "(no tools)";
    li.className = "tool-empty";
    toolList.appendChild(li);
  }
  for (const t of tools) {
    const li = document.createElement("li");
    li.textContent = t;
    toolList.appendChild(li);
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    toolSelect.appendChild(opt);
  }
}

function appendMessage(role, text) {
  const div = document.createElement("div");
  div.className = `msg msg-${role}`;
  div.textContent = `${role}: ${text}`;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
}

function renderTrace(trace) {
  traceList.innerHTML = "";
  for (const entry of trace) {
    const li = document.createElement("li");
    const ts = typeof entry.ts_ms === "number" ? entry.ts_ms.toFixed(1) : "—";
    const argsStr = entry.args ? JSON.stringify(entry.args) : "";
    li.textContent = `[${ts}ms] ${entry.kind}: ${entry.name || ""} ${argsStr}`;
    traceList.appendChild(li);
  }
  // "Pipeline trace" rather than "Tool-call trace" because the panel
  // also surfaces pipeline markers (request_start, response, error)
  // emitted by ``PlaygroundTraceMiddleware`` in addition to actual
  // tool invocations. The count therefore reflects every recorded
  // entry, not just tool calls.
  tracePanel.querySelector("summary").textContent =
    `Pipeline trace (${trace.length} entries)`;
}

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = chatInput.value.trim();
  if (!msg) return;
  const agentName = agentSelect.value;
  if (!agentName) {
    alert("pick an agent first");
    return;
  }
  appendMessage("user", msg);
  chatInput.value = "";
  chatInput.disabled = true;
  try {
    const resp = await fetch(`/api/agents/${encodeURIComponent(agentName)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg }),
    });
    if (!resp.ok) {
      const err = await resp.text();
      appendMessage("error", `${resp.status}: ${err}`);
    } else {
      const data = await resp.json();
      appendMessage("agent", data.text);
      renderTrace(data.trace || []);
    }
  } catch (err) {
    appendMessage("error", String(err));
  } finally {
    chatInput.disabled = false;
    chatInput.focus();
  }
});

toolForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const agentName = agentSelect.value;
  const tool = toolSelect.value;
  if (!agentName || !tool) return;
  let args = {};
  try {
    args = toolArgs.value.trim() ? JSON.parse(toolArgs.value) : {};
  } catch (err) {
    toolResult.textContent = `Invalid JSON: ${err.message}`;
    return;
  }
  toolResult.textContent = "(invoking…)";
  try {
    const resp = await fetch(
      `/api/agents/${encodeURIComponent(agentName)}/tools/${encodeURIComponent(tool)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ arguments: args }),
      },
    );
    const data = await resp.json();
    toolResult.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    toolResult.textContent = `Request failed: ${err.message}`;
  }
});

loadAgents();
