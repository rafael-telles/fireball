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
  paused: "PAUSADA",
  stopping: "PARANDO…",
};

// 'paused' é vivo: o engine continua de pé e o microfone segue tomado por esta
// reunião — ela só parou de capturar.
const LIVE_STATUSES = new Set(["starting", "recording", "paused", "stopping"]);

// Texto curto por status, para a linha de baixo de cada item da lista.
const STATUS_SHORT = {
  starting: "iniciando",
  recording: "gravando",
  paused: "pausada",
  stopping: "parando",
  stopped: "parada",
  finalizing: "finalizando",
  finalized: "finalizada",
  finalize_failed: "falha ao finalizar",
  crashed: "quebrada",
  failed: "falhou",
};

// Sem diarização, "Você" e "Outros participantes" representam as duas tracks.
// Na final diarizada, Sala N continua do lado local e Remoto N do lado oposto.
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
  backends: { realtime: [], final: [], summary: [], calendar: [] },
  prompts: { list: [], default: null, instructions: "" }, // prompts salvos, o padrão, e o texto embutido
  editingPrompt: null, // id em edição, ou "" para um prompt novo; null = editor fechado
  agenda: null, // última resposta de agenda() — status, events, error
  rows: [], // histórico como veio do daemon, mais recentes primeiro
  filter: "", // busca da barra lateral
  tab: "transcricao",
  settingsTab: "transcricao", // aba da tela de configuração, lembrada entre visitas
  notes: { loadedFor: null, saved: null, timer: null },
  playing: null, // seq do segmento destacado agora, pra não repintar a cada tique
  deleteTimer: null, // confirmação de exclusão pendente, que expira sozinha
};

const RATES = [1, 1.25, 1.5, 2];

const VIEWS = { home: "view-home", meeting: "view-meeting", settings: "view-settings" };
const TABS = { resumo: "pane-resumo", transcricao: "pane-transcricao", notas: "pane-notas" };
// As duas telas têm abas, e as da configuração são `.set-tab` de propósito:
// com a mesma classe, o listener de `.tab` trocaria também a aba da reunião
// aberta atrás — pedindo notas e resumo dela sem ninguém ter clicado nisso.
const SETTINGS_TABS = {
  transcricao: "set-pane-transcricao",
  resumo: "set-pane-resumo",
  agenda: "set-pane-agenda",
};

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

// Reunião nasce sem nome — quem nomeia é o provedor de resumo, depois que ela
// acaba. Até lá a tela diz "Sem nome", e não o id: o id é hora + slug genérico,
// que não identifica reunião nenhuma para quem está lendo a lista.
const NO_NAME = "Sem nome";

function nameOf(meeting) {
  return (meeting && meeting.name && String(meeting.name).trim()) || NO_NAME;
}

function speakerColor(name) {
  if (name === "Você") return "var(--accent)";
  if (name === "Outros participantes") return "var(--speaker)";
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return SPEAKER_COLORS[hash % SPEAKER_COLORS.length];
}

function isLocalSpeaker(name) {
  return name === "Você" || name.startsWith("Sala ");
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
  wrap.dataset.seq = seg.seq;
  if (isLocalSpeaker(speaker)) wrap.classList.add("mine");
  if (groupStart) wrap.classList.add("group-start");

  if (groupStart) {
    const name = document.createElement("div");
    name.className = "msg-speaker";
    name.textContent = speaker;
    name.style.setProperty("--speaker-color", speakerColor(speaker));
    wrap.appendChild(name);
  }

  const row = document.createElement("div");
  row.className = "bubble-row";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = seg.text || "";
  row.appendChild(bubble);
  row.appendChild(segmentActions(seg));
  wrap.appendChild(row);

  wrap.appendChild(segmentStamp(seg));
  return wrap;
}

/** A linha de baixo da bolha: hora e, se for o caso, a marca de editado. */
function segmentStamp(seg) {
  const time = document.createElement("div");
  time.className = "msg-time";
  const stamp = segmentTime(seg);
  time.textContent = stamp;
  if (seg.edited) {
    const mark = document.createElement("span");
    mark.className = "msg-edited";
    mark.textContent = stamp ? " · editado" : "editado";
    time.appendChild(mark);
  }
  return time;
}

/** Ações de um segmento, no hover. Só existem numa gravação encerrada: com o
    motor ainda escrevendo no arquivo, corrigir uma linha perderia as falas que
    chegassem no meio da reescrita (o daemon recusa, e a janela não oferece). */
function segmentActions(seg) {
  const box = document.createElement("div");
  box.className = "seg-actions";
  const o = state.open;
  if (!o || o.live) return box;

  if (playerReady() && typeof seg.start === "number") {  // eslint-disable-line
    const listen = document.createElement("button");
    listen.className = "seg-action";
    listen.textContent = "▶ ouvir";
    listen.addEventListener("click", () => playFrom(seg.start));
    box.appendChild(listen);
  }

  const edit = document.createElement("button");
  edit.className = "seg-action";
  edit.textContent = "✎ editar";
  edit.addEventListener("click", (event) => startEdit(event.target.closest(".msg"), seg));
  box.appendChild(edit);

  const remove = document.createElement("button");
  remove.className = "seg-action danger";
  remove.textContent = "✕ excluir";
  remove.addEventListener("click", (event) => confirmDelete(event.target.closest(".msg"), seg));
  box.appendChild(remove);
  return box;
}

/** Excluir uma fala pede confirmação no lugar das próprias ações da bolha.

    A transcrição não tem desfazer, e as ações ficam a um hover de distância
    de qualquer fala — sem o segundo passo, um clique torto apagaria a fala
    errada sem chance de perceber. */
function confirmDelete(wrap, seg) {
  const box = wrap.querySelector(".seg-actions");
  if (!box || box.dataset.confirming) return;
  box.dataset.confirming = "1";
  const original = [...box.children];
  box.innerHTML = "";

  const label = document.createElement("span");
  label.className = "seg-confirm-label";
  label.textContent = "Excluir esta fala?";

  const cancel = document.createElement("button");
  cancel.className = "seg-action";
  cancel.textContent = "Cancelar";
  cancel.addEventListener("click", () => {
    box.innerHTML = "";
    original.forEach((node) => box.appendChild(node));
    delete box.dataset.confirming;
  });

  const confirm = document.createElement("button");
  confirm.className = "seg-action danger solid";
  confirm.textContent = "Excluir";
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      await api("delete_segment", state.open.id, seg.seq);
      // tira da linha do tempo junto, senão o player continuaria destacando
      // uma bolha que não existe mais
      const o = state.open;
      o.timeline = o.timeline.filter((entry) => entry.seq !== seg.seq);
      if (state.playing === seg.seq) state.playing = null;
      wrap.remove();
    } catch (err) {
      confirm.disabled = false;
      setAlert("meeting-error", "Não foi possível excluir a fala", errText(err));
    }
  });

  box.append(label, cancel, confirm);
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
  o.timeline = [];
  state.playing = null;
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
    // só a transcrição final guarda deslocamento em segundos; é ela que dá
    // posição dentro do .wav, e por isso só ela acompanha o player
    if (typeof seg.start === "number") {
      o.timeline.push({ seq: seg.seq, start: seg.start, end: seg.end });
    }
  }
  if (stick) chat.scrollTop = chat.scrollHeight;
}

async function pollChat() {
  const o = state.open;
  if (!o) return;
  let segs;
  try {
    segs = await api("transcript", o.id, o.lastSeq);
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
  live.classList.toggle("hidden", !o.live);
  if (!o.live) {
    renderPlayer();
    return;
  }

  if (o.meeting.transcribe_live === false) {
    el("live-bar-title").textContent = "Só gravando o áudio";
    el("live-bar-detail").textContent = "transcrição ao vivo desligada — a final roda no fim.";
    return;
  }
  el("live-bar").classList.toggle("paused", o.meeting.status === "paused");
  if (o.meeting.status === "paused") {
    el("live-bar-title").textContent = "Pausada";
    el("live-bar-detail").textContent =
      "nada está sendo capturado — o que passar agora não entra na gravação.";
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
  el("mv-name").textContent = nameOf(m);
  el("mv-name").classList.toggle("unnamed", !m.name);
  el("mv-meta").textContent = metaLine(o);

  el("mv-elapsed").classList.toggle("hidden", !o.live);
  if (o.live) el("mv-elapsed").textContent = elapsedSince(m.started_at);
  el("mv-elapsed").classList.toggle("dim", m.status === "paused");

  const stop = el("stop-btn");
  stop.classList.toggle("hidden", !o.live);
  stop.disabled = m.status === "stopping";
  stop.textContent = m.status === "stopping" ? "Parando…" : "Parar";

  const pause = el("pause-btn");
  const pausable = o.live && (m.status === "recording" || m.status === "paused");
  pause.classList.toggle("hidden", !pausable);
  pause.disabled = false;
  pause.textContent = m.status === "paused" ? "Retomar" : "Pausar";

  // Finalizar é a transcrição final sobre o áudio inteiro. Só faz sentido
  // depois que a gravação fechou os .wav — antes disso não há áudio completo.
  const finalize = el("finalize-btn");
  const finished = !o.live && m.status !== "finalizing";
  finalize.classList.toggle("hidden", !finished && m.status !== "finalizing");
  finalize.disabled = m.status === "finalizing";
  finalize.textContent =
    m.status === "finalizing"
      ? "Finalizando…"
      : m.status === "finalized"
        ? "Retranscrever"
        : "Transcrever";
}

function renderFicha() {
  const o = state.open;
  const m = o.meeting;

  const rename = el("mv-rename");
  // vazio, não o id: o campo tem placeholder "Sem nome", e preencher com o id
  // faria a pessoa apagá-lo antes de escrever o nome de verdade
  if (document.activeElement !== rename) rename.value = m.name || "";
  el("rename-note").textContent =
    m.name_source === "ai"
      ? "nome escrito pela IA · clique para trocar"
      : m.name_source === "calendar"
        ? "nome da agenda · clique para trocar"
        : "clique para renomear";

  renderTags(m);
  renderEventFields(m);

  const day = longDate(m.started_at);
  const from = timeLabel(m.started_at);
  const to = m.ended_at ? timeLabel(m.ended_at) : null;
  el("mv-when").textContent = [day, to ? `${from} – ${to}` : `${from} — em andamento`]
    .filter(Boolean)
    .join(" · ");
  el("mv-path").textContent = o.path || "";

  const diarize = el("mv-diarize");
  diarize.checked = Boolean(m.diarize);
  diarize.disabled = o.live || m.status === "finalizing";
  el("mv-diarize-note").textContent = m.diarize
    ? o.live
      ? "Ao vivo: Sala 1–4 e Remoto 1–4. A finalização refina os rótulos."
      : "Na próxima transcrição final: Sala 1–4 e Remoto 1–4."
    : "Desligada: mantém Você e Outros participantes.";
}

/** Local, descrição e convidados do evento de agenda — ou o estado vazio. */
function renderEventFields(meeting) {
  const event = meeting.event || null;
  const location = el("mv-location");
  const loc = event && (event.location || event.conference_url);
  if (loc) {
    location.textContent = event.location || event.conference_url;
    location.classList.remove("muted");
  } else {
    location.textContent = event
      ? "— sem local neste evento"
      : "— o Fireball não registra local de reunião";
    location.classList.add("muted");
  }

  const descBox = el("mv-description");
  descBox.innerHTML = "";
  if (event && event.description) {
    const body = document.createElement("div");
    body.className = "value event-description";
    body.textContent = event.description;
    descBox.appendChild(body);
  } else {
    const empty = document.createElement("div");
    empty.className = "slab short";
    const note = document.createElement("div");
    note.className = "slab-note";
    note.textContent = event
      ? "Este evento de agenda não tinha descrição."
      : "A reunião não guarda descrição — o campo vem do evento de agenda.";
    empty.appendChild(note);
    descBox.appendChild(empty);
  }

  const people = el("mv-people");
  people.innerHTML = "";
  people.appendChild(personRow("V", "mine", "Você", "microfone"));
  people.appendChild(personRow("O", "other", "Outros participantes", "áudio do sistema"));

  const attendees = (event && event.attendees) || [];
  for (const person of attendees) {
    if (person.self) continue; // "Você" já está na lista
    const label = person.name || person.email || "?";
    const initial = (label.trim()[0] || "?").toUpperCase();
    const detail = person.email && person.name ? person.email : person.organizer ? "organizador" : "convidado";
    people.appendChild(personRow(initial, "guest", label, detail));
  }

  const note = el("mv-people-note");
  note.textContent = meeting.diarize
    ? "A diarização separa vozes ao vivo e na transcrição final; convidados da agenda não são identificados automaticamente."
    : attendees.length
      ? "Convidados do evento de agenda. As tracks continuam agrupadas por origem."
      : "Ative a diarização para separar vozes na sala e na chamada.";
}

function personRow(initial, kind, name, detail) {
  const row = document.createElement("div");
  row.className = "person";
  const avatar = document.createElement("span");
  avatar.className = `avatar ${kind}`;
  avatar.textContent = initial;
  const text = document.createElement("span");
  const b = document.createElement("b");
  b.textContent = name;
  const i = document.createElement("i");
  i.textContent = detail;
  text.appendChild(b);
  text.appendChild(i);
  row.appendChild(avatar);
  row.appendChild(text);
  return row;
}

/** As tags da reunião — geradas junto com o resumo, não digitadas aqui. */
function renderTags(meeting) {
  const box = el("mv-tags");
  const tags = meeting.tags || [];
  box.innerHTML = "";
  box.classList.toggle("muted", !tags.length);

  if (!tags.length) {
    box.textContent = "— saem junto com o resumo, na aba ao lado";
    return;
  }
  for (const tag of tags) {
    const chip = document.createElement("span");
    chip.className = "tag";
    chip.textContent = tag;
    box.appendChild(chip);
  }
}

function showTab(name) {
  state.tab = name;
  for (const [key, id] of Object.entries(TABS)) el(id).classList.toggle("hidden", key !== name);
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("selected", tab.dataset.tab === name);
  }
  if (name === "notas") loadNotes();
  if (name === "resumo") {
    // desenha antes de ir buscar: sem isto a caixa fica em branco entre abrir
    // a aba e o resumo chegar, o que se lê como "não tem nada aqui"
    renderSummary();
    loadSummary();
  }
  // sair do editor sem esperar o debounce: trocar de aba é uma pausa
  if (name !== "notas") flushNotes();
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

  state.open = {
    id,
    meeting: detail.meeting,
    segments: detail.segments_total,
    path: detail.path,
    live,
    lastSeq: 0,
    lastSpeaker: null,
    lastSegmentAt: null,
    timeline: [],
    warnings: [],
    audio: null,
    summaryState: null,
    awaitingFinalize: false,
    settling: 0, // ver `stillSettling`
    rate: 1,
  };
  state.notes.loadedFor = null;
  state.playing = null;

  resetDelete(); // confirmação pendente não atravessa para outra reunião
  renderMeetingHeader();
  renderFicha();
  renderWarnings();
  renderMeetingList(); // marca a linha da barra lateral
  resetChat();
  showTab(state.tab === "notas" ? "transcricao" : state.tab);
  // o áudio antes do chat: é ele que decide se cada bolha ganha "▶ ouvir"
  await loadAudio();
  await pollChat();
  el("chat").scrollTop = el("chat").scrollHeight;
  loadWarnings();
}

function goHome() {
  flushNotes();
  state.open = null;
  showView("home");
  renderHome();
  renderMeetingList();
  loadHistory();
}

// Quantas voltas do polling uma reunião recém-encerrada continua sendo
// relida. O daemon sobe o resumo no instante em que o engine sai, mas escrever
// `status: "stopped"` e escrever `summary_status: "running"` são duas escritas,
// e a janela pode ler entre uma e outra — sem essa folga, a leitura que caísse
// no meio deixaria a tela parada em "Sem nome" para sempre. Depois delas, quem
// segura o polling é o próprio `summary_status`.
const SETTLE_TICKS = 5;

function stillSettling(o) {
  if (o.meeting.summary_status === "running") return true;
  // `o.meeting` só é relido por `refreshOpenMeeting`, que é o que esta função
  // libera: pedir o resumo pelo botão não passa por lá, então sem olhar também
  // o que o próprio pedido devolveu ninguém volta a ler a reunião, e a caixa
  // fica em "Gerando…" até alguém trocar de aba.
  if (o.summaryState && o.summaryState.status === "running") return true;
  if (o.settling > 0) {
    o.settling -= 1;
    return true;
  }
  return false;
}

/** Reconcilia a reunião aberta com o que o daemon diz estar ativo. */
async function syncOpenMeeting(activeDetail) {
  const o = state.open;
  if (!o) return;

  if (activeDetail && activeDetail.meeting.id === o.id) {
    o.meeting = activeDetail.meeting;
    o.segments = activeDetail.segments_total;
    o.live = true;
    renderMeetingHeader();
    renderFicha();
    return;
  }

  if (!o.live) {
    // Não está viva, mas o status muda sozinho enquanto o `finalize` roda —
    // sem isso o botão ficaria "Finalizando…" para sempre. O mesmo vale para o
    // resumo, que renomeia a reunião quando termina.
    if (o.meeting.status === "finalizing" || o.awaitingFinalize || stillSettling(o)) {
      await refreshOpenMeeting();
    }
    return;
  }

  // A reunião aberta acabou de sair do ar — pelo botão daqui, pela bandeja ou
  // pela CLI. Drena o que o engine escreveu depois do sinal antes de virar
  // histórico, senão as últimas falas nunca apareceriam.
  o.live = false;
  o.settling = SETTLE_TICKS;
  await pollChat();
  await refreshOpenMeeting();
  // as bolhas foram criadas enquanto a reunião gravava, quando corrigir e
  // ouvir ainda não faziam sentido; agora fazem, então o chat é refeito
  await loadAudio();
  resetChat();
  await pollChat();
  loadWarnings();
}

async function refreshOpenMeeting() {
  const o = state.open;
  if (!o) return;
  const wasSummarizing = o.meeting.summary_status === "running";
  try {
    const detail = await api("meeting_status", o.id);
    if (state.open !== o) return;
    o.meeting = detail.meeting;
    o.segments = detail.segments_total;
    o.path = detail.path;
    o.live = LIVE_STATUSES.has(detail.meeting.status);
    if (o.awaitingFinalize && detail.meeting.status !== "finalizing") {
      // acabou (bem ou mal): a transcrição pode ter sido reescrita por cima, e
      // os seq não são mais os mesmos — o chat incremental recomeça do zero
      o.awaitingFinalize = false;
      // a transcrição nova dispara um resumo novo, que vai renomear a reunião
      o.settling = SETTLE_TICKS;
      await loadAudio(); // o meeting.wav pode ter acabado de aparecer
      resetChat();
      await pollChat();
      loadWarnings();
    }
    // o resumo roda em job próprio; quando ele acaba, a aba precisa saber
    if (o.summaryState && o.summaryState.status === "running") await loadSummary();
    if (wasSummarizing && detail.meeting.summary_status !== "running") {
      // o nome e as tags acabaram de mudar: a caixa do resumo e a linha desta
      // reunião na barra lateral estão desatualizadas as duas
      o.settling = 0;
      await loadSummary();
      loadHistory();
    }
  } catch (err) {
    setBanner(errText(err));
  }
  renderMeetingHeader();
  renderFicha();
  renderLiveBar();
  renderMeetingList();
}

// ----------------------------------------------- barra lateral/histórico

function matchesSearch(row, needle) {
  const hay = [row.name || "", row.id, ...(row.tags || [])].join(" ").toLowerCase();
  return hay.includes(needle);
}

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
  if (!row.name) title.classList.add("unnamed");
  title.textContent = nameOf(row);
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
    // busca também nas tags: quando a IA nomeia, é por elas que se acha um
    // grupo de reuniões ("contratação") sem lembrar do nome de nenhuma
    .filter((r) => !needle || matchesSearch(r, needle))
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
  const paused = active.status === "paused";
  card.classList.toggle("paused", paused);
  el("live-card-label").textContent = STATUS_LABEL[active.status] || String(active.status).toUpperCase();
  el("live-card-time").textContent = elapsedSince(active.started_at);
  el("live-card-name").textContent = nameOf(active);
  el("live-card-name").classList.toggle("unnamed", !active.name);
  el("live-card-stop").disabled = active.status === "stopping";
  el("live-card-stop").textContent = active.status === "stopping" ? "Parando…" : "Parar";

  const pause = el("live-card-pause");
  pause.classList.toggle("hidden", !(active.status === "recording" || paused));
  pause.textContent = paused ? "Retomar" : "Pausar";
}

function renderHome() {
  const active = state.active;
  el("home-kicker").textContent = active
    ? `${active.status === "paused" ? "Pausada" : "Gravando agora"} · ${nameOf(active)}`
    : "Ocioso · nada gravando";
  el("agenda-date").textContent = new Date().toLocaleDateString("pt-BR", {
    weekday: "short",
    day: "numeric",
    month: "long",
  });
  renderAgenda();
}

/** Preenche #agenda-list a partir de state.agenda. */
function renderAgenda() {
  const box = el("agenda-list");
  box.innerHTML = "";
  const agenda = state.agenda;
  const busy = Boolean(state.active);

  if (!agenda || agenda.status === "off") {
    box.appendChild(
      agendaSlab(
        "Sem integração com agenda",
        "Escolha um provedor em Configurações → Agenda para ler o calendário e iniciar a reunião a partir de um evento."
      )
    );
    return;
  }
  if (agenda.status === "loading") {
    box.appendChild(agendaSlab("Carregando agenda…", "Consultando o calendário."));
    return;
  }
  if (agenda.status === "error") {
    box.appendChild(
      agendaSlab("Não foi possível ler a agenda", agenda.error || "Erro desconhecido.")
    );
    return;
  }
  const events = agenda.events || [];
  if (!events.length) {
    box.appendChild(
      agendaSlab("Nada nas próximas horas", "Não há eventos na janela que o Fireball consulta.")
    );
    return;
  }

  const list = document.createElement("div");
  list.className = "agenda-list";
  for (const event of events) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "agenda-row";
    row.disabled = busy;
    row.title = busy
      ? "Já tem uma reunião em andamento"
      : `Iniciar gravação: ${event.title || "evento"}`;

    const when = document.createElement("span");
    when.className = "agenda-when";
    when.textContent = timeLabel(event.start) || "—";

    const mid = document.createElement("span");
    const title = document.createElement("div");
    title.className = "agenda-title";
    title.textContent = event.title || "(sem título)";
    mid.appendChild(title);
    if (event.location) {
      const loc = document.createElement("div");
      loc.className = "agenda-loc";
      loc.textContent = event.location;
      mid.appendChild(loc);
    }

    const go = document.createElement("span");
    go.className = "agenda-go";
    go.textContent = "Iniciar";

    row.appendChild(when);
    row.appendChild(mid);
    row.appendChild(go);
    row.addEventListener("click", () => onStartFromEvent(event.id));
    list.appendChild(row);
  }
  box.appendChild(list);
}

function agendaSlab(title, note) {
  const empty = document.createElement("div");
  empty.className = "slab";
  const t = document.createElement("div");
  t.className = "slab-title";
  t.textContent = title;
  const n = document.createElement("div");
  n.className = "slab-note";
  n.textContent = note;
  empty.appendChild(t);
  empty.appendChild(n);
  return empty;
}

async function refreshAgenda(force) {
  try {
    state.agenda = await api("agenda", Boolean(force));
  } catch (err) {
    state.agenda = {
      provider: "",
      status: "error",
      fetched_at: null,
      error: errText(err),
      events: [],
    };
  }
  if (state.view === "home") renderAgenda();
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

  // Resumo rodando em alguma reunião significa nome e tags prestes a mudar —
  // e é a lista, não a reunião aberta, que mostra os dois para todas elas.
  if (state.rows.some((row) => row.summary_status === "running")) loadHistory();

  const detail = status.active;
  const wasActive = state.active && state.active.id;
  state.active = detail ? detail.meeting : null;
  renderLiveCard();
  renderHome();

  // agenda junto do poll: o daemon cacheia, então isto não shella o gog a cada 2s
  if (state.view === "home") refreshAgenda(false);

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

// ----------------------------------------------------------------- avisos

/** Falhas que não interromperam a gravação — e por isso ninguém viu.

    Gravar só o microfone porque o monitor do sistema não abriu não quebra
    nada na hora: o arquivo sai, a reunião termina normalmente, e a falta só
    aparece quando alguém vai ler a transcrição e metade da conversa não está
    lá. Por isso o aviso é permanente na tela da reunião, e não um toast. */
async function loadWarnings() {
  const o = state.open;
  if (!o) return;
  let warnings;
  try {
    warnings = await api("warnings", o.id);
  } catch (err) {
    setBanner(errText(err));
    return;
  }
  if (state.open !== o) return;
  o.warnings = warnings;
  renderWarnings();
}

const WARNING_TITLE = {
  audio: "A captura de áudio não saiu completa",
  transcricao: "A transcrição ao vivo falhou",
  diarizacao: "A separação de falantes não foi aplicada",
};

function renderWarnings() {
  const box = el("warnings");
  const warnings = (state.open && state.open.warnings) || [];
  box.innerHTML = "";
  box.classList.toggle("hidden", !warnings.length);

  for (const warning of warnings) {
    const card = document.createElement("div");
    card.className = "alert warn";

    const body = document.createElement("div");
    const title = document.createElement("b");
    title.textContent = WARNING_TITLE[warning.kind] || "Aviso";
    body.appendChild(title);
    body.appendChild(document.createTextNode(warning.text));

    const file = document.createElement("span");
    file.className = "alert-file";
    file.textContent = warning.file;
    body.appendChild(file);

    card.appendChild(body);
    box.appendChild(card);
  }
}

// ----------------------------------------------------------------- player

/** Dá pra tocar o áudio e acompanhar a transcrição nele?

    Precisa das duas pontas: o `meeting.wav` (produzido pela transcrição
    final) e uma transcrição com deslocamento em segundos — só a final tem. A
    do tempo real carimba hora de relógio, que não diz posição no arquivo. */
/** Tem áudio gravado pra tocar? (independe de haver posição nos segmentos) */
function playerReady() {
  const o = state.open;
  return !!(o && !o.live && o.audio && o.audio.exists);
}

/** E dá pra acompanhar a transcrição dentro dele?

    São coisas separadas: o áudio toca de qualquer jeito, mas seguir a
    transcrição exige deslocamento em segundos nos segmentos. As duas
    transcrições guardam isso agora — reuniões gravadas antes só têm na final.
    Por isso a pergunta é sobre os dados que chegaram, não sobre qual fonte é. */
function canFollow() {
  const o = state.open;
  return !!(playerReady() && o.timeline.length);
}

async function loadAudio() {
  const o = state.open;
  if (!o) return;
  try {
    o.audio = o.live ? null : await api("audio_info", o.id);
  } catch (err) {
    o.audio = null;
    setBanner(errText(err));
  }
  if (state.open === o) renderPlayer();
}

function renderPlayer() {
  const o = state.open;
  const bar = el("player-bar");
  const none = el("player-none");
  const audio = el("audio");

  if (!o || o.live) {
    bar.classList.add("hidden");
    none.classList.add("hidden");
    el("edit-hint").classList.add("hidden");
    audio.pause();
    return;
  }

  el("edit-hint").classList.remove("hidden");

  if (!playerReady()) {
    bar.classList.add("hidden");
    none.classList.remove("hidden");
    audio.pause();
    // o .wav sai quando a gravação termina (mistura de mic + sistema, ou só
    // mic quando o monitor do sistema não abriu); não havendo nenhum dos
    // dois, não há o que tocar.
    el("player-none-note").textContent =
      "Esta reunião não deixou áudio gravado — não há mic.wav nem meeting.wav na pasta dela.";
    return;
  }

  none.classList.add("hidden");
  bar.classList.remove("hidden");

  // sem posição nos segmentos o áudio toca, mas nada tem o que seguir
  const follow = el("follow-chk");
  follow.disabled = !canFollow();
  follow.parentElement.title = canFollow()
    ? ""
    : "Esta transcrição não guarda posição no áudio, então não há o que acompanhar.";

  // URL http servida pelo daemon, não file:// — a página é servida por http
  // pelo pywebview, e o Chromium recusa mídia file:// vinda dela.
  const src = o.audio.url;
  if (audio.dataset.src !== src) {
    audio.dataset.src = src;
    audio.src = src;
    audio.playbackRate = o.rate || 1;
  }
}

function renderPlayerTime() {
  const audio = el("audio");
  const total = isFinite(audio.duration) ? audio.duration : 0;
  el("player-cur").textContent = clock(audio.currentTime);
  el("player-total").textContent = clock(total);
  el("player-fill").style.width = total ? `${(audio.currentTime / total) * 100}%` : "0";
}

/** O segmento que corresponde ao instante atual do áudio, por busca binária —
    a transcrição de uma reunião longa tem centenas de segmentos e isto roda a
    cada tique do player. */
function segmentAt(timeline, seconds) {
  let lo = 0;
  let hi = timeline.length - 1;
  let found = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (timeline[mid].start <= seconds) {
      found = timeline[mid];
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  // passou do fim do segmento e ainda não começou o próximo: silêncio entre
  // falas, e destacar a anterior mentiria menos que destacar nada
  return found;
}

function onTimeUpdate() {
  const o = state.open;
  if (!o || !playerReady()) return;
  renderPlayerTime();
  if (!canFollow()) return;

  const current = segmentAt(o.timeline, el("audio").currentTime);
  const seq = current ? current.seq : null;
  if (seq === state.playing) return;

  const chat = el("chat");
  const previous = chat.querySelector(".msg.playing");
  if (previous) previous.classList.remove("playing");
  state.playing = seq;
  if (seq === null) return;

  const node = chat.querySelector(`.msg[data-seq="${seq}"]`);
  if (!node) return;
  node.classList.add("playing");
  const follow = el("follow-chk");
  if (follow.checked && !follow.disabled) node.scrollIntoView({ block: "center", behavior: "smooth" });
}

function playFrom(seconds) {
  const audio = el("audio");
  audio.currentTime = seconds;
  audio.play();
}

function onPlayPause() {
  const audio = el("audio");
  if (audio.paused) audio.play();
  else audio.pause();
}

function renderPlayButton() {
  el("play-btn").textContent = el("audio").paused ? "▶" : "❚❚";
}

function onSeek(event) {
  const audio = el("audio");
  if (!isFinite(audio.duration)) return;
  const track = el("player-track");
  const box = track.getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
  audio.currentTime = ratio * audio.duration;
}

function onRate() {
  const o = state.open;
  const audio = el("audio");
  const next = RATES[(RATES.indexOf(audio.playbackRate) + 1) % RATES.length];
  audio.playbackRate = next;
  if (o) o.rate = next;
  el("player-rate").textContent = `${String(next).replace(".", ",")}×`;
}

// -------------------------------------------------- edição de um segmento

/** Troca a bolha por um campo de texto, com salvar/cancelar embaixo.

    Edita **só a transcrição**: o áudio não muda, e o segmento fica marcado
    como editado justamente para que a diferença continue visível depois. */
function startEdit(wrap, seg) {
  if (!wrap || wrap.querySelector(".bubble-edit")) return;
  const o = state.open;
  const row = wrap.querySelector(".bubble-row");
  const bubble = row.querySelector(".bubble");
  const original = bubble.textContent;

  const editor = document.createElement("textarea");
  editor.className = "bubble-edit";
  editor.value = original;
  editor.rows = Math.min(6, Math.ceil(original.length / 60) + 1);
  bubble.replaceWith(editor);

  const stamp = wrap.querySelector(".msg-time");
  const actions = document.createElement("div");
  actions.className = "edit-actions";
  const label = document.createElement("span");
  label.className = "label";
  label.textContent = "editando";
  const cancel = document.createElement("button");
  cancel.className = "btn outline small";
  cancel.textContent = "Cancelar";
  const save = document.createElement("button");
  save.className = "btn primary small";
  save.textContent = "Salvar";
  actions.append(label, cancel, save);
  stamp.classList.add("hidden");
  wrap.appendChild(actions);

  const finish = (seg2) => {
    const fresh = document.createElement("div");
    fresh.className = "bubble";
    fresh.textContent = seg2 ? seg2.text : original;
    editor.replaceWith(fresh);
    actions.remove();
    stamp.classList.remove("hidden");
    if (seg2) stamp.replaceWith(segmentStamp(seg2));
  };

  cancel.addEventListener("click", () => finish(null));
  save.addEventListener("click", async () => {
    const text = editor.value.trim();
    if (!text || text === original) return finish(null);
    save.disabled = true;
    try {
      const updated = await api("edit_segment", o.id, seg.seq, text);
      seg.text = updated.text;
      seg.edited = true;
      finish(updated);
    } catch (err) {
      save.disabled = false;
      setAlert("meeting-error", "Não foi possível salvar a correção", errText(err));
    }
  });

  editor.focus();
  editor.addEventListener("keydown", (event) => {
    if (event.key === "Escape") finish(null);
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) save.click();
  });
}

// ----------------------------------------------------------------- resumo

/** Markdown → DOM, só o que o prompt do resumo pede: títulos, parágrafos,
    listas, citação, `código` e **negrito**.

    Escrito à mão e montando nós, não string de HTML: o texto vem de um modelo
    de linguagem sobre uma transcrição, ou seja, de fora — e concatenar isso em
    innerHTML seria deixar a transcrição escrever marcação na janela. */
function inlineMarkdown(text, parent) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
    const token = match[0];
    const node = document.createElement(token.startsWith("`") ? "code" : "strong");
    node.textContent = token.slice(token.startsWith("`") ? 1 : 2, token.startsWith("`") ? -1 : -2);
    parent.appendChild(node);
    last = match.index + token.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

function renderMarkdown(markdown, box) {
  box.innerHTML = "";
  let list = null;

  const flush = () => {
    list = null;
  };

  for (const raw of markdown.split("\n")) {
    const line = raw.trimEnd();
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    const quote = /^>\s?(.*)$/.exec(line);

    if (!line.trim()) {
      flush();
      continue;
    }
    if (heading) {
      flush();
      const node = document.createElement(`h${Math.min(2, heading[1].length)}`);
      inlineMarkdown(heading[2], node);
      box.appendChild(node);
      continue;
    }
    if (bullet || numbered) {
      const wanted = bullet ? "UL" : "OL";
      if (!list || list.tagName !== wanted) {
        list = document.createElement(bullet ? "ul" : "ol");
        box.appendChild(list);
      }
      const item = document.createElement("li");
      inlineMarkdown((bullet || numbered)[1], item);
      list.appendChild(item);
      continue;
    }
    flush();
    if (quote) {
      const node = document.createElement("blockquote");
      inlineMarkdown(quote[1], node);
      box.appendChild(node);
      continue;
    }
    const para = document.createElement("p");
    inlineMarkdown(line, para);
    box.appendChild(para);
  }
}

async function loadSummary() {
  const o = state.open;
  if (!o) return;
  try {
    o.summaryState = await api("summary", o.id);
  } catch (err) {
    setBanner(errText(err));
    return;
  }
  if (state.open === o) renderSummary();
}

/** O seletor de prompt do cabeçalho, já apontando para o que vale agora. */
function renderSummaryPrompt() {
  const o = state.open;
  const select = el("summary-prompt");
  // o que a reunião usou vale mais que o padrão: quem já resumiu com um prompt
  // espera "Gerar de novo" repetir aquele, não trocar de formato sem avisar
  const chosen = (o && o.meeting.summary_prompt) || state.prompts.default;
  select.innerHTML = "";
  for (const prompt of state.prompts.list) {
    const option = document.createElement("option");
    option.value = prompt.id;
    option.textContent = prompt.name;
    option.selected = prompt.id === chosen;
    select.appendChild(option);
  }
  select.disabled = !state.prompts.list.length;
}

function renderSummary() {
  const o = state.open;
  const box = el("summary-box");
  const button = el("summary-btn");
  const meta = el("summary-meta");
  renderSummaryPrompt();
  const info = (o && o.summaryState) || {};
  const summary = info.summary;
  const loading = !!o && o.summaryState === null;

  box.innerHTML = "";
  box.classList.remove("empty");

  if (loading) {
    box.classList.add("empty");
    const note = document.createElement("div");
    note.className = "slab-note";
    note.textContent = "Carregando…";
    box.appendChild(note);
    button.disabled = true;
    meta.textContent = "";
    return;
  }
  button.disabled = info.status === "running";
  button.textContent = info.status === "running" ? "Gerando…" : summary ? "Regerar" : "Gerar";
  meta.textContent = "";

  if (info.status === "running") {
    box.classList.add("empty");
    const running = document.createElement("div");
    running.className = "summary-running";
    const dot = document.createElement("span");
    dot.className = "pulse-dot";
    running.append(dot, document.createTextNode(`Gerando o resumo com ${info.provider || "o provedor configurado"}…`));
    box.appendChild(running);
    return;
  }

  if (summary) {
    const bits = [summary.provider, summary.prompt_name];
    if (summary.generated_at) bits.push(dateLabel(summary.generated_at));
    meta.textContent = bits.filter(Boolean).join(" · ");
    renderMarkdown(summary.markdown, box);
    return;
  }

  box.classList.add("empty");
  if (info.status === "failed") {
    const card = document.createElement("div");
    card.className = "alert bad";
    const body = document.createElement("div");
    const title = document.createElement("b");
    title.textContent = "Não foi possível gerar o resumo";
    body.appendChild(title);
    body.appendChild(document.createTextNode(info.error || "veja summary.log na pasta da reunião."));
    card.appendChild(body);
    box.appendChild(card);
    return;
  }

  const title = document.createElement("div");
  title.className = "slab-title";
  title.textContent = "Nenhum resumo ainda";
  const note = document.createElement("div");
  note.className = "slab-note";
  note.textContent =
    "O resumo, o nome e as tags saem juntos da transcrição. Regerar substitui o resumo e as tags; " +
    "o nome só é trocado se ninguém tiver dado um à mão.";
  box.append(title, note);
}

async function onSummarize() {
  const o = state.open;
  if (!o) return;
  const button = el("summary-btn");
  button.disabled = true;
  setAlert("meeting-error", "");
  try {
    o.summaryState = await api("summarize", o.id, el("summary-prompt").value);
    renderSummary();
  } catch (err) {
    button.disabled = false;
    setAlert("meeting-error", "Não foi possível pedir o resumo", errText(err));
  }
}

// ---------------------------------------------------------- configuração

/** Preenche um <select>. Cada opção é o valor, ou um par [valor, rótulo] —
 *  backend e provedor se chamam pelo id, prompt tem nome escolhido por gente. */
function fillSelect(id, options, selected) {
  const select = el(id);
  select.innerHTML = "";
  for (const entry of options) {
    const [value, label] = Array.isArray(entry) ? entry : [entry, entry];
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = value === selected;
    select.appendChild(option);
  }
}

/** Linha do formulário de início que lembra com o que a reunião vai rodar. */
function renderConfigSummary() {
  const s = state.settings;
  if (!s) return;
  const live = s.transcribe_live ? `ao vivo: ${s.realtime_backend}` : "sem transcrição ao vivo";
  const diarize = s.diarize_default ? "diarização padrão ligada" : "diarização opcional";
  el("config-summary-text").textContent = `${live} · final: ${s.final_backend} · ${diarize} · ${s.language}`;
  el("start-diarize").checked = Boolean(s.diarize_default);
}

function renderPromptList() {
  const list = el("prompt-list");
  list.innerHTML = "";
  for (const prompt of state.prompts.list) {
    const row = document.createElement("div");
    row.className = "prompt-row" + (prompt.id === state.prompts.default ? " is-default" : "");

    const name = document.createElement("span");
    name.className = "prompt-name";
    name.textContent = prompt.name;
    row.appendChild(name);

    const edit = document.createElement("button");
    edit.className = "btn outline small";
    edit.textContent = "Editar";
    edit.addEventListener("click", () => openPromptEditor(prompt.id));
    row.appendChild(edit);

    const remove = document.createElement("button");
    remove.className = "btn outline small danger";
    remove.textContent = "Apagar";
    // o último não sai: sem nenhum prompt não haveria pedido a fazer
    remove.disabled = state.prompts.list.length < 2;
    remove.addEventListener("click", () => onDeletePrompt(prompt.id));
    row.appendChild(remove);

    list.appendChild(row);
  }
}

function openPromptEditor(promptId) {
  const prompt = state.prompts.list.find((p) => p.id === promptId);
  state.editingPrompt = prompt ? prompt.id : "";
  el("prompt-dialog-title").textContent = prompt ? `Editar "${prompt.name}"` : "Novo prompt";
  el("prompt-name").value = prompt ? prompt.name : "";
  el("prompt-instructions").value = prompt ? prompt.instructions : state.prompts.instructions;
  setAlert("prompt-error", "");
  el("prompt-dialog").showModal();
  el("prompt-name").focus();
}

function closePromptEditor() {
  // `close()` num diálogo fechado é ruído, não erro — mas chamar isto faz
  // parte de sair da tela, e não só de cancelar a edição
  const dialog = el("prompt-dialog");
  if (dialog.open) dialog.close();
}

async function loadPrompts() {
  const data = await api("prompts");
  state.prompts = {
    list: data.prompts || [],
    default: data.default,
    instructions: data.default_instructions || "",
  };
}

async function onSavePrompt() {
  const button = el("prompt-save");
  button.disabled = true;
  setAlert("prompt-error", "");
  try {
    await api("save_prompt", el("prompt-name").value, el("prompt-instructions").value, state.editingPrompt || "");
    await loadPrompts();
    closePromptEditor();
    renderPromptList();
    // o novo entra na lista do padrão e na do cabeçalho da reunião na hora
    fillSelect("set-summary-prompt", promptOptions(), state.settings.summary_prompt);
    if (state.open) renderSummaryPrompt();
  } catch (err) {
    setAlert("prompt-error", "Não foi possível salvar o prompt", errText(err));
  } finally {
    button.disabled = false;
  }
}

async function onDeletePrompt(promptId) {
  setAlert("settings-error", "");
  try {
    await api("delete_prompt", promptId);
    // apagar o padrão faz o daemon eleger outro: a configuração na tela
    // ficou velha, e reler é mais barato que adivinhar qual ele escolheu
    state.settings = await api("get_settings");
    await loadPrompts();
    if (state.editingPrompt === promptId) closePromptEditor();
    renderSettings();
  } catch (err) {
    setAlert("settings-error", "Não foi possível apagar o prompt", errText(err));
  }
}

/** Ids e nomes na forma que o `fillSelect` entende. */
function promptOptions() {
  return state.prompts.list.map((p) => [p.id, p.name]);
}

function calendarProviderOptions() {
  // "" = desligado: instalação limpa não shella nada até a pessoa escolher
  return [["", "Desligado"], ...(state.backends.calendar || []).map((name) => [name, name])];
}

function renderSettings() {
  const s = state.settings;
  fillSelect("set-live-backend", state.backends.realtime, s.realtime_backend);
  fillSelect("set-final-backend", state.backends.final, s.final_backend);
  fillSelect("set-summary-provider", state.backends.summary, s.summary_provider);
  fillSelect("set-summary-prompt", promptOptions(), s.summary_prompt);
  renderPromptList();
  fillSelect("set-calendar-provider", calendarProviderOptions(), s.calendar_provider || "");
  el("set-live-on").checked = s.transcribe_live;
  el("set-diarize-default").checked = s.diarize_default;
  el("set-auto-summarize").checked = s.auto_summarize;
  el("set-openai-url").value = s.openai_base_url || "";
  el("set-openai-key").value = s.openai_api_key || "";
  el("set-openai-model").value = s.openai_model || "";
  el("set-groq-key").value = s.groq_api_key || "";
  el("set-gog-account").value = s.gog_account || "";
  el("set-language").value = s.language;
  renderProviderFields();
}

/** Os campos da API só existem quando o provedor que os usa está escolhido. */
function renderProviderFields() {
  el("openai-fields").classList.toggle("hidden", el("set-summary-provider").value !== "openai_api");
  el("groq-fields").classList.toggle("hidden", el("set-final-backend").value !== "groq");
  el("gog-fields").classList.toggle("hidden", el("set-calendar-provider").value !== "gog");
}

async function loadSettings() {
  state.backends = await window.pywebview.api.backend_options();
  state.settings = await api("get_settings");
  await loadPrompts();
  renderConfigSummary();
}

function showSettingsTab(name) {
  state.settingsTab = name;
  for (const [key, id] of Object.entries(SETTINGS_TABS)) el(id).classList.toggle("hidden", key !== name);
  for (const tab of document.querySelectorAll(".set-tab")) {
    tab.classList.toggle("selected", tab.dataset.setTab === name);
  }
}

async function openSettings() {
  flushNotes();
  setAlert("settings-error", "");
  el("settings-note").textContent = "";
  closePromptEditor(); // edição pendente não atravessa uma saída da tela
  showSettingsTab(state.settingsTab);
  await loadPrompts();
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
      diarize_default: el("set-diarize-default").checked,
      final_backend: el("set-final-backend").value,
      summary_provider: el("set-summary-provider").value,
      summary_prompt: el("set-summary-prompt").value,
      auto_summarize: el("set-auto-summarize").checked,
      openai_base_url: el("set-openai-url").value,
      openai_api_key: el("set-openai-key").value,
      openai_model: el("set-openai-model").value,
      groq_api_key: el("set-groq-key").value,
      calendar_provider: el("set-calendar-provider").value,
      gog_account: el("set-gog-account").value,
      language: el("set-language").value,
    });
    renderSettings(); // o daemon é quem diz o que ficou valendo
    renderConfigSummary();
    // provedor/conta mudaram: a lista da home precisa refletir já
    refreshAgenda(true);
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
  // sem fallback para "Reunião": nome vazio é o pedido de que a IA nomeie
  const name = el("name").value.trim();
  const button = el("start-btn");
  button.disabled = true;
  setAlert("start-error", "");
  try {
    // só o nome (quando há um): modo, backend e idioma são decididos fora
    // daqui — ver `Api.start_meeting`
    const meeting = await api("start_meeting", name, "", el("start-diarize").checked);
    el("name").value = ""; // o nome digitado era desta reunião, não da próxima
    state.autoOpened = meeting.id; // já vamos abrir aqui; o polling não repete
    await openMeeting(meeting.id);
    await refreshStatus();
  } catch (err) {
    setAlert("start-error", "Não foi possível iniciar a reunião", errText(err));
  } finally {
    button.disabled = false;
  }
}

async function onStartFromEvent(eventId) {
  setAlert("start-error", "");
  try {
    const meeting = await api("start_meeting", "", eventId, el("start-diarize").checked);
    state.autoOpened = meeting.id;
    await openMeeting(meeting.id);
    await refreshStatus();
  } catch (err) {
    setAlert("start-error", "Não foi possível iniciar a partir do evento", errText(err));
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

async function togglePause(id, paused, button) {
  if (button) button.disabled = true;
  setAlert("meeting-error", "");
  try {
    await api(paused ? "resume_meeting" : "pause_meeting", id);
    await refreshStatus();
  } catch (err) {
    setBanner(errText(err));
  } finally {
    if (button) button.disabled = false;
  }
}

/** Excluir é em dois toques, no mesmo botão.

    Um diálogo do sistema aqui seria um modal que a janela do pywebview não
    desenha bem, e um "tem certeza?" solto é fácil demais de confirmar no
    automático. Pedindo o segundo clique no mesmo lugar, o botão muda de cor e
    de texto antes de apagar — e desiste sozinho se ninguém confirmar. */
function resetDelete() {
  const button = el("delete-btn");
  button.classList.remove("confirming");
  button.textContent = "Excluir reunião";
  el("delete-note").textContent = "";
  clearTimeout(state.deleteTimer);
  state.deleteTimer = null;
}

async function onDelete() {
  const o = state.open;
  if (!o) return;
  const button = el("delete-btn");

  if (!button.classList.contains("confirming")) {
    button.classList.add("confirming");
    button.textContent = "Confirmar exclusão";
    el("delete-note").textContent = "Apaga o áudio, a transcrição e as notas. Não tem desfazer.";
    state.deleteTimer = setTimeout(resetDelete, 6000);
    return;
  }

  clearTimeout(state.deleteTimer);
  button.disabled = true;
  try {
    await api("delete_meeting", o.id);
    resetDelete();
    button.disabled = false;
    goHome();
  } catch (err) {
    button.disabled = false;
    resetDelete();
    setAlert("meeting-error", "Não foi possível excluir", errText(err));
  }
}

async function onFinalize() {
  const o = state.open;
  if (!o) return;
  const button = el("finalize-btn");
  button.disabled = true;
  setAlert("meeting-error", "");
  try {
    // o retorno é o resumo do job (segmentos, backend), não a reunião — quem
    // diz como ela ficou é o meeting_status logo abaixo
    await api("finalize", o.id, Boolean(o.meeting.diarize));
    // marca que estamos esperando: não dá pra depender de *flagrar* o status
    // 'finalizing' num polling de 2s, porque um backend rápido termina entre
    // duas voltas e a transcrição nova nunca apareceria na tela.
    o.awaitingFinalize = true;
    await refreshOpenMeeting();
  } catch (err) {
    setAlert("meeting-error", "Não foi possível finalizar", errText(err));
    button.disabled = false;
  }
}

async function onDiarizeChange(event) {
  const o = state.open;
  if (!o) return;
  const checkbox = event.currentTarget;
  checkbox.disabled = true;
  try {
    o.meeting = await api("set_diarization", o.id, checkbox.checked);
    renderFicha();
  } catch (err) {
    checkbox.checked = Boolean(o.meeting.diarize);
    setAlert("meeting-error", "Não foi possível mudar a diarização", errText(err));
  } finally {
    checkbox.disabled = o.live || o.meeting.status === "finalizing";
  }
}

async function onRename() {
  const o = state.open;
  if (!o) return;
  const input = el("mv-rename");
  const name = input.value.trim();
  if (!name || name === o.meeting.name) {
    input.value = o.meeting.name || "";
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
  el("mv-diarize").addEventListener("change", onDiarizeChange);
  el("open-folder").addEventListener("click", onOpenFolder);
  el("delete-btn").addEventListener("click", onDelete);
  el("pause-btn").addEventListener("click", (event) => {
    const o = state.open;
    if (o) togglePause(o.id, o.meeting.status === "paused", event.currentTarget);
  });
  el("live-card-pause").addEventListener("click", (event) => {
    const active = state.active;
    if (active) togglePause(active.id, active.status === "paused", event.currentTarget);
  });

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

  const rename = el("mv-rename");
  rename.addEventListener("blur", onRename);
  rename.addEventListener("keydown", (event) => {
    if (event.key === "Enter") rename.blur();
    if (event.key === "Escape") {
      rename.value = state.open ? state.open.meeting.name || "" : "";
      rename.blur();
    }
  });

  el("summary-btn").addEventListener("click", onSummarize);

  const audio = el("audio");
  audio.addEventListener("timeupdate", onTimeUpdate);
  audio.addEventListener("loadedmetadata", renderPlayerTime);
  audio.addEventListener("play", renderPlayButton);
  audio.addEventListener("pause", renderPlayButton);
  audio.addEventListener("ended", renderPlayButton);
  el("play-btn").addEventListener("click", onPlayPause);
  el("player-track").addEventListener("click", onSeek);
  el("player-rate").addEventListener("click", onRate);

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
  // trocar de provedor mostra/esconde os campos dele na hora, sem salvar:
  // preencher URL e chave só faz sentido para quem já escolheu usá-los
  el("set-summary-provider").addEventListener("change", renderProviderFields);
  el("set-calendar-provider").addEventListener("change", renderProviderFields);
  for (const tab of document.querySelectorAll(".set-tab")) {
    tab.addEventListener("click", () => showSettingsTab(tab.dataset.setTab));
  }
  el("prompt-new").addEventListener("click", () => openPromptEditor(null));
  el("prompt-save").addEventListener("click", onSavePrompt);
  el("prompt-cancel").addEventListener("click", closePromptEditor);
  el("prompt-close").addEventListener("click", closePromptEditor);
  el("prompt-restore").addEventListener("click", () => {
    el("prompt-instructions").value = state.prompts.instructions;
  });
  // o Esc fecha sozinho (é `<dialog>`), mas quem larga o estado é isto: sem
  // ele, sair pelo Esc deixaria `editingPrompt` apontando pra edição abandonada
  el("prompt-dialog").addEventListener("close", () => {
    state.editingPrompt = null;
  });
  // clique no fundo escuro fecha. O alvo do clique é o próprio <dialog> só
  // quando ele cai fora do cartão — dentro, o alvo é algum filho.
  el("prompt-dialog").addEventListener("click", (event) => {
    if (event.target === el("prompt-dialog")) closePromptEditor();
  });
  el("set-final-backend").addEventListener("change", renderProviderFields);

  await loadSettings();
  await loadHistory();
  await refreshAgenda(false);
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
