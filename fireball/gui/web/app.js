const card = document.getElementById("card");

let tickTimer = null;
let activeMeeting = null; // { id, name, started_at, fake, backend, language }
let renderedMode = null; // "start" | "active" — evita recriar o form enquanto o usuário digita

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

function renderActive(status) {
  const m = status.meeting;
  activeMeeting = m;
  const modeLabel = m.fake ? "simulado" : `real · ${m.backend || "?"}`;
  card.innerHTML = `
    <div class="status-row"><div class="status-dot"></div><span style="font-size:12px;color:var(--text-dim)">GRAVANDO</span></div>
    <div class="meeting-name">${escapeHtml(m.name)}</div>
    <div class="meeting-meta">${modeLabel} · ${status.segments_total} segmento(s)${status.actions_pending ? ` · ${status.actions_pending} ação(ões) pendente(s)` : ""}</div>
    <div class="elapsed" id="elapsed">${formatElapsed(m.started_at)}</div>
    <button class="danger" id="stop-btn">Parar reunião</button>
    <div class="error" id="error"></div>
  `;
  document.getElementById("stop-btn").addEventListener("click", onStop);
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

async function refresh() {
  try {
    const status = await window.pywebview.api.get_status();

    if (status.active) {
      const sameMeeting = renderedMode === "active" && activeMeeting && activeMeeting.id === status.active.meeting.id;
      if (!sameMeeting) {
        if (tickTimer) clearInterval(tickTimer);
        renderActive(status.active);
        renderedMode = "active";
        tickTimer = setInterval(() => {
          const el = document.getElementById("elapsed");
          if (el && activeMeeting) el.textContent = formatElapsed(activeMeeting.started_at);
        }, 1000);
      } else {
        // já montado — só atualiza os números, sem recriar o DOM
        activeMeeting = status.active.meeting;
        const meta = document.querySelector(".meeting-meta");
        if (meta) {
          const modeLabel = activeMeeting.fake ? "simulado" : `real · ${activeMeeting.backend || "?"}`;
          meta.textContent = `${modeLabel} · ${status.active.segments_total} segmento(s)` +
            (status.active.actions_pending ? ` · ${status.active.actions_pending} ação(ões) pendente(s)` : "");
        }
      }
    } else if (renderedMode !== "start") {
      if (tickTimer) clearInterval(tickTimer);
      activeMeeting = null;
      const backends = await window.pywebview.api.list_backends();
      renderStart(backends);
      renderedMode = "start";
    }
  } catch (err) {
    setError(String(err));
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
    await window.pywebview.api.start_meeting(name, !real, backend);
    await refresh();
  } catch (err) {
    setError(String(err));
    btn.disabled = false;
  }
}

async function onStop() {
  if (!activeMeeting) return;
  const btn = document.getElementById("stop-btn");
  btn.disabled = true;
  setError("");
  try {
    await window.pywebview.api.stop_meeting(activeMeeting.id);
    await refresh();
  } catch (err) {
    setError(String(err));
    btn.disabled = false;
  }
}

window.addEventListener("pywebviewready", refresh);
setInterval(refresh, 3000);
