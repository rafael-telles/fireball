const card = document.getElementById("card");

let tickTimer = null;
let activeMeeting = null; // { id, name, started_at, fake, backend, language, status }
let renderedMode = null; // "start" | "active" | "offline" — evita recriar o DOM a cada tick

// Rótulo de cada status vivo que o daemon pode reportar. 'stopping' existe
// porque parar não é instantâneo: o engine ainda empacota os .wav e drena a
// última transcrição depois do sinal, e a janela mostra isso em vez de
// mentir que já acabou.
const STATUS_LABEL = {
  starting: "INICIANDO",
  recording: "GRAVANDO",
  stopping: "PARANDO…",
};

function formatElapsed(startedAtIso) {
  const started = new Date(startedAtIso).getTime();
  const secs = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function setError(msg) {
  const el = document.getElementById("error");
  if (el) el.textContent = msg || "";
}

// Toda chamada ao daemon volta como {ok, result} ou {ok:false, error, kind}.
function unwrap(res) {
  if (!res || res.ok !== true) throw new Error((res && res.error) || "falha falando com o daemon");
  return res.result;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

function renderOffline(message) {
  card.innerHTML = `
    <div class="meeting-name">Daemon fora do ar</div>
    <div class="meeting-meta">${escapeHtml(message)}</div>
    <button class="primary" id="retry-btn">Tentar de novo</button>
  `;
  document.getElementById("retry-btn").addEventListener("click", () => {
    renderedMode = null;
    refresh();
  });
}

function renderStart(backends) {
  card.innerHTML = `
    <div class="field">
      <label for="name">Nome da reunião</label>
      <input type="text" id="name" value="Reunião" />
    </div>
    <div class="field">
      <label for="backend">Backend de transcrição</label>
      <select id="backend">
        ${backends.map((b) => `<option value="${b}">${b}</option>`).join("")}
      </select>
    </div>
    <div class="field checkbox-row">
      <input type="checkbox" id="real" />
      <label for="real" style="margin:0">Gravação real (mic + sistema) — sem marcar, roda simulado</label>
    </div>
    <button class="primary" id="start-btn">Iniciar reunião</button>
    <div class="error" id="error"></div>
  `;
  document.getElementById("start-btn").addEventListener("click", onStart);
}

function metaLine(status) {
  const m = status.meeting;
  const modeLabel = m.fake ? "simulado" : `real · ${m.backend || "?"}`;
  return `${modeLabel} · ${status.segments_total} segmento(s)` +
    (status.actions_pending ? ` · ${status.actions_pending} ação(ões) pendente(s)` : "");
}

function renderActive(status) {
  const m = status.meeting;
  activeMeeting = m;
  card.innerHTML = `
    <div class="status-row"><div class="status-dot"></div><span style="font-size:12px;color:var(--text-dim)" id="status-label">${STATUS_LABEL[m.status] || m.status.toUpperCase()}</span></div>
    <div class="meeting-name">${escapeHtml(m.name)}</div>
    <div class="meeting-meta">${metaLine(status)}</div>
    <div class="elapsed" id="elapsed">${formatElapsed(m.started_at)}</div>
    <button class="danger" id="stop-btn">Parar reunião</button>
    <div class="error" id="error"></div>
  `;
  const stopBtn = document.getElementById("stop-btn");
  stopBtn.addEventListener("click", onStop);
  stopBtn.disabled = m.status === "stopping";
}

async function refresh() {
  let status;
  try {
    status = unwrap(await window.pywebview.api.get_status());
  } catch (err) {
    if (renderedMode !== "offline") {
      if (tickTimer) clearInterval(tickTimer);
      activeMeeting = null;
      renderOffline(String(err.message || err));
      renderedMode = "offline";
    }
    return;
  }

  if (status.active) {
    const m = status.active.meeting;
    const sameMeeting = renderedMode === "active" && activeMeeting && activeMeeting.id === m.id;
    if (!sameMeeting) {
      if (tickTimer) clearInterval(tickTimer);
      renderActive(status.active);
      renderedMode = "active";
      tickTimer = setInterval(() => {
        const el = document.getElementById("elapsed");
        if (el && activeMeeting) el.textContent = formatElapsed(activeMeeting.started_at);
      }, 1000);
    } else {
      // já montado — só atualiza os números e o status, sem recriar o DOM
      const previousStatus = activeMeeting.status;
      activeMeeting = m;
      const meta = document.querySelector(".meeting-meta");
      if (meta) meta.textContent = metaLine(status.active);
      if (previousStatus !== m.status) {
        const label = document.getElementById("status-label");
        if (label) label.textContent = STATUS_LABEL[m.status] || m.status.toUpperCase();
        const stopBtn = document.getElementById("stop-btn");
        if (stopBtn) stopBtn.disabled = m.status === "stopping";
      }
    }
  } else if (renderedMode !== "start") {
    if (tickTimer) clearInterval(tickTimer);
    activeMeeting = null;
    renderStart(await window.pywebview.api.list_backends());
    renderedMode = "start";
  }
}

async function onStart() {
  const name = document.getElementById("name").value.trim() || "Reunião";
  const backend = document.getElementById("backend").value;
  const real = document.getElementById("real").checked;
  const btn = document.getElementById("start-btn");
  btn.disabled = true;
  setError("");
  try {
    unwrap(await window.pywebview.api.start_meeting(name, !real, backend));
    await refresh();
  } catch (err) {
    setError(String(err.message || err));
    btn.disabled = false;
  }
}

async function onStop() {
  if (!activeMeeting) return;
  const btn = document.getElementById("stop-btn");
  btn.disabled = true;
  setError("");
  try {
    unwrap(await window.pywebview.api.stop_meeting(activeMeeting.id));
    await refresh();
  } catch (err) {
    setError(String(err.message || err));
    btn.disabled = false;
  }
}

window.addEventListener("pywebviewready", refresh);
setInterval(refresh, 3000);
