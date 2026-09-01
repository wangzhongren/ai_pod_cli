/* Compact, low-fatigue Pod build progress presentation. */
const podProgress = document.getElementById("podProgress");
const podPhases = [
  ["impact_analysis", "Analyze"],
  ["models", "Models"],
  ["providers", "Providers"],
  ["services", "Services"],
  ["pipelines", "Pipelines"],
  ["interfaces", "Interface"],
  ["verification", "Verify"],
];
const podPhaseRanges = {
  impact_analysis: [2, 9],
  models: [10, 24],
  providers: [25, 39],
  services: [40, 59],
  pipelines: [60, 76],
  interfaces: [77, 91],
  verification: [92, 99],
};
const podPhaseAliases = {
  planning: "impact_analysis",
  planned: "impact_analysis",
  components: "services",
  entrypoint: "interfaces",
  validation: "verification",
  repair: "verification",
  completed: "verification",
};

podProgress.innerHTML = `
  <section class="pod-overview">
    <div class="pod-stage-row">
      <span class="pod-spinner" aria-hidden="true"></span>
      <div class="pod-stage-copy">
        <span class="pod-stage-kicker">Current step</span>
        <strong id="podStage">Preparing</strong>
      </div>
      <span class="pod-percent-pill" id="podPercent">0%</span>
    </div>
    <div class="pod-progress-track" aria-label="Pod build progress">
      <i id="podProgressBar"></i>
    </div>
    <div class="pod-phase-strip" id="podPhaseStrip">
      ${podPhases.map(([key, label]) => `<span data-phase="${key}">${label}</span>`).join("")}
    </div>
  </section>
  <div class="pod-activity">
    <span class="pod-activity-dot" aria-hidden="true"></span>
    <span id="podMessage">Preparing project context…</span>
  </div>
  <details class="pod-details" id="podDetails">
    <summary>Build details <span id="podLogCount">0</span></summary>
    <pre class="pod-log" id="podLog"></pre>
  </details>
`;

function podPhaseIndex(stage) {
  const normalized = podPhaseAliases[stage] || stage;
  return Math.max(0, podPhases.findIndex(([key]) => key === normalized));
}

function boundedPodPercent(stage, rawPercent, status) {
  if (status === "completed") return 100;
  const normalized = podPhaseAliases[stage] || stage;
  const range = podPhaseRanges[normalized];
  const percent = Math.max(0, Math.min(100, Number(rawPercent || 0)));
  if (!range) return percent;
  return Math.max(range[0], Math.min(range[1], percent));
}

renderPodTask = function renderPodTaskCompact(task) {
  const stage = String(task.stage || "working");
  const percent = boundedPodPercent(stage, task.percent, task.status);
  const logs = task.logs || [];
  const stageLabel = stage.replace(/(^|_)([a-z])/g, (_, prefix, letter) =>
    (prefix ? " " : "") + letter.toUpperCase()
  );
  document.getElementById("podStage").textContent = stageLabel;
  document.getElementById("podPercent").textContent = `${percent}%`;
  document.getElementById("podMessage").textContent = task.message || "Working…";
  document.querySelector("#podProgress .pod-spinner").classList.toggle(
    "settled", task.status !== "running",
  );

  const bar = document.getElementById("podProgressBar");
  bar.style.width = `${percent}%`;
  bar.classList.toggle(
    "indeterminate",
    task.status === "running" && percent < 18 && Number(task.received_characters || 0) === 0,
  );

  const activeIndex = podPhaseIndex(stage);
  document.querySelectorAll("#podPhaseStrip [data-phase]").forEach((node, index) => {
    node.classList.toggle("done", index < activeIndex);
    node.classList.toggle("active", index === activeIndex);
  });

  const logNode = document.getElementById("podLog");
  const logText = logs.join("\n");
  document.getElementById("podLogCount").textContent = String(logs.length);
  if (logNode.textContent !== logText) {
    logNode.textContent = logText;
    logNode.scrollTop = logNode.scrollHeight;
  }
  const details = document.getElementById("podDetails");
  details.hidden = logs.length === 0;
  if (["failed", "cancelled"].includes(task.status)) details.open = true;
};

/* Keep the cancellation handler reusable after a failed or cancelled build. */
document.getElementById("buildPod").addEventListener("click", () => {
  queueMicrotask(() => {
    const cancel = document.getElementById("cancelPod");
    cancel.disabled = false;
    cancel.onclick = async () => {
      if (podBuildId) {
        cancel.disabled = true;
        await invoke("cancel_pod_build", podBuildId);
        cancel.disabled = false;
        cancel.textContent = "Cancelling…";
        return;
      }
      podDialog.close();
    };
  });
});
