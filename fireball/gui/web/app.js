// Front-end da janela do Fireball.
//
// Três telas: a inicial (formulário + histórico de reuniões), a da reunião
// (transcrição em formato de chat) e a de configuração (backends e idioma).
// O chat é *incremental* de propósito — a
// cada volta do polling pedimos só os segmentos com seq maior que o último
// desenhado e damos append no DOM. Redesenhar a conversa inteira a cada
// segundo jogaria fora a rolagem do usuário e piscaria a tela.

const el = (id) => document.getElementById(id);

// Rótulo de cada status vivo. 'stopping' existe porque parar não é
// instantâneo: o engine ainda empacota os .wav e drena a última transcrição
// depois do sinal, e a janela mostra isso em vez de mentir que já acabou.
const STATUS_LABEL = {
  starting: "INICIANDO",
  recording: "GRAVANDO",
  stopping: "PARANDO…",
};

const LIVE_STATUSES = new Set(["starting", "recording", "stopping"]);

// [classe do selo, texto] por status — inclui os terminais, que só aparecem
// na lista de reuniões passadas.
const BADGE = {
  starting: ["live", "iniciando"],
  recording: ["live", "gravando"],
  stopping: ["warn", "parando"],
  stopped: ["ok", "parada"],
  finalizing: ["warn", "finalizando"],
  finalized: ["ok", "finalizada"],
  finalize_failed: ["bad", "falha ao finalizar"],
  crashed: ["bad", "quebrada"],
  failed: ["bad", "falhou"],
};

// Cor do nome de quem fala. "Você" é o microfone daqui, então herda o laranja
// da marca; os outros ganham uma cor estável derivada do nome, o que separa
// falantes de olho num roteiro simulado com vários nomes.
const SPEAKER_COLORS = ["#7aa2f7", "#bb9af7", "#7dcfff", "#9ece6a", "#e0af68", "#f7768e"];

const POLL_CHAT_MS = 1000;
const POLL_STATUS_MS = 2000;

const state = {
  view: "home", // "home" | "meeting" | "settings"
  active: null, // meeting.json da reunião gravando agora, ou null
  open: null, // reunião aberta na tela da reunião (ativa ou passada)
  autoOpened: null, // id já aberto sozinho, pra não reabrir depois do "voltar"
  settings: null, // preferências vindas do daemon (backends, idioma)
  backends: { realtime: [], final: [] },
};

const VIEWS = { home: "view-home", meeting: "view-meeting", settings: "view-settings" };

function showView(name) {
  state.view = name;
  for (const [key, id] of Object.entries(VIEWS)) el(id).classList.toggle("hidden", key !== name);
}

// ------------------------------------------------------------------ ponte

// Toda chamada ao daemon volta como {ok, result} ou {ok:false, error, kind}.
async function api(name, ...args) {
  const res = await window.pywebview.api[name](...args);
  if (!res || res.ok !== true) throw new Error((res && res.error) || "falha falando com o daemon");
  return res.result;
}

function setBanner(msg) {
  const banner = el("banner");
  banner.textContent = msg || "";
  banner.classList.toggle("hidden", !msg);
}

function setError(id, msg) {
  el(id).textContent = msg || "";
}

// --------------------------------------------------------------- formatos

function clock(totalSeconds) {
  const secs = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function elapsedSince(startedAtIso) {
  return clock((Date.now() - new Date(startedAtIso).getTime()) / 1000);
}

function dateLabel(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function durationLabel(meeting) {
  if (!meeting.started_at || !meeting.ended_at) return "";
  const secs = (new Date(meeting.ended_at) - new Date(meeting.started_at)) / 1000;
  return secs > 0 ? clock(secs) : "";
}

// O segmento ao vivo tem carimbo absoluto (ts); o da transcrição final tem
// deslocamento em segundos desde o começo da gravação (start). Os dois viram
// hora na bolha, cada um no que faz sentido pra sua origem.
function segmentTime(seg) {
  if (seg.ts) {
    const d = new Date(seg.ts);
    return isNaN(d) ? "" : d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  }
  if (typeof seg.start === "number") return clock(seg.start);
  return "";
}

function speakerColor(name) {
  if (name === "Você") return "var(--accent)";
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return SPEAKER_COLORS[hash % SPEAKER_COLORS.length];
}

// ------------------------------------------------------------------ chat

function isNearBottom(node) {
  // 60px de folga: quem está lendo o histórico mais acima não é arrastado
  // para baixo quando chega fala nova.
  return node.scrollHeight - node.scrollTop - node.clientHeight < 60;
}

function chatMessage(seg, previousSpeaker) {
  const speaker = seg.speaker || "?";
  const groupStart = speaker !== previousSpeaker;

  const wrap = document.createElement("div");
  wrap.className = "msg";
  if (speaker === "Você") wrap.classList.add("mine");
  if (groupStart) wrap.classList.add("group-start");

  if (groupStart) {
    const name = document.createElement("div");
    name.className = "msg-speaker";
    name.textContent = speaker;
    name.style.setProperty("--speaker-color", speakerColor(speaker));
    wrap.appendChild(name);
  }

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = seg.text || "";
  wrap.appendChild(bubble);

  const stamp = segmentTime(seg);
  if (stamp) {
    const time = document.createElement("div");
    time.className = "msg-time";
    time.textContent = stamp;
    wrap.appendChild(time);
  }
  return wrap;
}

function chatPlaceholder(text) {
  const chat = el("chat");
  chat.innerHTML = "";
  const empty = document.createElement("div");
  empty.className = "chat-empty";
  empty.textContent = text;
  chat.appendChild(empty);
}

function resetChat() {
  const o = state.open;
  o.lastSeq = 0;
  o.lastSpeaker = null;
  chatPlaceholder(
    o.live ? "Esperando a primeira fala…" : "Esta reunião não tem transcrição nesta fonte."
  );
}

function appendSegments(segs) {
  const chat = el("chat");
  const placeholder = chat.querySelector(".chat-empty");
  if (placeholder) placeholder.remove();

  const stick = isNearBottom(chat);
  const o = state.open;
  for (const seg of segs) {
    chat.appendChild(chatMessage(seg, o.lastSpeaker));
    o.lastSpeaker = seg.speaker || "?";
    o.lastSeq = seg.seq;
  }
  if (stick) chat.scrollTop = chat.scrollHeight;
}

async function pollChat() {
  const o = state.open;
  if (!o) return;
  let segs;
  try {
    segs = await api("transcript", o.id, o.lastSeq, o.source);
    setBanner("");
  } catch (err) {
    setBanner(String(err.message || err));
    return;
  }
  if (segs.length) appendSegments(segs);
}

// ------------------------------------------------------- view da reunião

function metaLine(o) {
  const m = o.meeting;
  const parts = [];
  if (o.live) {
    parts.push(STATUS_LABEL[m.status] || String(m.status).toUpperCase());
  } else {
    const when = dateLabel(m.started_at);
    const dur = durationLabel(m);
    parts.push(dur ? `${when} · ${dur}` : when);
  }
  parts.push(m.fake ? "simulado" : `real${m.backend ? ` · ${m.backend}` : ""}`);
  parts.push(`${o.segments} segmento(s)`);
  if (o.actionsPending) parts.push(`${o.actionsPending} ação(ões) pendente(s)`);
  return parts.filter(Boolean).join(" · ");
}

function renderMeetingHeader() {
  const o = state.open;
  const m = o.meeting;
  el("mv-name").textContent = m.name || o.id;
  el("mv-meta").textContent = metaLine(o);

  el("mv-elapsed").classList.toggle("hidden", !o.live);
  if (o.live) el("mv-elapsed").textContent = elapsedSince(m.started_at);

  const stop = el("stop-btn");
  stop.classList.toggle("hidden", !o.live);
  stop.disabled = m.status === "stopping";
  stop.textContent = m.status === "stopping" ? "Parando…" : "Parar";
}

function renderSourceSwitch() {
  const o = state.open;
  const box = el("source-switch");
  // só faz sentido escolher quando existem as duas transcrições: ao vivo há
  // apenas a do tempo real.
  box.classList.toggle("hidden", o.live || !o.hasFinal);
  for (const chip of box.querySelectorAll(".chip")) {
    chip.classList.toggle("selected", chip.dataset.source === o.source);
  }
}

async function openMeeting(id, row) {
  setError("meeting-error", "");
  showView("meeting");
  chatPlaceholder("Carregando…");

  let detail;
  try {
    detail = await api("meeting_status", id);
  } catch (err) {
    setBanner(String(err.message || err));
    goHome();
    return;
  }

  const live = LIVE_STATUSES.has(detail.meeting.status);
  // uma reunião viva nunca tem transcrição final (ela só é escrita depois do
  // `finalize`), então nesse caso nem vale a consulta.
  let hasFinal = false;
  if (!live) {
    hasFinal = row ? !!row.has_final : (await api("transcript", id, 0, "final")).length > 0;
  }

  state.open = {
    id,
    meeting: detail.meeting,
    segments: detail.segments_total,
    actionsPending: detail.actions_pending,
    live,
    hasFinal,
    source: hasFinal ? "final" : "realtime",
    lastSeq: 0,
    lastSpeaker: null,
  };

  renderMeetingHeader();
  renderSourceSwitch();
  resetChat();
  await pollChat();
  el("chat").scrollTop = el("chat").scrollHeight;
}

function goHome() {
  state.open = null;
  showView("home");
  loadHistory();
}

/** Reconcilia a reunião aberta com o que o daemon diz estar ativo. */
async function syncOpenMeeting(activeDetail) {
  const o = state.open;
  if (!o) return;

  if (activeDetail && activeDetail.meeting.id === o.id) {
    o.meeting = activeDetail.meeting;
    o.segments = activeDetail.segments_total;
    o.actionsPending = activeDetail.actions_pending;
    o.live = true;
    renderMeetingHeader();
    return;
  }

  if (!o.live) return;

  // A reunião aberta acabou de sair do ar — pelo botão daqui, pela bandeja ou
  // pela CLI. Drena o que o engine escreveu depois do sinal antes de virar
  // histórico, senão as últimas falas nunca apareceriam.
  o.live = false;
  await pollChat();
  try {
    const detail = await api("meeting_status", o.id);
    o.meeting = detail.meeting;
    o.segments = detail.segments_total;
    o.actionsPending = detail.actions_pending;
  } catch (err) {
    setBanner(String(err.message || err));
  }
  renderMeetingHeader();
  renderSourceSwitch();
}

// -------------------------------------------------------- histórico/home

function meetingRow(row) {
  const status = row.status || "?";
  const [badgeClass, badgeText] = BADGE[status] || ["", status];

  const button = document.createElement("button");
  button.className = "meeting-row" + (LIVE_STATUSES.has(status) ? " live" : "");
  button.addEventListener("click", () => openMeeting(row.id, row));

  const info = document.createElement("div");
  info.className = "info";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = row.name || row.id;
  info.appendChild(title);

  const bits = [dateLabel(row.started_at), durationLabel(row)];
  bits.push(`${row.segments} segmento(s)`);
  if (row.has_final) bits.push("transcrição final");
  if (row.fake) bits.push("simulado");

  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = bits.filter(Boolean).join(" · ");
  info.appendChild(sub);

  const badge = document.createElement("div");
  badge.className = `badge ${badgeClass}`;
  badge.textContent = badgeText;

  button.appendChild(info);
  button.appendChild(badge);
  return button;
}

async function loadHistory() {
  let rows;
  try {
    rows = await api("list_meetings");
    setBanner("");
  } catch (err) {
    setBanner(String(err.message || err));
    return;
  }

  const list = el("meeting-list");
  list.innerHTML = "";
  el("past-count").textContent = rows.length ? `${rows.length} no total` : "";

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Nenhuma reunião ainda. Inicie a primeira aí em cima.";
    list.appendChild(empty);
    return;
  }
  for (const row of rows) list.appendChild(meetingRow(row));
}

function renderLiveChip() {
  const chip = el("live-chip");
  chip.classList.toggle("hidden", !state.active);
  if (!state.active) return;
  el("live-chip-label").textContent =
    STATUS_LABEL[state.active.status] || String(state.active.status).toUpperCase();
  el("live-chip-time").textContent = elapsedSince(state.active.started_at);
}

async function refreshStatus() {
  let status;
  try {
    status = await api("get_status");
    setBanner("");
  } catch (err) {
    setBanner(String(err.message || err));
    return;
  }

  const detail = status.active;
  const wasActive = state.active && state.active.id;
  state.active = detail ? detail.meeting : null;
  renderLiveChip();

  await syncOpenMeeting(detail);

  if (!detail) {
    state.autoOpened = null;
    // reunião acabou enquanto a lista estava na tela: os números mudaram
    if (wasActive && state.view === "home") loadHistory();
    return;
  }

  // Reunião nova (iniciada aqui, pela CLI ou por outra janela) abre sozinha —
  // é o que a pessoa quer ver. Só uma vez por reunião: quem voltou pra lista
  // de propósito não é jogado de volta pro chat a cada 2s.
  if (state.view === "home" && state.autoOpened !== detail.meeting.id) {
    state.autoOpened = detail.meeting.id;
    await openMeeting(detail.meeting.id);
  }
}

// ---------------------------------------------------------- configuração

function fillSelect(id, options, selected) {
  const select = el(id);
  select.innerHTML = "";
  for (const name of options) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    option.selected = name === selected;
    select.appendChild(option);
  }
}

/** Linha do formulário de início que lembra com o que a reunião vai rodar. */
function renderConfigSummary() {
  const s = state.settings;
  if (!s) return;
  const live = s.transcribe_live ? `ao vivo: ${s.realtime_backend}` : "sem transcrição ao vivo";
  el("config-summary-text").textContent = `${live} · final: ${s.final_backend} · ${s.language}`;
}

function renderSettings() {
  const s = state.settings;
  fillSelect("set-live-backend", state.backends.realtime, s.realtime_backend);
  fillSelect("set-final-backend", state.backends.final, s.final_backend);
  el("set-live-on").checked = s.transcribe_live;
  el("set-language").value = s.language;
}

async function loadSettings() {
  state.backends = await window.pywebview.api.backend_options();
  state.settings = await api("get_settings");
  renderConfigSummary();
}

function openSettings() {
  setError("settings-error", "");
  el("settings-note").textContent = "";
  renderSettings(); // sempre do estado conhecido, nunca do que ficou na tela
  showView("settings");
}

async function onSaveSettings() {
  const button = el("settings-save");
  button.disabled = true;
  setError("settings-error", "");
  try {
    state.settings = await api("save_settings", {
      realtime_backend: el("set-live-backend").value,
      transcribe_live: el("set-live-on").checked,
      final_backend: el("set-final-backend").value,
      language: el("set-language").value,
    });
    renderSettings(); // o daemon é quem diz o que ficou valendo
    renderConfigSummary();
    el("settings-note").textContent = "salvo";
    setTimeout(() => (el("settings-note").textContent = ""), 2500);
  } catch (err) {
    setError("settings-error", String(err.message || err));
  } finally {
    button.disabled = false;
  }
}

// -------------------------------------------------------------- comandos

async function onStart() {
  const name = el("name").value.trim() || "Reunião";
  const button = el("start-btn");
  button.disabled = true;
  setError("start-error", "");
  try {
    // só o nome: modo, backend e idioma são decididos fora daqui — ver
    // `Api.start_meeting`
    const meeting = await api("start_meeting", name);
    state.autoOpened = meeting.id; // já vamos abrir aqui; o polling não repete
    await openMeeting(meeting.id);
    await refreshStatus();
  } catch (err) {
    setError("start-error", String(err.message || err));
  } finally {
    button.disabled = false;
  }
}

async function onStop() {
  const o = state.open;
  if (!o) return;
  const button = el("stop-btn");
  button.disabled = true;
  setError("meeting-error", "");
  try {
    o.meeting = await api("stop_meeting", o.id);
    renderMeetingHeader();
  } catch (err) {
    setError("meeting-error", String(err.message || err));
    button.disabled = false;
  }
}

function onPickSource(event) {
  const source = event.currentTarget.dataset.source;
  const o = state.open;
  if (!o || o.source === source) return;
  o.source = source;
  renderSourceSwitch();
  resetChat();
  pollChat();
}

// ------------------------------------------------------------------ boot

function tick() {
  // o relógio anda localmente, sem ida ao daemon: só o polling de status
  // (2s) descobre mudança de verdade.
  if (state.active) el("live-chip-time").textContent = elapsedSince(state.active.started_at);
  if (state.open && state.open.live) {
    el("mv-elapsed").textContent = elapsedSince(state.open.meeting.started_at);
  }
}

async function boot() {
  el("start-btn").addEventListener("click", onStart);
  el("stop-btn").addEventListener("click", onStop);
  el("back-btn").addEventListener("click", goHome);
  el("live-chip").addEventListener("click", () => {
    if (state.active) openMeeting(state.active.id);
  });
  for (const chip of el("source-switch").querySelectorAll(".chip")) {
    chip.addEventListener("click", onPickSource);
  }
  el("settings-btn").addEventListener("click", openSettings);
  el("config-link").addEventListener("click", openSettings);
  el("settings-back").addEventListener("click", goHome);
  el("settings-save").addEventListener("click", onSaveSettings);

  await loadSettings();
  await loadHistory();
  await refreshStatus();

  setInterval(refreshStatus, POLL_STATUS_MS);
  setInterval(() => {
    if (state.view === "meeting" && state.open && state.open.live) pollChat();
  }, POLL_CHAT_MS);
  setInterval(tick, 1000);
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
