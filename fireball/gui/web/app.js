// Front-end da janela do Fireball.
//
// A barra lateral é a navegação e está sempre na tela: a reunião gravando
// agora fica à vista mesmo enquanto se mexe na configuração — o app é sobre
// uma gravação em curso, e escondê-la atrás de um "voltar" seria esconder a
// única coisa que exige ação.
//
// Três telas no painel: início (nova reunião), reunião (abas Resumo ·
// Transcrição · Notas) e configuração. O chat é *incremental* de propósito —
// a cada volta do polling pedimos só os segmentos com seq maior que o último
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

// Texto curto por status, para a linha de baixo de cada item da lista.
const STATUS_SHORT = {
  starting: "iniciando",
  recording: "gravando",
  stopping: "parando",
  stopped: "parada",
  finalizing: "finalizando",
  finalized: "finalizada",
  finalize_failed: "falha ao finalizar",
  crashed: "quebrada",
  failed: "falhou",
};

// Cor do nome de quem fala. Os dois falantes reais de hoje são fixos: "Você" é
// o microfone daqui e herda o acento da marca, "Outros participantes" é o
// monitor do sistema e fica no azul. São só esses dois enquanto não houver
// diarização, e é essa oposição que faz a conversa ser legível de relance.
//
// A paleta abaixo é para nomes além desses dois (o roteiro simulado do
// `--fake`, e a diarização quando chegar). Nenhum tom avermelhado aqui de
// propósito: derivado por hash, um vermelho cairia em cima do acento e faria
// um participante qualquer passar por "Você".
const SPEAKER_COLORS = ["#bb9af7", "#7dcfff", "#9ece6a", "#e0af68", "#2ac3de"];

const POLL_CHAT_MS = 1000;
const POLL_STATUS_MS = 2000;
// Digitar não pode virar uma escrita em disco por tecla; espera a pausa.
const NOTES_SAVE_MS = 900;

const state = {
  view: "home", // "home" | "meeting" | "settings"
  active: null, // meeting.json da reunião gravando agora, ou null
  open: null, // reunião aberta na tela da reunião (ativa ou passada)
  autoOpened: null, // id já aberto sozinho, pra não reabrir depois de sair
  settings: null, // preferências vindas do daemon (backends, idioma)
  backends: { realtime: [], final: [] },
  rows: [], // histórico como veio do daemon, mais recentes primeiro
  filter: "", // busca da barra lateral
  tab: "transcricao",
  notes: { loadedFor: null, saved: null, timer: null },
};

const VIEWS = { home: "view-home", meeting: "view-meeting", settings: "view-settings" };
const TABS = { resumo: "pane-resumo", transcricao: "pane-transcricao", notas: "pane-notas" };

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

/** Alerta em cartão: título forte e detalhe embaixo, como no desenho. */
function setAlert(id, title, detail) {
  const box = el(id);
  box.classList.toggle("hidden", !title);
  if (!title) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = "";
  const body = document.createElement("div");
  const strong = document.createElement("b");
  strong.textContent = title;
  body.appendChild(strong);
  if (detail) body.appendChild(document.createTextNode(detail));
  box.appendChild(body);
}

const errText = (err) => String((err && err.message) || err);

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

function timeLabel(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

function dayLabel(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });
}

function dateLabel(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function longDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleDateString("pt-BR", { weekday: "short", day: "numeric", month: "long", year: "numeric" });
}

function durationLabel(meeting) {
  if (!meeting.started_at || !meeting.ended_at) return "";
  const secs = (new Date(meeting.ended_at) - new Date(meeting.started_at)) / 1000;
  return secs > 0 ? clock(secs) : "";
}

/** Quantos dias inteiros atrás — por dia de calendário, não por 24h. */
function daysAgo(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return null;
  const now = new Date();
  const a = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const b = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  return Math.round((a - b) / 86400000);
}

// O segmento ao vivo tem carimbo absoluto (ts); o da transcrição final tem
// deslocamento em segundos desde o começo da gravação (start). Os dois viram
// hora na bolha, cada um no que faz sentido pra sua origem.
function segmentTime(seg) {
  if (seg.ts) return timeLabel(seg.ts);
  if (typeof seg.start === "number") return clock(seg.start);
  return "";
}

function speakerColor(name) {
  if (name === "Você") return "var(--accent)";
  if (name === "Outros participantes") return "var(--speaker)";
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
  o.lastSegmentAt = null;
  if (o.live && o.meeting.transcribe_live === false) {
    chatPlaceholder(
      "Esta reunião está sendo só gravada — a transcrição ao vivo está desligada na configuração."
    );
  } else {
    chatPlaceholder(
      o.live ? "Esperando a primeira fala…" : "Esta reunião não tem transcrição nesta fonte."
    );
  }
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
    if (seg.ts) o.lastSegmentAt = new Date(seg.ts).getTime();
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
    setBanner(errText(err));
    return;
  }
  if (segs.length) appendSegments(segs);
  renderLiveBar();
}

/** Rodapé da aba Transcrição: ao vivo mostra o pipeline, encerrada mostra o
    lugar (ainda vazio) do player. */
function renderLiveBar() {
  const o = state.open;
  if (!o) return;
  const live = el("live-bar");
  const player = el("player-bar");
  live.classList.toggle("hidden", !o.live);
  player.classList.toggle("hidden", o.live);
  if (!o.live) return;

  if (o.meeting.transcribe_live === false) {
    el("live-bar-title").textContent = "Só gravando o áudio";
    el("live-bar-detail").textContent = "transcrição ao vivo desligada — a final roda no fim.";
    return;
  }
  el("live-bar-title").textContent = "Transcrevendo ao vivo";
  const bits = [];
  if (o.lastSegmentAt) {
    bits.push(`última fala transcrita há ${Math.max(0, Math.round((Date.now() - o.lastSegmentAt) / 1000))} s`);
  } else {
    bits.push("nenhuma fala transcrita ainda");
  }
  bits.push(`${o.lastSeq} segmento(s)`);
  el("live-bar-detail").textContent = bits.join(" · ");
}

// ------------------------------------------------------- view da reunião

function metaLine(o) {
  const m = o.meeting;
  const parts = [];
  if (o.live) {
    parts.push(STATUS_LABEL[m.status] || String(m.status).toUpperCase());
    parts.push(`desde ${timeLabel(m.started_at)}`);
  } else {
    const when = dateLabel(m.started_at);
    const dur = durationLabel(m);
    parts.push(dur ? `${when} · ${dur}` : when);
    parts.push(STATUS_SHORT[m.status] || m.status);
  }
  parts.push(m.fake ? "simulado" : `real${m.backend ? ` · ${m.backend}` : ""}`);
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

  // Finalizar é a transcrição final sobre o áudio inteiro. Só faz sentido
  // depois que a gravação fechou os .wav — antes disso não há áudio completo.
  const finalize = el("finalize-btn");
  const finished = !o.live && m.status !== "finalizing";
  finalize.classList.toggle("hidden", !finished && m.status !== "finalizing");
  finalize.disabled = m.status === "finalizing";
  finalize.textContent =
    m.status === "finalizing" ? "Finalizando…" : o.hasFinal ? "Retranscrever" : "Finalizar";
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

function renderFicha() {
  const o = state.open;
  const m = o.meeting;

  const rename = el("mv-rename");
  if (document.activeElement !== rename) rename.value = m.name || o.id;

  const day = longDate(m.started_at);
  const from = timeLabel(m.started_at);
  const to = m.ended_at ? timeLabel(m.ended_at) : null;
  el("mv-when").textContent = [day, to ? `${from} – ${to}` : `${from} — em andamento`]
    .filter(Boolean)
    .join(" · ");
  el("mv-path").textContent = o.path || "";
}

function renderActions() {
  const list = el("actions-list");
  const actions = (state.open && state.open.actions) || [];
  list.innerHTML = "";
  el("actions-count").textContent = actions.length ? `${actions.length} no total` : "";

  if (!actions.length) {
    const empty = document.createElement("div");
    empty.className = "slab short";
    const note = document.createElement("div");
    note.className = "slab-note";
    note.textContent =
      "Nenhuma ação registrada. Quem cria ação hoje é o Claude durante a reunião (fireball action add).";
    empty.appendChild(note);
    list.appendChild(empty);
    return;
  }

  for (const action of actions) {
    const row = document.createElement("div");
    row.className = "action-row" + (action.status === "done" ? " done" : "");

    const box = document.createElement("div");
    box.className = "box";
    box.textContent = action.status === "done" ? "✓" : "";
    row.appendChild(box);

    const body = document.createElement("div");
    const title = document.createElement("div");
    title.className = "action-title";
    title.textContent = action.title || action.id;
    body.appendChild(title);
    if (action.detail) {
      const detail = document.createElement("div");
      detail.className = "action-detail";
      detail.textContent = action.detail;
      body.appendChild(detail);
    }
    row.appendChild(body);
    list.appendChild(row);
  }
}

function showTab(name) {
  state.tab = name;
  for (const [key, id] of Object.entries(TABS)) el(id).classList.toggle("hidden", key !== name);
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("selected", tab.dataset.tab === name);
  }
  if (name === "notas") loadNotes();
  if (name === "resumo") loadActions();
  // sair do editor sem esperar o debounce: trocar de aba é uma pausa
  if (name !== "notas") flushNotes();
}

async function loadActions() {
  const o = state.open;
  if (!o) return;
  try {
    o.actions = await api("actions", o.id);
  } catch (err) {
    o.actions = [];
    setBanner(errText(err));
  }
  if (state.open === o) renderActions();
}

async function openMeeting(id, row) {
  await flushNotes(); // o que estava no editor da reunião anterior não se perde
  setAlert("meeting-error", "");
  showView("meeting");
  chatPlaceholder("Carregando…");

  let detail;
  try {
    detail = await api("meeting_status", id);
  } catch (err) {
    setBanner(errText(err));
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
    path: detail.path,
    actions: null,
    live,
    hasFinal,
    source: hasFinal ? "final" : "realtime",
    lastSeq: 0,
    lastSpeaker: null,
    lastSegmentAt: null,
  };
  state.notes.loadedFor = null;

  renderMeetingHeader();
  renderSourceSwitch();
  renderFicha();
  renderMeetingList(); // marca a linha da barra lateral
  resetChat();
  showTab(state.tab === "notas" ? "transcricao" : state.tab);
  await pollChat();
  el("chat").scrollTop = el("chat").scrollHeight;
}

function goHome() {
  flushNotes();
  state.open = null;
  showView("home");
  renderHome();
  renderMeetingList();
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
    renderFicha();
    return;
  }

  if (!o.live) {
    // Não está viva, mas o status muda sozinho enquanto o `finalize` roda —
    // sem isso o botão ficaria "Finalizando…" para sempre.
    if (o.meeting.status === "finalizing") await refreshOpenMeeting();
    return;
  }

  // A reunião aberta acabou de sair do ar — pelo botão daqui, pela bandeja ou
  // pela CLI. Drena o que o engine escreveu depois do sinal antes de virar
  // histórico, senão as últimas falas nunca apareceriam.
  o.live = false;
  await pollChat();
  await refreshOpenMeeting();
}

async function refreshOpenMeeting() {
  const o = state.open;
  if (!o) return;
  try {
    const detail = await api("meeting_status", o.id);
    if (state.open !== o) return;
    const wasFinalizing = o.meeting.status === "finalizing";
    o.meeting = detail.meeting;
    o.segments = detail.segments_total;
    o.actionsPending = detail.actions_pending;
    o.path = detail.path;
    o.live = LIVE_STATUSES.has(detail.meeting.status);
    if (wasFinalizing && detail.meeting.status === "finalized") {
      o.hasFinal = true;
      o.source = "final";
      resetChat();
      await pollChat();
    }
  } catch (err) {
    setBanner(errText(err));
  }
  renderMeetingHeader();
  renderSourceSwitch();
  renderFicha();
  renderLiveBar();
  renderMeetingList();
}

// ----------------------------------------------- barra lateral/histórico

/** Em que bloco da barra lateral esta reunião cai. */
function groupOf(iso) {
  const days = daysAgo(iso);
  if (days === null) return "Sem data";
  if (days <= 0) return "Hoje";
  if (days === 1) return "Ontem";
  if (days < 7) return "Esta semana";
  if (days < 30) return "Este mês";
  return "Mais antigas";
}

function sidebarRow(row) {
  const live = LIVE_STATUSES.has(row.status);
  const button = document.createElement("button");
  button.className = "sb-row";
  if (live) button.classList.add("live");
  if (state.open && state.open.id === row.id) button.classList.add("selected");
  button.addEventListener("click", () => openMeeting(row.id, row));

  const title = document.createElement("div");
  title.className = "sb-row-title";
  title.textContent = row.name || row.id;
  button.appendChild(title);

  // Hoje o horário basta; mais antiga precisa do dia. Depois vem a duração —
  // ou o status, quando ele diz mais que ela (gravando, quebrada).
  const days = daysAgo(row.started_at);
  const when = days === 0 ? timeLabel(row.started_at) : dayLabel(row.started_at);
  const right = live
    ? STATUS_SHORT[row.status]
    : durationLabel(row) || STATUS_SHORT[row.status] || "";

  const sub = document.createElement("div");
  sub.className = "sb-row-sub";
  sub.textContent = [when, right].filter(Boolean).join(" · ");
  button.appendChild(sub);
  return button;
}

function renderMeetingList() {
  const list = el("meeting-list");
  list.innerHTML = "";

  const needle = state.filter.trim().toLowerCase();
  const rows = state.rows
    .filter((r) => !needle || String(r.name || r.id).toLowerCase().includes(needle))
    // O daemon ordena pela pasta, cujo nome começa com a hora de criação — o
    // que quase sempre bate com started_at, mas não é a mesma coisa. Ordenar
    // aqui pelo campo que os títulos de grupo usam é o que garante que eles
    // saiam em ordem e apareçam uma vez só.
    .sort((a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0));

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "sb-empty";
    empty.textContent = needle
      ? "Nenhuma reunião com esse nome."
      : "Nenhuma reunião ainda. Comece a primeira aí em cima.";
    list.appendChild(empty);
    return;
  }

  // As linhas já vêm mais recentes primeiro, então os grupos saem em ordem
  // cronológica sem precisar ordená-los à parte.
  let current = null;
  for (const row of rows) {
    const group = groupOf(row.started_at);
    if (group !== current) {
      current = group;
      const head = document.createElement("div");
      head.className = "sb-group";
      head.textContent = group;
      list.appendChild(head);
    }
    list.appendChild(sidebarRow(row));
  }
}

async function loadHistory() {
  try {
    state.rows = await api("list_meetings");
    setBanner("");
  } catch (err) {
    setBanner(errText(err));
    return;
  }
  renderMeetingList();
}

function renderLiveCard() {
  const card = el("live-card");
  const active = state.active;
  card.classList.toggle("hidden", !active);
  if (!active) return;
  el("live-card-label").textContent = STATUS_LABEL[active.status] || String(active.status).toUpperCase();
  el("live-card-time").textContent = elapsedSince(active.started_at);
  el("live-card-name").textContent = active.name || active.id;
  el("live-card-stop").disabled = active.status === "stopping";
  el("live-card-stop").textContent = active.status === "stopping" ? "Parando…" : "Parar";
}

function renderHome() {
  const active = state.active;
  el("home-kicker").textContent = active
    ? `Gravando agora · ${active.name || active.id}`
    : "Ocioso · nada gravando";
  el("agenda-date").textContent = new Date().toLocaleDateString("pt-BR", {
    weekday: "short",
    day: "numeric",
    month: "long",
  });
}

async function refreshStatus() {
  let status;
  try {
    status = await api("get_status");
    setBanner("");
  } catch (err) {
    setBanner(errText(err));
    return;
  }

  const detail = status.active;
  const wasActive = state.active && state.active.id;
  state.active = detail ? detail.meeting : null;
  renderLiveCard();
  renderHome();

  await syncOpenMeeting(detail);

  if (!detail) {
    state.autoOpened = null;
    // reunião acabou enquanto a lista estava na tela: os números mudaram
    if (wasActive) loadHistory();
    return;
  }

  if (!wasActive) loadHistory(); // reunião nova entrou na lista

  // Reunião nova (iniciada aqui, pela CLI ou por outra janela) abre sozinha —
  // é o que a pessoa quer ver. Só uma vez por reunião: quem foi olhar outra
  // coisa de propósito não é jogado de volta pro chat a cada 2s.
  if (state.view === "home" && state.autoOpened !== detail.meeting.id) {
    state.autoOpened = detail.meeting.id;
    await openMeeting(detail.meeting.id);
  }
}

// ------------------------------------------------------------------ notas

async function loadNotes() {
  const o = state.open;
  if (!o || state.notes.loadedFor === o.id) return;
  const box = el("notes");
  try {
    const text = await api("notes", o.id);
    if (state.open !== o) return;
    box.value = text;
    state.notes.loadedFor = o.id;
    state.notes.saved = text;
    setNotesStatus("");
  } catch (err) {
    setBanner(errText(err));
  }
}

function setNotesStatus(text, ok) {
  const node = el("notes-status");
  node.textContent = text;
  node.classList.toggle("on", !!ok);
}

/** Salva já, se houver o que salvar. Chamado ao sair do editor — o debounce
    sozinho perderia o que foi digitado nos últimos instantes. */
async function flushNotes() {
  const o = state.open;
  if (!o || state.notes.loadedFor !== o.id) return;
  clearTimeout(state.notes.timer);
  const text = el("notes").value;
  if (text === state.notes.saved) return;
  try {
    await api("save_notes", o.id, text);
    state.notes.saved = text;
    setNotesStatus("salvo agora", true);
  } catch (err) {
    setNotesStatus("");
    setBanner(errText(err));
  }
}

function onNotesInput() {
  setNotesStatus("editando…");
  clearTimeout(state.notes.timer);
  state.notes.timer = setTimeout(flushNotes, NOTES_SAVE_MS);
}

// Botões da barra do editor: markdown de verdade no texto, não formatação
// simulada — o arquivo é markdown e é isso que a CLI e o Claude também leem.
const MD_WRAP = { bold: "**", italic: "_", code: "`" };
const MD_PREFIX = { h2: "## ", ul: "- ", ol: "1. ", task: "- [ ] ", quote: "> " };

function applyMarkdown(kind) {
  const box = el("notes");
  const { selectionStart: from, selectionEnd: to, value } = box;
  const selected = value.slice(from, to);

  let replacement;
  let caret;
  if (MD_WRAP[kind]) {
    const mark = MD_WRAP[kind];
    replacement = `${mark}${selected}${mark}`;
    caret = selected ? from + replacement.length : from + mark.length;
  } else if (kind === "link") {
    replacement = `[${selected || "texto"}](url)`;
    caret = from + replacement.length - 4;
  } else {
    // prefixo de bloco: cai no começo de cada linha da seleção
    const lineStart = value.lastIndexOf("\n", from - 1) + 1;
    const block = value.slice(lineStart, to) || "";
    replacement = block
      .split("\n")
      .map((line) => MD_PREFIX[kind] + line)
      .join("\n");
    box.setRangeText(replacement, lineStart, to, "end");
    box.focus();
    onNotesInput();
    return;
  }

  box.setRangeText(replacement, from, to, "end");
  box.selectionStart = box.selectionEnd = caret;
  box.focus();
  onNotesInput();
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
  flushNotes();
  setAlert("settings-error", "");
  el("settings-note").textContent = "";
  renderSettings(); // sempre do estado conhecido, nunca do que ficou na tela
  showView("settings");
}

async function onSaveSettings() {
  const button = el("settings-save");
  button.disabled = true;
  setAlert("settings-error", "");
  try {
    state.settings = await api("save_settings", {
      realtime_backend: el("set-live-backend").value,
      transcribe_live: el("set-live-on").checked,
      final_backend: el("set-final-backend").value,
      language: el("set-language").value,
    });
    renderSettings(); // o daemon é quem diz o que ficou valendo
    renderConfigSummary();
    const note = el("settings-note");
    note.textContent = "salvo";
    note.classList.add("on");
    setTimeout(() => {
      note.textContent = "";
      note.classList.remove("on");
    }, 2500);
  } catch (err) {
    setAlert("settings-error", "Não foi possível salvar", errText(err));
  } finally {
    button.disabled = false;
  }
}

// -------------------------------------------------------------- comandos

async function onStart() {
  const name = el("name").value.trim() || "Reunião";
  const button = el("start-btn");
  button.disabled = true;
  setAlert("start-error", "");
  try {
    // só o nome: modo, backend e idioma são decididos fora daqui — ver
    // `Api.start_meeting`
    const meeting = await api("start_meeting", name);
    state.autoOpened = meeting.id; // já vamos abrir aqui; o polling não repete
    await openMeeting(meeting.id);
    await refreshStatus();
  } catch (err) {
    setAlert("start-error", "Não foi possível iniciar a reunião", errText(err));
  } finally {
    button.disabled = false;
  }
}

async function stopMeeting(id, button) {
  if (button) button.disabled = true;
  try {
    await api("stop_meeting", id);
    await refreshStatus();
  } catch (err) {
    setBanner(errText(err));
    if (button) button.disabled = false;
  }
}

async function onStop() {
  const o = state.open;
  if (!o) return;
  setAlert("meeting-error", "");
  await stopMeeting(o.id, el("stop-btn"));
}

async function onFinalize() {
  const o = state.open;
  if (!o) return;
  const button = el("finalize-btn");
  button.disabled = true;
  setAlert("meeting-error", "");
  try {
    o.meeting = await api("finalize", o.id);
    renderMeetingHeader();
  } catch (err) {
    setAlert("meeting-error", "Não foi possível finalizar", errText(err));
    button.disabled = false;
  }
}

async function onRename() {
  const o = state.open;
  if (!o) return;
  const input = el("mv-rename");
  const name = input.value.trim();
  if (!name || name === o.meeting.name) {
    input.value = o.meeting.name || o.id;
    return;
  }
  try {
    o.meeting = await api("rename_meeting", o.id, name);
    renderMeetingHeader();
    await loadHistory();
    el("rename-note").textContent = "renomeada";
    setTimeout(() => (el("rename-note").textContent = "clique para renomear"), 2000);
  } catch (err) {
    input.value = o.meeting.name || o.id;
    setBanner(errText(err));
  }
}

async function onOpenFolder() {
  const o = state.open;
  if (!o) return;
  try {
    await api("open_folder", o.id);
  } catch (err) {
    setAlert("meeting-error", "Não foi possível abrir a pasta", errText(err));
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
  if (state.active) el("live-card-time").textContent = elapsedSince(state.active.started_at);
  if (state.open && state.open.live) {
    el("mv-elapsed").textContent = elapsedSince(state.open.meeting.started_at);
    if (state.tab === "transcricao") renderLiveBar();
  }
}

async function boot() {
  el("start-btn").addEventListener("click", onStart);
  el("stop-btn").addEventListener("click", onStop);
  el("finalize-btn").addEventListener("click", onFinalize);
  el("open-folder").addEventListener("click", onOpenFolder);

  el("new-meeting-btn").addEventListener("click", goHome);
  el("live-card-name").addEventListener("click", () => {
    if (state.active) openMeeting(state.active.id);
  });
  el("live-card-stop").addEventListener("click", (event) => {
    if (state.active) stopMeeting(state.active.id, event.currentTarget);
  });
  el("search").addEventListener("input", (event) => {
    state.filter = event.target.value;
    renderMeetingList();
  });

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
  }
  for (const chip of el("source-switch").querySelectorAll(".chip")) {
    chip.addEventListener("click", onPickSource);
  }

  const rename = el("mv-rename");
  rename.addEventListener("blur", onRename);
  rename.addEventListener("keydown", (event) => {
    if (event.key === "Enter") rename.blur();
    if (event.key === "Escape") {
      rename.value = state.open ? state.open.meeting.name || state.open.id : "";
      rename.blur();
    }
  });

  el("notes").addEventListener("input", onNotesInput);
  el("notes").addEventListener("blur", flushNotes);
  for (const tool of document.querySelectorAll(".tool")) {
    tool.addEventListener("click", () => applyMarkdown(tool.dataset.md));
  }

  el("settings-btn").addEventListener("click", openSettings);
  el("mv-settings").addEventListener("click", openSettings);
  el("config-link").addEventListener("click", openSettings);
  el("settings-back").addEventListener("click", goHome);
  el("settings-save").addEventListener("click", onSaveSettings);

  await loadSettings();
  await loadHistory();
  renderHome();
  await refreshStatus();

  setInterval(refreshStatus, POLL_STATUS_MS);
  setInterval(() => {
    if (state.view === "meeting" && state.open && state.open.live) pollChat();
  }, POLL_CHAT_MS);
  setInterval(tick, 1000);
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
