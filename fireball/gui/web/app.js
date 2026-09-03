// Front-end da janela do Fireball.
//
// A barra lateral é a navegação e está sempre na tela: a reunião gravando
// agora fica à vista mesmo enquanto se mexe na configuração — o app é sobre
// uma gravação em curso, e escondê-la atrás de um "voltar" seria esconder a
// única coisa que exige ação.
//
// Quatro telas no painel: início (a próxima reunião da agenda em primeiro
// plano), acervo (tabela e calendário sobre a mesma consulta filtrada),
// reunião (abas Visão Geral · Resumo · Transcrição · Notas) e configuração.
// O chat é *incremental* de propósito —
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
const SEARCH_MS = 250;

const state = {
  view: "home", // "home" | "browse" | "meeting" | "settings"
  active: null, // meeting.json da reunião gravando agora, ou null
  profile: null, // nome/e-mail de quem usa, na medida em que a agenda conta
  open: null, // reunião aberta na tela da reunião (ativa ou passada)
  autoOpened: null, // id já aberto sozinho, pra não reabrir depois de sair
  settings: null, // preferências vindas do daemon (backends, idioma)
  backends: { realtime: [], final: [], summary: [], calendar: [] },
  prompts: { list: [], default: null, instructions: "" }, // prompts salvos, o padrão, e o texto embutido
  voices: [],
  enrollingSpeaker: null,
  editingVoice: null, // id do perfil aberto no diálogo de edição
  editingVoiceEmails: [],
  editingPrompt: null, // id em edição, ou "" para um prompt novo; null = editor fechado
  agenda: null, // última resposta de agenda() — status, events, error
  rows: [], // histórico como veio do daemon, mais recentes primeiro
  filter: "", // texto da caixa de busca
  searchHits: null, // resultado do catálogo, ou null quando a caixa está vazia
  searchTimer: null,
  tab: "visao",
  // Acervo: os filtros são um só conjunto para as duas superfícies, então
  // trocar de tabela para calendário não perde o filtro montado.
  browse: {
    mode: "tabela", // "tabela" | "semana"
    query: "",
    rangeDays: "", // "" = qualquer data
    tags: [],
    people: [],
    sort: "started_at",
    desc: true,
    weekStart: null, // segunda-feira da semana desenhada, em ISO curto
    facets: null,
    result: null,
    timer: null,
  },
  settingsTab: "transcricao", // aba da tela de configuração, lembrada entre visitas
  notes: { loadedFor: null, saved: null, timer: null },
  playing: null, // seq do segmento destacado agora, pra não repintar a cada tique
  deleteTimer: null, // confirmação de exclusão pendente, que expira sozinha
};

const RATES = [1, 1.25, 1.5, 2];

const VIEWS = {
  home: "view-home",
  browse: "view-browse",
  meeting: "view-meeting",
  settings: "view-settings",
};
const TABS = {
  visao: "pane-visao",
  resumo: "pane-resumo",
  transcricao: "pane-transcricao",
  notas: "pane-notas",
};
// As duas telas têm abas, e as da configuração são `.set-tab` de propósito:
// com a mesma classe, o listener de `.tab` trocaria também a aba da reunião
// aberta atrás — pedindo notas e resumo dela sem ninguém ter clicado nisso.
const SETTINGS_TABS = {
  transcricao: "set-pane-transcricao",
  resumo: "set-pane-resumo",
  agenda: "set-pane-agenda",
  vozes: "set-pane-vozes",
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

/** Duração longa, para somas do acervo: "9h12", "48min", "—". */
function spanLabel(seconds) {
  const secs = Math.max(0, Math.round(seconds || 0));
  if (!secs) return "—";
  const h = Math.floor(secs / 3600);
  const m = Math.round((secs % 3600) / 60);
  return h ? `${h}h${String(m).padStart(2, "0")}` : `${m}min`;
}

/** Iniciais para o avatar: duas quando há nome e sobrenome, uma quando não. */
function initialsOf(label) {
  const parts = String(label || "?")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 1).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

// Cor do avatar por hash do nome, como a dos falantes e pelo mesmo motivo:
// nenhum tom avermelhado aqui, senão um avatar competiria com o acento que
// significa "gravando".
const AVATAR_COLORS = ["#3d5a80", "#5a4a80", "#4a6b52", "#7a5230", "#3d6b7a", "#6b4a5f"];

function avatarColor(label) {
  let hash = 0;
  for (const ch of String(label || "")) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

/** Segunda-feira da semana daquele dia, como Date à meia-noite local. */
function weekStartOf(date) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const shift = (d.getDay() + 6) % 7; // domingo=0 vira 6: a semana começa na segunda
  d.setDate(d.getDate() - shift);
  return d;
}

/** Data como "2026-08-24", que é o formato que o catálogo compara. */
function isoDay(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** "28 ago" — dia e mês curtos, sem o "de" que o pt-BR insere e que só gasta
    largura numa coluna de tabela. */
function shortDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const month = d.toLocaleDateString("pt-BR", { month: "short" }).replace(".", "");
  return `${d.getDate()} ${month}`;
}

function greetingFor(hour) {
  if (hour < 12) return "Bom dia";
  if (hour < 18) return "Boa tarde";
  return "Boa noite";
}

/** "Começa em 12 minutos", "Começou às 15:00", "Em 2 horas". */
function startsInLabel(iso) {
  const start = new Date(iso);
  if (isNaN(start)) return "";
  const minutes = Math.round((start - Date.now()) / 60000);
  if (minutes < -1) return `Começou às ${timeLabel(iso)}`;
  if (minutes <= 1) return "Começa agora";
  if (minutes < 60) return `Começa em ${minutes} minutos`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `Começa em ${hours} ${hours === 1 ? "hora" : "horas"}` : `Começa ${dayLabel(iso)}`;
}

/** Duração prevista de um evento da agenda: "1h prevista", "30 min". */
function plannedLabel(event) {
  if (!event.start || !event.end) return "";
  const mins = Math.round((new Date(event.end) - new Date(event.start)) / 60000);
  if (!(mins > 0)) return "";
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? `${h}h${String(rest).padStart(2, "0")}` : `${h}h`;
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

function isLocalSpeaker(name, seg = null) {
  if (seg && (seg.track === "mic" || String(seg.speaker_key || "").startsWith("mic:"))) return true;
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
  if (isLocalSpeaker(speaker, seg)) wrap.classList.add("mine");
  if (groupStart) wrap.classList.add("group-start");

  if (groupStart) {
    // Vale ao vivo: o daemon guarda só o perfil e o rótulo enquanto o motor
    // escreve a transcrição. Finalizando, não — o arquivo está sendo trocado.
    const canEnroll = seg.speaker_key && state.open?.meeting.status !== "finalizing";
    const name = document.createElement(canEnroll ? "button" : "div");
    name.className = "msg-speaker";
    name.textContent = speaker;
    name.style.setProperty("--speaker-color", speakerColor(speaker));
    if (canEnroll) {
      name.classList.add("voice-link");
      name.title = "Confirmar quem é esta voz";
      name.setAttribute("aria-label", `Confirmar identidade de ${speaker}`);
      name.addEventListener("click", () => openVoiceDialog(seg));
    }
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
  o.lastEnd = null;
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
    const gap = silenceBefore(o, seg);
    if (gap) chat.appendChild(silenceMark(gap));
    chat.appendChild(chatMessage(seg, o.lastSpeaker));
    o.lastSpeaker = seg.speaker || "?";
    o.lastSeq = seg.seq;
    if (typeof seg.end === "number") o.lastEnd = seg.end;
    if (seg.ts) o.lastSegmentAt = new Date(seg.ts).getTime();
    // só a transcrição final guarda deslocamento em segundos; é ela que dá
    // posição dentro do .wav, e por isso só ela acompanha o player
    if (typeof seg.start === "number") {
      o.timeline.push({ seq: seg.seq, start: seg.start, end: seg.end });
    }
  }
  if (stick) chat.scrollTop = chat.scrollHeight;
}

// Quanto tempo parado justifica uma marca no meio da conversa. O mesmo limite
// que `fireball/stats.py` usa para contar "pausas longas": abaixo disso é
// respiro entre frases, e uma marca a cada respiro só picaria a leitura.
const SILENCE_MARK_S = 20;

/** Os segundos de silêncio antes desta fala, quando vale marcá-los.

    Só na transcrição final: é a única que tem `start`/`end`. A do tempo real
    carimba quando o trecho *chegou*, e o atraso do motor apareceria como
    silêncio que nunca houve. */
function silenceBefore(o, seg) {
  if (typeof seg.start !== "number" || typeof o.lastEnd !== "number") return 0;
  const gap = seg.start - o.lastEnd;
  return gap >= SILENCE_MARK_S ? gap : 0;
}

function silenceMark(seconds) {
  const mark = document.createElement("div");
  mark.className = "silence-mark";
  const left = document.createElement("i");
  const right = document.createElement("i");
  const label = document.createElement("span");
  label.textContent = `${clock(seconds)} de silêncio`;
  mark.append(left, label, right);
  return mark;
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

  renderTranscriptBar();
}

/** A barra da aba Transcrição: qual texto é este, e o que fazer com ele.

    Retranscrever mora aqui e não no cabeçalho da reunião de propósito — é uma
    ação sobre *este* texto, e o que justifica clicá-la é justamente a linha ao
    lado, que diz com que motor ele foi feito. */
function renderTranscriptBar() {
  const o = state.open;
  if (!o) return;
  const m = o.meeting;
  const meta = o.transcriptMeta;
  // a passada final desligada muda as duas coisas desta barra: o que a linha
  // diz e se o botão existe
  const enabled = !state.settings || state.settings.transcribe_final !== false;

  const bits = [];
  if (o.live) {
    bits.push(
      m.transcribe_live === false
        ? "Só gravando — transcrição ao vivo desligada"
        : "Transcrevendo ao vivo"
    );
    if (m.backend) bits.push(m.backend);
  } else if (!meta) {
    bits.push("Carregando…");
  } else if (meta.kind === "final") {
    bits.push("Transcrição final");
    if (meta.backend) bits.push(meta.backend);
    if (meta.generated_at) bits.push(`gerada em ${dateLabel(meta.generated_at)}`);
    bits.push(`${meta.segments} ${meta.segments === 1 ? "segmento" : "segmentos"}`);
    if (meta.diarized) bits.push("com falantes separados");
  } else if (meta.kind === "live") {
    bits.push("Transcrição do tempo real");
    if (meta.backend) bits.push(meta.backend);
    bits.push(`${meta.segments} ${meta.segments === 1 ? "segmento" : "segmentos"}`);
  } else {
    bits.push("Sem transcrição");
  }
  // sem isto, quem desligou a passada final não entende por que o botão sumiu
  if (!enabled && !o.live) bits.push("transcrição final desligada");
  el("tr-meta").textContent = bits.join(" · ");

  // Retranscrever é a transcrição final sobre o áudio inteiro. Só faz sentido
  // depois que a gravação fechou os .wav — antes disso não há áudio completo.
  // E não aparece com a passada final desligada: oferecer o botão que o daemon
  // vai recusar é pior que não oferecer nada.
  const finalize = el("finalize-btn");
  const finalizing = m.status === "finalizing";
  finalize.classList.toggle("hidden", (o.live && !finalizing) || (!enabled && !finalizing));
  finalize.disabled = finalizing;
  finalize.textContent = finalizing
    ? "Retranscrevendo…"
    : meta && meta.kind === "final"
      ? "Retranscrever"
      : "Transcrever";

  el("tr-export").disabled = !meta || !meta.segments;
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
  renderPeople();
  renderDescription(m);

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

/** A descrição do evento de agenda. Sem evento, o grupo inteiro sai da tela:
    um campo vazio explicando por que está vazio ocupa o lugar dos números. */
function renderDescription(meeting) {
  const event = meeting.event || null;
  const group = el("mv-description-group");
  group.classList.toggle("hidden", !(event && event.description));
  const box = el("mv-description");
  box.innerHTML = "";
  if (event && event.description) {
    const body = document.createElement("div");
    body.className = "value event-description";
    body.textContent = event.description;
    box.appendChild(body);
  }
}

const ANON_SPEAKER = /^(Você|Outros participantes|(Sala|Remoto) \d+)$/;
const ANON_SLOT = /^(Sala|Remoto) \d+$/;

/** As chaves por que duas entradas são a mesma pessoa.

    O **e-mail** primeiro: é o único identificador que o perfil de voz e o
    convidado da agenda de fato compartilham. O nome normalizado entra como
    reserva, para quem não tem e-mail em lugar nenhum — mas só ele não bastava,
    e era por isso que uma voz reconhecida e o convidado dela apareciam duas
    vezes na ficha sempre que os dois nomes não batiam letra por letra. */
function identityKeys(name, emails) {
  const keys = new Set();
  for (const email of emails || []) {
    const clean = String(email).trim().toLowerCase();
    if (clean) keys.add(`email:${clean}`);
  }
  const label = String(name || "").trim().toLowerCase();
  if (label) keys.add(`name:${label}`);
  return keys;
}

/** O que o Fireball sabe sobre um falante: o perfil de voz, quando há um. */
function speakerIdentity(speaker) {
  const voice = state.voices.find((item) => item.id === speaker.voice_profile_id);
  if (voice) return { name: voice.name, emails: voice.emails || [] };
  // sem perfil, o nome na transcrição é o que temos — e o e-mail dele pode
  // estar no convite, que é o outro lado desta mesma junção
  const email = emailFor(state.open.meeting, speaker.speaker);
  return { name: speaker.speaker, emails: email ? [email] : [] };
}

/** Junta num participante só as entradas que são a mesma pessoa.

    Três fontes falam da mesma gente e nenhuma sabe das outras: os falantes da
    transcrição (com nome de perfil de voz, ou anônimos), os convidados do
    evento, e a track do microfone — que se chama "Você" e não tem nome nenhum
    até alguém dizer, na configuração, qual das vozes cadastradas é a sua.

    Slot anônimo de diarização (`Sala 2`, `Remoto 1`) nunca se junta a nada: ele
    é precisamente a pessoa que o Fireball **não** identificou, e adivinhar
    aqui seria atribuir fala a quem não falou. */
function participantGroups() {
  const o = state.open;
  const m = o.meeting;
  const me = state.profile || {};
  const timed = Boolean(o.stats && o.stats.timed);
  const groups = [];

  const place = (keys, solo) => {
    const found = solo
      ? null
      : groups.find((group) => !group.solo && [...keys].some((key) => group.keys.has(key)));
    if (found) {
      for (const key of keys) found.keys.add(key);
      return found;
    }
    const group = {
      keys: new Set(keys),
      solo: Boolean(solo),
      isMe: false,
      seconds: 0,
      ratio: 0,
      tracks: new Set(),
      names: [],
      emails: [],
      slot: null,
      speaker: null,
      attendee: null,
      spoke: false,
    };
    groups.push(group);
    return group;
  };

  for (const speaker of (o.stats && o.stats.speakers) || []) {
    const name = speaker.speaker;
    const anon = ANON_SPEAKER.test(name);
    const isMine = name === "Você";
    // "Você" só se junta a alguém pela voz marcada como minha na configuração:
    // é a única coisa que liga a track do microfone a um nome e a um e-mail
    const identity = isMine
      ? { name: me.name || "", emails: me.emails || [] }
      : anon
        ? { name, emails: [] }
        : speakerIdentity(speaker);

    const group = place(identityKeys(identity.name, identity.emails), ANON_SLOT.test(name));
    group.seconds += speaker.seconds || 0;
    group.ratio += speaker.ratio || 0;
    group.spoke = group.spoke || timed;
    if (isMine) group.isMe = true;
    if (anon) group.tracks.add(name);
    else if (identity.name) group.names.push(identity.name);
    if (ANON_SLOT.test(name)) group.slot = name;
    for (const email of identity.emails) {
      if (!group.emails.includes(email)) group.emails.push(email);
    }
    group.speaker = group.speaker || (speaker.speaker_key ? speaker : null);
  }

  for (const person of (m.event && m.event.attendees) || []) {
    const label = person.name || person.email || "";
    if (!label) continue;
    const emails = person.email ? [person.email] : [];
    // o convidado marcado como `self` é você: entra com a sua identidade para
    // cair no mesmo grupo do microfone
    const keys = person.self
      ? identityKeys(me.name || label, [...(me.emails || []), ...emails])
      : identityKeys(person.name, emails);

    const group = place(keys, false);
    group.attendee = group.attendee || person;
    if (person.self) group.isMe = true;
    if (person.name && !group.names.includes(person.name)) group.names.push(person.name);
    for (const email of emails) {
      const clean = email.toLowerCase();
      if (!group.emails.includes(clean)) group.emails.push(clean);
    }
  }

  // Só existe um "eu": a track do microfone, a voz marcada como minha e o
  // convidado `self` da agenda são a mesma pessoa por definição, mesmo quando
  // nenhuma chave em comum os ligou (é o caso de quem não configurou "minha
  // voz" e cujo perfil não tem o e-mail do convite).
  const mine = groups.filter((group) => group.isMe && !group.solo);
  if (mine.length > 1) {
    const first = mine[0];
    for (const other of mine.slice(1)) {
      for (const key of other.keys) first.keys.add(key);
      first.seconds += other.seconds;
      first.ratio += other.ratio;
      first.spoke = first.spoke || other.spoke;
      for (const track of other.tracks) first.tracks.add(track);
      for (const name of other.names) if (!first.names.includes(name)) first.names.push(name);
      for (const email of other.emails) if (!first.emails.includes(email)) first.emails.push(email);
      first.speaker = first.speaker || other.speaker;
      first.attendee = first.attendee || other.attendee;
      groups.splice(groups.indexOf(other), 1);
    }
  }

  // "Você" sem voz configurada, sem convite e sem conta continua um grupo só
  // dele; nada a fazer além de não fingir que ele tem nome.
  for (const group of groups) group.label = groupLabel(group);
  return groups;
}

/** Como o grupo se chama na tela.

    "Você" ganha o nome de gente quando ele existe (é mais útil numa lista de
    participantes que um pronome), e o pronome vira o detalhe da linha. */
function groupLabel(group) {
  if (group.names.length) return group.names[0];
  if (group.isMe) return "Você";
  if (group.slot) return group.slot;
  for (const track of group.tracks) return track;
  return group.emails[0] || "?";
}

/** Quem falou nesta reunião — do que a transcrição mostra, não do convite.

    A lista sai dos falantes detectados, e não dos convidados do evento: é ela
    que responde "quem estava mesmo aqui". Os convidados que não apareceram na
    transcrição entram no fim, marcados como tal, porque a ausência também é
    informação. */
function renderPeople() {
  const o = state.open;
  const m = o.meeting;
  const box = el("mv-people");
  box.innerHTML = "";

  const groups = participantGroups();
  // quem falou primeiro, convidado que não apareceu depois
  const ordered = [...groups].sort((a, b) => (b.seconds || 0) - (a.seconds || 0));

  for (const group of ordered) {
    const detail = [];
    // "você" já diz qual track é a sua; as duas juntas seriam a mesma coisa
    // dita duas vezes numa linha que já é longa
    if (group.isMe && group.label !== "Você") detail.push("você");
    else if (group.tracks.has("Você")) detail.push("microfone");
    if (group.tracks.has("Outros participantes")) detail.push("áudio do sistema");
    if (group.slot) detail.push("sem nome atribuído");
    if (group.emails.length) detail.push(group.emails[0]);
    else if (!group.tracks.size && !group.slot) detail.push("convidado do evento");
    if (group.seconds) detail.push(`${clock(group.seconds)} de fala`);
    else if (group.tracks.size || group.slot) detail.push("sem fala medida");
    else detail.push("não apareceu na transcrição");

    box.appendChild(personRow(group, detail.join(" · ")));
  }

  if (!box.childElementCount) {
    const note = document.createElement("div");
    note.className = "note";
    note.textContent = m.diarize
      ? "Ninguém detectado ainda — a diarização separa as vozes na transcrição final."
      : "Ninguém detectado ainda. Ative a diarização para separar vozes na sala e na chamada.";
    box.appendChild(note);
  }

  const detected = groups.filter((group) => group.tracks.size || group.slot || group.seconds).length;
  const unnamed = groups.filter((group) => group.slot).length;
  el("mv-people-count").textContent = detected
    ? `${detected} ${detected === 1 ? "detectada" : "detectadas"}`
    : "";
  el("mv-voices-note").textContent = unnamed
    ? `${unnamed} sem nome`
    : state.voices.length
      ? `${state.voices.length} cadastrada${state.voices.length === 1 ? "" : "s"}`
      : "";
}

/** O e-mail que o evento de agenda conhece para este nome. */
function emailFor(meeting, name) {
  const attendees = (meeting.event && meeting.event.attendees) || [];
  const found = attendees.find(
    (person) => (person.name || "").toLowerCase() === String(name).toLowerCase()
  );
  return (found && found.email) || "";
}

/** A cor do avatar e da barra de participação de um grupo, seguindo o chat:
    acento para quem fala pelo microfone, azul para a track remota, cinza para
    o slot que ninguém identificou. */
function groupKind(group) {
  if (group.isMe || group.tracks.has("Você")) return "mine";
  if (group.tracks.has("Outros participantes")) return "other";
  if (group.slot) return "slot";
  return group.seconds ? "known" : "guest";
}

function groupColor(group) {
  const kind = groupKind(group);
  if (kind === "mine") return "var(--accent)";
  if (kind === "other") return "var(--speaker)";
  if (kind === "slot") return "var(--faint)";
  return speakerColor(group.label);
}

function personRow(group, detail) {
  // Slot de diarização sem nome é clicável: é daqui que se cadastra a voz, e
  // exigir voltar à transcrição para achar a bolha certa era o caminho longo.
  const speaker = group.speaker;
  const enrollable = Boolean(
    speaker && speaker.speaker_key && state.open.meeting.status !== "finalizing"
  );
  const row = document.createElement(enrollable ? "button" : "div");
  row.className = "person";
  if (enrollable) {
    row.type = "button";
    row.classList.add("voice-link");
    row.title = "Confirmar quem é esta voz";
    row.addEventListener("click", () =>
      openVoiceDialog({
        speaker: speaker.speaker,
        speaker_key: speaker.speaker_key,
        voice_profile_id: speaker.voice_profile_id,
      })
    );
  }

  const kind = groupKind(group);
  const avatar = document.createElement("span");
  avatar.className = `avatar ${kind}`;
  avatar.textContent = group.slot ? group.slot.split(" ")[1] : initialsOf(group.label).slice(0, 1);
  if (kind === "known" || kind === "guest") avatar.style.background = avatarColor(group.label);

  const text = document.createElement("span");
  const b = document.createElement("b");
  b.textContent = group.label;
  const i = document.createElement("i");
  i.textContent = detail;
  text.append(b, i);
  row.append(avatar, text);
  return row;
}

/** As tags da reunião: saem do resumo e se corrigem aqui.

    Quem estava na reunião classifica melhor que o modelo, e a tag é o que faz
    a tabela do acervo encontrar um grupo de reuniões depois. */
function renderTags(meeting) {
  const box = el("mv-tags");
  const tags = meeting.tags || [];
  box.innerHTML = "";

  for (const tag of tags) {
    const chip = document.createElement("span");
    chip.className = "tag removable";
    chip.textContent = tag;
    const x = document.createElement("button");
    x.className = "chip-x";
    x.type = "button";
    x.textContent = "×";
    x.title = `Remover a tag ${tag}`;
    x.addEventListener("click", () => saveTags(tags.filter((other) => other !== tag)));
    chip.appendChild(x);
    box.appendChild(chip);
  }

  const add = document.createElement("button");
  add.className = "tag add";
  add.type = "button";
  add.textContent = "+ tag";
  add.addEventListener("click", () => startTagInput(box, add, tags));
  box.appendChild(add);
}

/** O "+ tag" virando campo no lugar: um diálogo para escrever uma palavra
    custaria mais atenção que a palavra vale. */
function startTagInput(box, addButton, tags) {
  const input = document.createElement("input");
  input.className = "tag-input";
  input.placeholder = "nova tag";
  input.maxLength = 40;
  box.replaceChild(input, addButton);
  input.focus();

  const commit = () => {
    const value = input.value.trim();
    if (!value || tags.includes(value)) {
      renderTags(state.open.meeting);
      return;
    }
    saveTags([...tags, value]);
  };
  input.addEventListener("blur", commit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    }
    if (event.key === "Escape") {
      input.value = "";
      input.blur();
    }
  });
}

async function saveTags(tags) {
  const o = state.open;
  if (!o) return;
  try {
    o.meeting = await api("set_tags", o.id, tags);
  } catch (err) {
    setAlert("meeting-error", "Não foi possível salvar as tags", errText(err));
    return;
  }
  renderTags(o.meeting);
  loadHistory();
  if (state.view === "browse") loadFacets();
}

// ------------------------------------------------------------ estatísticas

async function loadStats() {
  const o = state.open;
  if (!o) return;
  try {
    o.stats = await api("meeting_stats", o.id);
  } catch (err) {
    setBanner(errText(err));
    return;
  }
  if (state.open !== o) return;
  renderStats();
  renderPeople(); // o tempo de fala de cada um sai daqui
}

function statTile(value, label) {
  const tile = document.createElement("div");
  tile.className = "stat";
  const big = document.createElement("div");
  big.className = "stat-value";
  big.textContent = value;
  const small = document.createElement("div");
  small.className = "stat-label";
  small.textContent = label;
  tile.append(big, small);
  return tile;
}

function statsBlock(title, right) {
  const block = document.createElement("div");
  block.className = "stat-block";
  const head = document.createElement("div");
  head.className = "sec-head tight";
  const label = document.createElement("span");
  label.className = "lbl";
  label.textContent = title;
  head.appendChild(label);
  if (right) {
    const note = document.createElement("span");
    note.className = "sec-right";
    note.textContent = right;
    head.appendChild(note);
  }
  block.appendChild(head);
  return block;
}

function renderStats() {
  const o = state.open;
  const box = el("mv-stats");
  box.innerHTML = "";
  const stats = o && o.stats;

  if (!stats) {
    const note = document.createElement("div");
    note.className = "slab-note";
    note.textContent = "Carregando…";
    box.appendChild(note);
    return;
  }

  const pct = (ratio) => `${Math.round((ratio || 0) * 100)}%`;

  const numbers = statsBlock("Estatísticas da reunião");
  const grid = document.createElement("div");
  grid.className = "stat-grid";
  grid.appendChild(statTile(clock(stats.recorded_seconds), "duração gravada"));
  if (stats.timed) {
    grid.appendChild(
      statTile(clock(stats.silence_seconds), `tempo de silêncio · ${pct(stats.silence_ratio)}`)
    );
    grid.appendChild(statTile(clock(stats.speech_seconds), "tempo de fala"));
    grid.appendChild(
      statTile(
        stats.longest_silence ? clock(stats.longest_silence.seconds) : "—",
        "maior trecho de silêncio"
      )
    );
    grid.appendChild(statTile(String(stats.turn_changes), "trocas de turno"));
  } else {
    grid.appendChild(statTile(String(stats.segments), "falas transcritas"));
    grid.appendChild(statTile(String(stats.words), "palavras"));
  }
  grid.appendChild(
    statTile(stats.words_per_minute ? String(stats.words_per_minute) : "—", "palavras por minuto")
  );
  numbers.appendChild(grid);
  box.appendChild(numbers);

  if (!stats.timed) {
    const slab = document.createElement("div");
    slab.className = "slab";
    const title = document.createElement("div");
    title.className = "slab-title";
    title.textContent = "Silêncio e participação pedem a transcrição final";
    const note = document.createElement("div");
    note.className = "slab-note";
    note.textContent =
      "A transcrição do tempo real carimba a hora em que cada trecho chegou, não quanto ele durou. " +
      "Rode Retranscrever na aba Transcrição para medir fala, silêncio e participação.";
    slab.append(title, note);
    box.appendChild(slab);
    return;
  }

  // Participação por pessoa, com o silêncio na mesma escala: sem ele as
  // barras somariam 100% e a reunião pareceria cheia de fala.
  //
  // Pelos mesmos grupos da lista de participantes, e não pelos falantes
  // crus: quem aparece como "Você" no microfone e outra vez pelo nome que a
  // diarização reconheceu é uma pessoa só, e duas barras de 30% e 15% no
  // lugar de uma de 45% não é layout — é conta errada.
  const share = statsBlock("Participação por pessoa", "por tempo de fala");
  const card = document.createElement("div");
  card.className = "stat-card bars-card";
  const spoke = participantGroups()
    .filter((group) => group.seconds > 0)
    .sort((a, b) => b.seconds - a.seconds);
  for (const group of spoke) {
    card.appendChild(
      shareRow(group.label, group.ratio, `${clock(group.seconds)} · ${pct(group.ratio)}`, groupColor(group))
    );
  }
  card.appendChild(
    shareRow(
      "Silêncio",
      stats.silence_ratio,
      `${clock(stats.silence_seconds)} · ${pct(stats.silence_ratio)}`,
      "var(--faint)"
    )
  );
  share.appendChild(card);
  box.appendChild(share);

  const pauses = stats.long_pauses || [];
  const timeline = statsBlock(
    "Silêncio ao longo da reunião",
    pauses.length
      ? `${pauses.length} ${pauses.length === 1 ? "pausa" : "pausas"} acima de 20 s`
      : "nenhuma pausa longa"
  );
  timeline.appendChild(silenceChart(stats));
  box.appendChild(timeline);
}

function shareRow(label, ratio, value, color) {
  const row = document.createElement("div");
  row.className = "share-row";
  const name = document.createElement("span");
  name.className = "share-name";
  name.textContent = label;
  name.title = label;
  const track = document.createElement("div");
  track.className = "share-track";
  const fill = document.createElement("div");
  fill.className = "share-fill";
  fill.style.width = `${Math.min(100, Math.max(0, (ratio || 0) * 100))}%`;
  fill.style.background = color;
  track.appendChild(fill);
  const amount = document.createElement("span");
  amount.className = "share-value";
  amount.textContent = value;
  row.append(name, track, amount);
  return row;
}

/** A reunião fatiada no tempo: barra alta é gente falando, barra cinza e rasa
    é a reunião parada. As pausas longas saem escritas embaixo — a barra diz
    onde, o texto diz quanto. */
function silenceChart(stats) {
  const card = document.createElement("div");
  card.className = "stat-card";

  const chart = document.createElement("div");
  chart.className = "spark";
  for (const bucket of stats.timeline) {
    const bar = document.createElement("i");
    const quiet = bucket.ratio < 0.2;
    if (quiet) bar.classList.add("quiet");
    bar.style.height = `${Math.max(4, Math.round(bucket.ratio * 100))}%`;
    bar.title = `${clock(bucket.start)} – ${clock(bucket.end)} · ${Math.round(bucket.ratio * 100)}% de fala`;
    chart.appendChild(bar);
  }
  card.appendChild(chart);

  const axis = document.createElement("div");
  axis.className = "spark-axis";
  const started = stats.recorded_seconds;
  const meeting = state.open.meeting;
  const at = (offset) => {
    const base = new Date(meeting.started_at);
    return isNaN(base) ? clock(offset) : timeLabel(new Date(base.getTime() + offset * 1000));
  };
  for (const offset of [0, started / 2, started]) {
    const mark = document.createElement("span");
    mark.textContent = at(offset);
    axis.appendChild(mark);
  }
  card.appendChild(axis);

  const pauses = (stats.long_pauses || []).slice(0, 3);
  if (pauses.length) {
    const note = document.createElement("div");
    note.className = "spark-note";
    note.textContent =
      "As barras em cinza são as pausas longas: " +
      pauses.map((gap) => `${at(gap.start)} (${clock(gap.seconds)})`).join(", ") +
      ".";
    card.appendChild(note);
  }
  return card;
}

function showTab(name) {
  state.tab = name;
  for (const [key, id] of Object.entries(TABS)) el(id).classList.toggle("hidden", key !== name);
  for (const tab of document.querySelectorAll(".tab[data-tab]")) {
    tab.classList.toggle("selected", tab.dataset.tab === name);
  }
  if (name === "notas") loadNotes();
  // desenha antes de ir buscar: sem isto a caixa fica em branco entre abrir a
  // aba e o dado chegar, o que se lê como "não tem nada aqui"
  if (name === "visao") {
    renderStats();
    loadStats();
  }
  if (name === "resumo") {
    renderSummary();
    loadSummary();
  }
  // sair do editor sem esperar o debounce: trocar de aba é uma pausa
  if (name !== "notas") flushNotes();
}

function revealSeq(seq) {
  const node = el("chat").querySelector(`.msg[data-seq="${seq}"]`);
  if (!node) return false;
  node.classList.add("search-hit");
  node.scrollIntoView({ block: "center" });
  return true;
}

async function openMeeting(id, row, jumpSeq) {
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
    stats: null,
    transcriptMeta: null,
    summaryState: null,
    summarySel: null, // qual resumo da lista está aberto à direita
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
  if (state.view === "browse") renderTable();
  resetChat();
  showTab(jumpSeq != null ? "transcricao" : state.tab === "notas" ? "visao" : state.tab);
  // o áudio antes do chat: é ele que decide se cada bolha ganha "▶ ouvir"
  await loadAudio();
  await loadTranscriptMeta();
  await pollChat();
  if (jumpSeq == null || !revealSeq(jumpSeq)) {
    el("chat").scrollTop = el("chat").scrollHeight;
  }
  loadWarnings();
}

function goHome() {
  flushNotes();
  state.open = null;
  showView("home");
  // o formulário avulso volta a se esconder atrás do link: ele é a saída para
  // o que não está na agenda, e deixá-lo aberto faria a home ser um formulário
  el("start-card").classList.add("hidden");
  renderHome();
  renderMeetingList();
  loadHistory();
  loadFacets();
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
  await loadTranscriptMeta();
  loadStats();
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
      await loadTranscriptMeta();
      resetChat();
      await pollChat();
      loadWarnings();
      // silêncio, fala e participação são medidos sobre a transcrição — a
      // que mudou é justamente a que eles mediam
      loadStats();
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
  const jumpSeq = row.hits && row.hits[0] ? row.hits[0].seq : undefined;
  button.addEventListener("click", () => openMeeting(row.id, row, jumpSeq));

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

  if (row.snippet) {
    const hit = document.createElement("div");
    hit.className = "sb-row-hit";
    hit.textContent = row.snippet;
    button.appendChild(hit);
  }
  return button;
}

async function runSearch(needle) {
  if (state.filter.trim() !== needle) return;
  try {
    const result = await api("search_meetings", needle);
    if (state.filter.trim() !== needle) return;
    state.searchHits = result;
  } catch (err) {
    setBanner(errText(err));
    return;
  }
  renderMeetingList();
}

function onSearchInput(event) {
  state.filter = event.target.value;
  if (state.searchTimer) clearTimeout(state.searchTimer);
  const needle = state.filter.trim();
  if (!needle) {
    state.searchHits = null;
    renderMeetingList();
    return;
  }
  state.searchTimer = setTimeout(() => runSearch(needle), SEARCH_MS);
  renderMeetingList();
}

function renderMeetingList() {
  const list = el("meeting-list");
  list.innerHTML = "";

  const needle = state.filter.trim();
  let rows;
  if (!needle) {
    rows = [...state.rows].sort(
      (a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0)
    );
  } else if (state.searchHits && state.searchHits.query === needle) {
    rows = state.searchHits.meetings;
  } else {
    const empty = document.createElement("div");
    empty.className = "sb-empty";
    empty.textContent = "Buscando…";
    list.appendChild(empty);
    return;
  }

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "sb-empty";
    empty.textContent = needle
      ? "Nenhuma reunião encontrada."
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

  const first = ((state.profile && state.profile.name) || "").trim().split(/\s+/)[0] || "";
  const hello = greetingFor(new Date().getHours());
  el("home-greeting").textContent = first ? `${hello}, ${first}` : hello;

  renderAgenda();
}

/** Em que seção da home este evento cai: hoje, amanhã, ou o dia dele. */
function agendaGroup(iso) {
  const days = -daysAgo(iso);
  if (days <= 0) return "Ainda hoje";
  if (days === 1) return "Amanhã";
  return new Date(iso).toLocaleDateString("pt-BR", { weekday: "long", day: "numeric", month: "long" });
}

/** Só os eventos que ainda vão acontecer (com uma folga: quem está atrasado
    para a reunião das 15:00 às 15:05 ainda quer o botão de gravar ela). */
const AGENDA_GRACE_MIN = 10;

function upcomingEvents() {
  const events = (state.agenda && state.agenda.events) || [];
  const floor = Date.now() - AGENDA_GRACE_MIN * 60000;
  return events
    .filter((event) => {
      const end = new Date(event.end || event.start);
      return !isNaN(end) && end.getTime() >= floor;
    })
    .sort((a, b) => new Date(a.start) - new Date(b.start));
}

/** Preenche o cartão da próxima e as linhas do resto. */
function renderAgenda() {
  const card = el("next-card");
  const rest = el("agenda-rest");
  card.innerHTML = "";
  rest.innerHTML = "";

  const agenda = state.agenda;
  const today = new Date().toLocaleDateString("pt-BR", {
    weekday: "long",
    day: "numeric",
    month: "long",
  });

  if (!agenda || agenda.status === "off") {
    el("home-today").textContent = today;
    card.appendChild(
      agendaSlab(
        "Sem integração com agenda",
        "Escolha um provedor em Configurações → Agenda para ver a próxima reunião aqui e gravá-la com nome, participantes e descrição já preenchidos."
      )
    );
    revealAdHoc(true);
    return;
  }
  if (agenda.status === "loading") {
    el("home-today").textContent = today;
    card.appendChild(agendaSlab("Carregando agenda…", "Consultando o calendário."));
    return;
  }
  if (agenda.status === "error") {
    el("home-today").textContent = today;
    card.appendChild(agendaSlab("Não foi possível ler a agenda", agenda.error || "Erro desconhecido."));
    revealAdHoc(true);
    return;
  }

  const events = upcomingEvents();
  el("home-today").textContent = events.length
    ? `${today} · ${events.length} ${events.length === 1 ? "reunião" : "reuniões"} pela frente`
    : today;

  if (!events.length) {
    card.appendChild(
      agendaSlab("Nada nas próximas horas", "Não há eventos na janela que o Fireball consulta.")
    );
    revealAdHoc(true);
    return;
  }

  card.appendChild(nextCard(events[0]));

  // Agrupa o resto por dia: "Ainda hoje" e "Amanhã" são as duas seções que
  // aparecem quase sempre, e o dia por extenso cobre o que passa disso.
  let current = null;
  let section = null;
  let bucket = [];
  const flush = () => {
    if (!section) return;
    const meta = section.querySelector(".sec-right");
    const total = bucket.reduce((sum, event) => {
      const mins = (new Date(event.end) - new Date(event.start)) / 60000;
      return sum + (mins > 0 ? mins : 0);
    }, 0);
    const count = `${bucket.length} ${bucket.length === 1 ? "evento" : "eventos"}`;
    meta.textContent = total ? `${count} · ${spanLabel(total * 60)} prevista` : count;
  };

  for (const event of events.slice(1)) {
    const group = agendaGroup(event.start);
    if (group !== current) {
      flush();
      current = group;
      bucket = [];
      section = document.createElement("section");
      section.className = "agenda-section";
      const head = document.createElement("div");
      head.className = "sec-head";
      const title = document.createElement("span");
      title.className = "lbl";
      title.textContent = group;
      const meta = document.createElement("span");
      meta.className = "sec-right";
      head.append(title, meta);
      const list = document.createElement("div");
      list.className = "agenda-list";
      section.append(head, list);
      rest.appendChild(section);
    }
    bucket.push(event);
    section.querySelector(".agenda-list").appendChild(agendaRow(event, current !== "Ainda hoje"));
  }
  flush();
  revealAdHoc(false);
}

/** O cartão grande da próxima reunião — o único caminho em primeiro plano. */
function nextCard(event) {
  const busy = Boolean(state.active);
  const card = document.createElement("section");
  card.className = "next-card";

  const top = document.createElement("div");
  top.className = "next-top";
  const dot = document.createElement("span");
  dot.className = "pulse-dot";
  const when = document.createElement("span");
  when.className = "next-when";
  when.textContent = startsInLabel(event.start);
  const range = document.createElement("span");
  range.className = "next-range";
  const planned = plannedLabel(event);
  range.textContent = [
    event.end ? `${timeLabel(event.start)} – ${timeLabel(event.end)}` : timeLabel(event.start),
    planned && `${planned} prevista`,
  ]
    .filter(Boolean)
    .join(" · ");
  top.append(dot, when, range);
  card.appendChild(top);

  const title = document.createElement("h2");
  title.className = "next-title";
  title.textContent = event.title || "(sem título)";
  card.appendChild(title);

  if (event.description) {
    const body = document.createElement("p");
    body.className = "next-desc";
    body.textContent = event.description;
    card.appendChild(body);
  }

  const chips = eventChips(event);
  if (chips) card.appendChild(chips);

  // sem você na lista: quem está lendo sabe que vai à própria reunião, e o
  // que a contagem precisa dizer é quantos *outros* estarão lá
  const attendees = (event.attendees || []).filter(
    (person) => !person.self && (person.name || person.email)
  );
  if (attendees.length) {
    const block = document.createElement("div");
    block.className = "next-people";
    const head = document.createElement("div");
    head.className = "lbl";
    head.textContent = `${attendees.length} ${attendees.length === 1 ? "participante" : "participantes"}`;
    const list = document.createElement("div");
    list.className = "person-chips";
    for (const person of attendees) list.appendChild(personChip(person));
    block.append(head, list);
    card.appendChild(block);
  }

  const actions = document.createElement("div");
  actions.className = "next-actions";
  const record = document.createElement("button");
  record.className = "btn primary big";
  record.textContent = "Gravar esta reunião";
  record.disabled = busy;
  record.title = busy ? "Já tem uma reunião em andamento" : "";
  record.addEventListener("click", () => onStartFromEvent(event.id));
  actions.appendChild(record);

  if (event.conference_url) {
    const join = document.createElement("button");
    join.className = "btn outline";
    join.textContent = `Entrar no ${conferenceName(event.conference_url)}`;
    join.addEventListener("click", () => onOpenUrl(event.conference_url));
    actions.appendChild(join);
  }

  const note = document.createElement("span");
  note.className = "next-note";
  note.textContent = "Nome, participantes e descrição vêm do evento da agenda.";
  actions.appendChild(note);
  card.appendChild(actions);
  return card;
}

/** Nome do serviço a partir do host do link — o botão diz onde se entra. */
function conferenceName(url) {
  const host = (String(url).split("/")[2] || "").toLowerCase();
  if (host.includes("meet.google")) return "Meet";
  if (host.includes("zoom")) return "Zoom";
  if (host.includes("teams.microsoft")) return "Teams";
  if (host.includes("whereby")) return "Whereby";
  return "link";
}

/** Onde a chamada acontece, se ela repete, e o que mais o evento carrega. */
function eventChips(event) {
  const row = document.createElement("div");
  row.className = "event-chips";
  let any = false;

  if (event.conference_url) {
    const code = String(event.conference_url).split("/").filter(Boolean).pop() || "";
    row.appendChild(chip("🔗", `${conferenceName(event.conference_url)}${code ? ` · ${code}` : ""}`));
    any = true;
  }
  if (event.location) {
    row.appendChild(chip("📍", event.location));
    any = true;
  }
  const repeat = recurrenceLabel(event);
  if (repeat) {
    row.appendChild(chip("↻", repeat));
    any = true;
  }
  return any ? row : null;
}

function chip(icon, text) {
  const span = document.createElement("span");
  span.className = "event-chip";
  const ico = document.createElement("i");
  ico.textContent = icon;
  span.append(ico, document.createTextNode(text));
  return span;
}

const RRULE_DAYS = {
  MO: "segunda",
  TU: "terça",
  WE: "quarta",
  TH: "quinta",
  FR: "sexta",
  SA: "sábado",
  SU: "domingo",
};

/** "recorrente · toda sexta", quando a regra diz isso — só "recorrente" quando
    ela é mais complicada que uma frase curta. */
function recurrenceLabel(event) {
  if (!event.recurring && !(event.recurrence || []).length) return "";
  const rule = (event.recurrence || []).find((line) => String(line).includes("FREQ=")) || "";
  const freq = /FREQ=(\w+)/.exec(rule);
  const byDay = /BYDAY=([A-Z,]+)/.exec(rule);
  if (freq && freq[1] === "WEEKLY" && byDay) {
    const days = byDay[1]
      .split(",")
      .map((code) => RRULE_DAYS[code.slice(-2)])
      .filter(Boolean);
    if (days.length === 1) return `recorrente · toda ${days[0]}`;
    if (days.length) return `recorrente · ${days.join(", ")}`;
  }
  if (freq) {
    const named = { DAILY: "diário", WEEKLY: "semanal", MONTHLY: "mensal", YEARLY: "anual" }[freq[1]];
    if (named) return `recorrente · ${named}`;
  }
  return "recorrente";
}

/** Uma pessoa do evento: avatar, nome, e o "talvez" de quem não confirmou. */
function personChip(person) {
  const label = person.name || person.email || "?";
  const pending = person.response === "tentative" || person.response === "needsAction";
  const declined = person.response === "declined";

  const span = document.createElement("span");
  span.className = "person-chip";
  if (pending || declined) span.classList.add("unsure");

  const avatar = document.createElement("span");
  avatar.className = "avatar";
  if (pending || declined) {
    avatar.textContent = declined ? "✕" : "?";
  } else {
    avatar.textContent = initialsOf(label);
    avatar.style.background = avatarColor(label);
  }

  const name = document.createElement("span");
  name.textContent = declined ? `${label} · recusou` : pending ? `${label} · talvez` : label;
  span.append(avatar, name);
  if (person.email && person.email !== label) span.title = person.email;
  return span;
}

/** Linha compacta do resto do dia. `dim` deixa o dia seguinte mais apagado:
    ele é informação, não o que se vai gravar agora. */
function agendaRow(event, dim) {
  const busy = Boolean(state.active);
  const row = document.createElement("div");
  row.className = "agenda-row";
  if (dim) row.classList.add("dim");

  const when = document.createElement("div");
  when.className = "agenda-when";
  const hour = document.createElement("div");
  hour.className = "agenda-hour";
  hour.textContent = timeLabel(event.start) || "—";
  const planned = document.createElement("div");
  planned.className = "agenda-planned";
  planned.textContent = plannedLabel(event);
  when.append(hour, planned);

  const mid = document.createElement("div");
  mid.className = "grow";
  const title = document.createElement("div");
  title.className = "agenda-title";
  title.textContent = event.title || "(sem título)";
  mid.appendChild(title);

  const guests = (event.attendees || []).filter((person) => !person.self);
  const bits = [];
  if (event.location) bits.push(event.location);
  else if (event.conference_url) bits.push(conferenceName(event.conference_url));
  const repeat = recurrenceLabel(event);
  if (repeat) bits.push(repeat);
  const people = guests.length;
  if (people) bits.push(`${people} ${people === 1 ? "participante" : "participantes"}`);
  if (bits.length) {
    const sub = document.createElement("div");
    sub.className = "agenda-loc";
    sub.textContent = bits.join(" · ");
    mid.appendChild(sub);
  }

  const faces = document.createElement("div");
  faces.className = "faces";
  for (const person of guests.slice(0, 2)) {
    faces.appendChild(faceOf(person.name || person.email || "?"));
  }
  const extra = guests.length - 2;
  if (extra > 0) faces.appendChild(faceOf(`+${extra}`, true));

  const go = document.createElement("button");
  go.className = "btn outline small";
  go.textContent = "Gravar";
  go.disabled = busy;
  go.title = busy ? "Já tem uma reunião em andamento" : `Iniciar gravação: ${event.title || "evento"}`;
  go.addEventListener("click", () => onStartFromEvent(event.id));

  row.append(when, mid, faces, go);
  return row;
}

/** Um avatar da pilha sobreposta. `plain` é o "+7", que não é uma pessoa. */
function faceOf(label, plain) {
  const face = document.createElement("span");
  face.className = "face";
  if (plain) {
    face.classList.add("more");
    face.textContent = label;
    return face;
  }
  face.textContent = initialsOf(label);
  face.style.background = avatarColor(label);
  return face;
}

/** O formulário avulso fica escondido atrás de um link: ele é a saída para o
    que não está na agenda, não o caminho principal. Quando não há agenda
    nenhuma, ele aparece sozinho — aí ele *é* o caminho principal. */
function revealAdHoc(force) {
  const card = el("start-card");
  if (force) card.classList.remove("hidden");
  // o convite e o formulário nunca aparecem juntos: um é a porta do outro, e
  // "nada na agenda que sirva?" sem o link ao lado é uma pergunta sem resposta
  el("adhoc-link").parentElement.classList.toggle(
    "hidden",
    !card.classList.contains("hidden")
  );
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

async function loadProfile() {
  try {
    state.profile = await api("profile");
  } catch (err) {
    // sem perfil a home cumprimenta sem nome; não vale um banner por isso
    state.profile = null;
  }
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
  // é da agenda que sai o nome do cumprimento (o convidado marcado como
  // `self`), então o perfil só existe depois de haver evento no cache
  if (state.profile && !state.profile.name) await loadProfile();
  if (state.view === "home") renderHome();
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
    if (wasActive) {
      loadHistory();
      // a reunião que acabou ganhou duração e vai ganhar nome e tags — os três
      // são colunas da tabela do acervo e a soma do cabeçalho
      if (state.view === "browse") {
        loadFacets();
        runBrowse();
      }
    }
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

// ------------------------------------------------------------------ acervo

// A tabela e o calendário saem da *mesma* consulta filtrada. O que muda é a
// superfície: a tabela acha uma reunião específica (é ordenável, densa), o
// calendário mostra como a semana foi gasta (blocos na posição e no tamanho
// reais da gravação). Por isso os filtros moram no state, e não em cada uma:
// trocar de superfície não pode perder o filtro montado.

const BROWSE_MS = 250;

// Altura de uma hora no calendário. Fixa: é ela que faz o bloco ter o tamanho
// real da gravação, e um bloco elástico deixaria de dizer quanto tempo durou.
const HOUR_PX = 40;
const WEEK_DAYS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"];

async function openBrowse() {
  await flushNotes();
  showView("browse");
  if (!state.browse.weekStart) state.browse.weekStart = isoDay(weekStartOf(new Date()));
  renderBrowse();
  loadFacets();
  runBrowse();
}

async function loadFacets() {
  try {
    state.browse.facets = await api("catalog_facets");
  } catch (err) {
    setBanner(errText(err));
    return;
  }
  renderBrowseCount();
  renderFilters();
}

/** O intervalo que a consulta pede: no calendário é a semana desenhada, na
    tabela é o que o seletor de data escolheu. */
function browseRange() {
  const b = state.browse;
  if (b.mode === "semana") {
    const start = new Date(`${b.weekStart}T00:00:00`);
    const end = new Date(start);
    end.setDate(end.getDate() + 7);
    return { since: isoDay(start), until: isoDay(end) };
  }
  if (!b.rangeDays) return { since: null, until: null };
  const since = new Date();
  since.setDate(since.getDate() - Number(b.rangeDays));
  return { since: isoDay(since), until: null };
}

async function runBrowse() {
  const b = state.browse;
  const range = browseRange();
  const filters = {
    query: b.query.trim(),
    since: range.since,
    until: range.until,
    tags: b.tags,
    people: b.people,
    sort: b.sort,
    desc: b.desc,
  };
  try {
    b.result = await api("browse_meetings", filters);
  } catch (err) {
    setBanner(errText(err));
    return;
  }
  if (state.view === "browse") renderBrowse();
}

function scheduleBrowse() {
  const b = state.browse;
  if (b.timer) clearTimeout(b.timer);
  b.timer = setTimeout(runBrowse, BROWSE_MS);
}

function renderBrowse() {
  const b = state.browse;
  const week = b.mode === "semana";

  el("bv-title").textContent = week ? weekTitle(b.weekStart) : "Todas as reuniões";
  el("bv-week-nav").classList.toggle("hidden", !week);
  el("bv-legend").classList.toggle("hidden", !week);
  el("bv-table-wrap").classList.toggle("hidden", week);
  el("bv-week").classList.toggle("hidden", !week);
  el("bv-count").classList.toggle("hidden", week);
  for (const button of el("bv-modes").querySelectorAll(".seg-btn")) {
    button.classList.toggle("selected", button.dataset.mode === b.mode);
  }
  // no calendário a semana *é* o filtro de data; deixar o seletor de intervalo
  // aceso ali seria oferecer dois controles para a mesma coisa
  el("bv-range").closest(".filter-pick").classList.toggle("hidden", week);

  el("bv-foot-note").textContent = week
    ? "Blocos na posição e no tamanho reais da gravação · clique para abrir"
    : "Clique numa linha para abrir · Ctrl+K vai para a busca";

  renderBrowseCount();
  renderFilters();
  renderTableHead();
  if (week) renderWeek();
  else renderTable();
}

function weekTitle(iso) {
  const start = new Date(`${iso}T00:00:00`);
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  const sameMonth = start.getMonth() === end.getMonth();
  const day = (d) => d.getDate();
  const month = (d) => d.toLocaleDateString("pt-BR", { month: "long" });
  return sameMonth
    ? `${day(start)} – ${day(end)} de ${month(start)}`
    : `${day(start)} de ${month(start)} – ${day(end)} de ${month(end)}`;
}

/** As duas linhas de números: o tamanho do acervo no cabeçalho, o do
    resultado filtrado logo acima da tabela. */
function renderBrowseCount() {
  const b = state.browse;
  const facets = b.facets;
  el("bv-sub").textContent = facets
    ? `${facets.meetings} ${facets.meetings === 1 ? "gravada" : "gravadas"} · ${spanLabel(facets.duration_s)} de áudio`
    : "";
  el("browse-count").textContent = facets ? String(facets.meetings) : "";

  const result = b.result;
  if (!result) {
    el("bv-count").textContent = "Carregando…";
    return;
  }
  const totals = result.totals;
  const label = SORT_LABEL[b.sort] || b.sort;
  el("bv-count").textContent =
    `${totals.meetings} ${totals.meetings === 1 ? "reunião" : "reuniões"} · ` +
    `${spanLabel(totals.duration_s)} · ordenado por ${label} ${b.desc ? "↓" : "↑"}`;

  if (b.mode === "semana") {
    el("bv-sub").textContent =
      `${totals.meetings} ${totals.meetings === 1 ? "reunião" : "reuniões"} · ${spanLabel(totals.duration_s)}`;
  }
}

const SORT_LABEL = { name: "nome", started_at: "data", duration: "duração" };

/** Chips do que está filtrando + os seletores que acrescentam mais.

    O chip é a única forma de *saber* que um filtro está ligado: um select que
    voltou ao valor escolhido some no meio da barra, e ninguém entende por que
    a tabela está curta. */
function renderFilters() {
  const b = state.browse;
  const facets = b.facets || { tags: [], people: [] };

  const tagBox = el("bv-tag-chips");
  tagBox.innerHTML = "";
  for (const tag of b.tags) tagBox.appendChild(filterChip(tag, "tag", () => toggleTag(tag)));

  const peopleBox = el("bv-people-chips");
  peopleBox.innerHTML = "";
  for (const person of b.people) {
    peopleBox.appendChild(filterChip(person, "person", () => togglePerson(person)));
  }

  fillFilterSelect("bv-tag-add", "+ tags", facets.tags.map((row) => [row.tag, `${row.tag} · ${row.meetings}`]), b.tags);
  fillFilterSelect(
    "bv-people-add",
    "+ participantes",
    facets.people.map((row) => [row.label, `${row.label} · ${row.meetings}`]),
    b.people
  );

  const dirty = Boolean(b.query.trim() || b.rangeDays || b.tags.length || b.people.length);
  el("bv-clear").classList.toggle("hidden", !dirty);
  if (el("bv-q").value !== b.query) el("bv-q").value = b.query;
  el("bv-range").value = b.rangeDays;
}

function fillFilterSelect(id, placeholder, options, chosen) {
  const select = el(id);
  select.innerHTML = "";
  const head = document.createElement("option");
  head.value = "";
  head.textContent = placeholder;
  select.appendChild(head);
  for (const [value, label] of options) {
    if (chosen.includes(value)) continue;
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  select.value = "";
  select.disabled = select.options.length <= 1;
}

function filterChip(label, kind, onRemove) {
  const chip = document.createElement("button");
  chip.className = `filter-chip ${kind}`;
  chip.type = "button";
  chip.title = "Remover este filtro";
  if (kind === "person") {
    const face = faceOf(label);
    chip.appendChild(face);
  } else {
    const swatch = document.createElement("i");
    swatch.className = "swatch";
    chip.appendChild(swatch);
  }
  chip.appendChild(document.createTextNode(label));
  const x = document.createElement("span");
  x.className = "chip-x";
  x.textContent = "×";
  chip.appendChild(x);
  chip.addEventListener("click", onRemove);
  return chip;
}

function toggleTag(tag) {
  const list = state.browse.tags;
  const at = list.indexOf(tag);
  if (at >= 0) list.splice(at, 1);
  else list.push(tag);
  renderFilters();
  runBrowse();
}

function togglePerson(person) {
  const list = state.browse.people;
  const at = list.indexOf(person);
  if (at >= 0) list.splice(at, 1);
  else list.push(person);
  renderFilters();
  runBrowse();
}

function clearBrowseFilters() {
  const b = state.browse;
  b.query = "";
  b.rangeDays = "";
  b.tags = [];
  b.people = [];
  renderFilters();
  runBrowse();
}

function setBrowseMode(mode) {
  state.browse.mode = mode;
  renderBrowse();
  runBrowse();
}

function shiftWeek(weeks) {
  const start = new Date(`${state.browse.weekStart}T00:00:00`);
  start.setDate(start.getDate() + weeks * 7);
  state.browse.weekStart = isoDay(start);
  renderBrowse();
  runBrowse();
}

function onSortColumn(column) {
  const b = state.browse;
  if (b.sort === column) b.desc = !b.desc;
  else {
    b.sort = column;
    // nome sobe (A→Z é o que se espera de uma lista de nomes); data e duração
    // descem (o mais recente e o mais longo primeiro)
    b.desc = column !== "name";
  }
  renderBrowse();
  runBrowse();
}

function renderTableHead() {
  const b = state.browse;
  for (const col of el("bv-table-head").querySelectorAll(".mt-col[data-sort]")) {
    const active = col.dataset.sort === b.sort;
    col.classList.toggle("active", active);
    col.dataset.arrow = active ? (b.desc ? "↓" : "↑") : "";
  }
}

function renderTable() {
  const box = el("bv-rows");
  box.innerHTML = "";
  const rows = (state.browse.result && state.browse.result.meetings) || [];

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "mt-empty";
    empty.textContent = state.browse.result
      ? "Nenhuma reunião passa por estes filtros."
      : "Carregando…";
    box.appendChild(empty);
    return;
  }

  for (const row of rows) {
    const line = document.createElement("button");
    line.className = "mt-row";
    line.type = "button";
    if (state.open && state.open.id === row.id) line.classList.add("selected");
    line.addEventListener("click", () => openMeeting(row.id, row));

    const name = document.createElement("div");
    name.className = "mt-name";
    const title = document.createElement("div");
    title.className = "mt-title";
    if (!row.name) title.classList.add("unnamed");
    title.textContent = nameOf(row);
    name.appendChild(title);
    const sub = document.createElement("div");
    sub.className = "mt-sub";
    sub.textContent = row.subtitle || STATUS_SHORT[row.status] || "";
    name.appendChild(sub);

    const date = document.createElement("div");
    date.className = "mt-date";
    const days = daysAgo(row.started_at);
    const top = document.createElement("div");
    top.textContent = days === 0 ? "Hoje" : days === 1 ? "Ontem" : shortDate(row.started_at);
    const hour = document.createElement("div");
    hour.className = "mt-dim";
    hour.textContent = timeLabel(row.started_at);
    date.append(top, hour);

    const dur = document.createElement("div");
    dur.className = "mt-dur";
    dur.textContent = row.duration_s ? clock(row.duration_s) : LIVE_STATUSES.has(row.status) ? "gravando" : "—";

    const faces = document.createElement("div");
    faces.className = "faces";
    for (const person of (row.people || []).slice(0, 2)) faces.appendChild(faceOf(person.label));
    const extra = (row.people || []).length - 2;
    if (extra > 0) faces.appendChild(faceOf(`+${extra}`, true));

    const tags = document.createElement("div");
    tags.className = "mt-tags";
    for (const tag of row.tags || []) {
      const chip = document.createElement("span");
      chip.className = "tag";
      if (state.browse.tags.includes(tag)) chip.classList.add("on");
      chip.textContent = tag;
      tags.appendChild(chip);
    }

    line.append(name, date, dur, faces, tags);
    box.appendChild(line);
  }
}

/** A semana em horas. A faixa de horas não é fixa em 08–18: ela abre o
    suficiente para caber a reunião das 7h e a das 21h, senão o bloco ficaria
    fora da grade — e um bloco fora da grade é a mesma coisa que não existir. */
function renderWeek() {
  const grid = el("bv-week");
  grid.innerHTML = "";
  const rows = (state.browse.result && state.browse.result.meetings) || [];
  const start = new Date(`${state.browse.weekStart}T00:00:00`);

  let first = 8;
  let last = 18;
  for (const row of rows) {
    const begin = new Date(row.started_at);
    if (isNaN(begin)) continue;
    const endHour = begin.getHours() + (row.duration_s || 0) / 3600;
    first = Math.min(first, begin.getHours());
    last = Math.max(last, Math.ceil(endHour));
  }
  last = Math.min(24, Math.max(last, first + 1));

  const hours = document.createElement("div");
  hours.className = "week-hours";
  const spacer = document.createElement("div");
  spacer.className = "week-head-spacer";
  hours.appendChild(spacer);
  for (let hour = first; hour < last; hour += 1) {
    const label = document.createElement("div");
    label.className = "week-hour";
    label.textContent = `${String(hour).padStart(2, "0")}:00`;
    hours.appendChild(label);
  }
  grid.appendChild(hours);

  for (let index = 0; index < 7; index += 1) {
    const day = new Date(start);
    day.setDate(day.getDate() + index);
    const weekend = index >= 5;

    const column = document.createElement("div");
    column.className = "week-col";
    if (weekend) column.classList.add("weekend");

    const head = document.createElement("div");
    head.className = "week-day";
    const name = document.createElement("div");
    name.className = "week-day-name";
    name.textContent = WEEK_DAYS[index];
    const number = document.createElement("div");
    number.className = "week-day-num";
    number.textContent = String(day.getDate());
    if (isoDay(day) === isoDay(new Date())) head.classList.add("today");
    head.append(name, number);

    const slots = document.createElement("div");
    slots.className = "week-slots";
    slots.style.height = `${(last - first) * HOUR_PX}px`;
    for (let hour = first; hour < last; hour += 1) {
      const line = document.createElement("div");
      line.className = "week-line";
      slots.appendChild(line);
    }

    for (const row of rows) {
      const begin = new Date(row.started_at);
      if (isNaN(begin) || isoDay(begin) !== isoDay(day)) continue;
      slots.appendChild(weekBlock(row, begin, first));
    }

    column.append(head, slots);
    grid.appendChild(column);
  }
}

function weekBlock(row, begin, firstHour) {
  const offset = begin.getHours() + begin.getMinutes() / 60 - firstHour;
  const hours = (row.duration_s || 0) / 3600;
  const block = document.createElement("button");
  block.className = "week-block";
  block.type = "button";
  // vermelho é a reunião que fechou, âmbar a que parou no meio: é a mesma
  // distinção que a legenda do pé explica
  if (row.status !== "finalized") block.classList.add("stopped");
  block.style.top = `${offset * HOUR_PX}px`;
  block.style.height = `${Math.max(hours * HOUR_PX, 22)}px`;
  block.addEventListener("click", () => openMeeting(row.id, row));

  const title = document.createElement("div");
  title.className = "week-block-title";
  title.textContent = nameOf(row);
  const meta = document.createElement("div");
  meta.className = "week-block-meta";
  const people = (row.people || []).length;
  meta.textContent = [
    timeLabel(row.started_at),
    row.duration_s ? clock(row.duration_s) : STATUS_SHORT[row.status],
    people ? `${people} ${people === 1 ? "pessoa" : "pessoas"}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  block.append(title, meta);
  block.title = `${nameOf(row)} — ${meta.textContent}`;
  return block;
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
  voz: "O reconhecimento de voz não foi aplicado",
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

async function loadTranscriptMeta() {
  const o = state.open;
  if (!o) return;
  try {
    o.transcriptMeta = await api("transcript_meta", o.id);
  } catch (err) {
    setBanner(errText(err));
    return;
  }
  if (state.open === o) renderTranscriptBar();
}

/** O seletor de prompt do rodapé, já apontando para o que vale agora. */
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

/** Qual resumo da lista está aberto à direita.

    Sem escolha explícita, o mais recente — é o que acabou de sair, ou o que a
    reunião usou por último. A escolha sobrevive ao polling: quem abriu o
    "para quem não estava" não pode ser jogado de volta no padrão a cada 2s. */
function selectedSummary(list) {
  const o = state.open;
  if (!list.length) return null;
  const chosen = o.summarySel && list.find((entry) => entry.id === o.summarySel);
  return chosen || list[0];
}

function renderSummary() {
  const o = state.open;
  renderSummaryPrompt();
  const info = (o && o.summaryState) || {};
  const list = info.summaries || [];
  const loading = !!o && o.summaryState === null;
  const running = info.status === "running";

  el("sum-count").textContent = list.length ? String(list.length) : "";
  const button = el("summary-btn");
  button.disabled = running || !state.prompts.list.length;
  button.textContent = running ? "Gerando…" : list.length ? "Gerar com outro prompt" : "Gerar resumo";

  renderSummaryList(list, running, info);
  renderSummaryView(list, loading, running, info);
}

function renderSummaryList(list, running, info) {
  const box = el("sum-list");
  box.innerHTML = "";
  const current = selectedSummary(list);

  if (running) {
    const pending = document.createElement("div");
    pending.className = "sum-item pending";
    const dot = document.createElement("span");
    dot.className = "pulse-dot";
    const text = document.createElement("div");
    const name = document.createElement("div");
    name.className = "sum-item-name";
    name.textContent = "Gerando…";
    const meta = document.createElement("div");
    meta.className = "sum-item-meta";
    meta.textContent = [info.provider, promptName(info.prompt)].filter(Boolean).join(" · ");
    text.append(name, meta);
    pending.append(dot, text);
    box.appendChild(pending);
  }

  for (const entry of list) {
    const item = document.createElement("button");
    item.className = "sum-item";
    item.type = "button";
    if (current && entry.id === current.id) item.classList.add("selected");
    item.addEventListener("click", () => {
      state.open.summarySel = entry.id;
      renderSummary();
    });

    const name = document.createElement("div");
    name.className = "sum-item-name";
    name.textContent = entry.prompt_name || entry.id;
    const meta = document.createElement("div");
    meta.className = "sum-item-meta";
    meta.textContent = [
      entry.provider,
      entry.generated_at ? timeLabel(entry.generated_at) : "",
      entry.prompt === state.prompts.default ? "prompt padrão" : "prompt próprio",
    ]
      .filter(Boolean)
      .join(" · ");
    item.append(name, meta);
    box.appendChild(item);
  }

  if (!list.length && !running) {
    const empty = document.createElement("div");
    empty.className = "sum-empty";
    empty.textContent = "Nenhum resumo ainda.";
    box.appendChild(empty);
  }
}

function promptName(promptId) {
  const found = state.prompts.list.find((prompt) => prompt.id === promptId);
  return (found && found.name) || promptId || "";
}

function renderSummaryView(list, loading, running, info) {
  const box = el("summary-box");
  const head = el("sum-view-head");
  const promptBox = el("sum-prompt-box");
  const current = selectedSummary(list);

  box.innerHTML = "";
  box.classList.remove("empty");
  head.classList.toggle("hidden", !current);
  promptBox.classList.toggle("hidden", !(current && current.prompt_instructions));

  if (current) {
    el("sum-name").textContent = current.prompt_name || current.id;
    el("sum-meta").textContent = [
      current.provider,
      current.generated_at ? dateLabel(current.generated_at) : "",
      current.segments ? `sobre ${current.segments} falas` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    if (current.prompt_instructions) el("sum-prompt-text").textContent = current.prompt_instructions;
    el("sum-regen").disabled = running;
    renderMarkdown(current.markdown, box);
    return;
  }

  box.classList.add("empty");

  if (loading) {
    const note = document.createElement("div");
    note.className = "slab-note";
    note.textContent = "Carregando…";
    box.appendChild(note);
    return;
  }
  if (running) {
    const wait = document.createElement("div");
    wait.className = "summary-running";
    const dot = document.createElement("span");
    dot.className = "pulse-dot";
    wait.append(
      dot,
      document.createTextNode(`Gerando o resumo com ${info.provider || "o provedor configurado"}…`)
    );
    box.appendChild(wait);
    return;
  }
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
    "O resumo, o nome e as tags saem juntos da transcrição. Escolha um prompt ao lado e gere: " +
    "cada prompt guarda o resumo dele, e o nome só é trocado se ninguém tiver dado um à mão.";
  box.append(title, note);
}

/** Gera com o prompt escolhido no rodapé — acrescenta um resumo à lista. */
async function onSummarize() {
  await requestSummary(el("summary-prompt").value);
}

/** Regera o que está aberto: mesmo prompt, resumo novo no lugar daquele. */
async function onRegenerateSummary() {
  const o = state.open;
  if (!o) return;
  const current = selectedSummary((o.summaryState && o.summaryState.summaries) || []);
  if (!current) return;
  await requestSummary(current.prompt || "");
}

async function requestSummary(promptId) {
  const o = state.open;
  if (!o) return;
  el("summary-btn").disabled = true;
  setAlert("meeting-error", "");
  try {
    o.summaryState = await api("summarize", o.id, promptId);
    // o resumo que está a caminho é o que a pessoa vai querer ver quando ele
    // chegar, e não aquele em que ela clicou antes de pedir
    o.summarySel = null;
    renderSummary();
  } catch (err) {
    setAlert("meeting-error", "Não foi possível pedir o resumo", errText(err));
    renderSummary();
  }
}

async function onCopySummary() {
  const o = state.open;
  const current = o && selectedSummary((o.summaryState && o.summaryState.summaries) || []);
  if (!current) return;
  const button = el("sum-copy");
  try {
    await navigator.clipboard.writeText(current.markdown);
    button.textContent = "Copiado";
  } catch (err) {
    setAlert("meeting-error", "Não foi possível copiar", errText(err));
    return;
  }
  setTimeout(() => {
    button.textContent = "Copiar";
  }, 1500);
}

/** Excluir um resumo confirma no próprio botão, como excluir a reunião: um
    `confirm()` do sistema não desenha bem na janela do pywebview. */
async function onDeleteSummary() {
  const o = state.open;
  const current = o && selectedSummary((o.summaryState && o.summaryState.summaries) || []);
  if (!current) return;
  const button = el("sum-delete");

  if (button.dataset.confirming !== "1") {
    button.dataset.confirming = "1";
    button.classList.add("confirming");
    button.textContent = "Confirmar";
    setTimeout(() => {
      if (button.dataset.confirming !== "1") return;
      delete button.dataset.confirming;
      button.classList.remove("confirming");
      button.textContent = "Excluir";
    }, 4000);
    return;
  }

  delete button.dataset.confirming;
  button.classList.remove("confirming");
  button.textContent = "Excluir";
  try {
    await api("delete_summary", o.id, current.id);
  } catch (err) {
    setAlert("meeting-error", "Não foi possível excluir o resumo", errText(err));
    return;
  }
  o.summarySel = null;
  await loadSummary();
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
  const final = s.transcribe_final ? `final: ${s.final_backend}` : "sem transcrição final";
  const diarize = s.diarize_default ? "diarização padrão ligada" : "diarização opcional";
  el("config-summary-text").textContent = `${live} · ${final} · ${diarize} · ${s.language}`;
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

// ------------------------------------------------------- perfis de voz

async function loadVoices() {
  state.voices = await api("voices");
  renderVoices();
}

function renderVoices() {
  const list = el("voice-list");
  const empty = el("voice-empty");
  if (!list || !empty) return;
  list.innerHTML = "";
  empty.classList.toggle("hidden", state.voices.length > 0);

  const mine = (state.settings && state.settings.my_voice) || "";
  for (const voice of state.voices) {
    const row = document.createElement("div");
    row.className = "prompt-row";
    if (voice.id === mine) row.classList.add("is-me");

    const meta = document.createElement("div");
    meta.className = "voice-meta";
    const name = document.createElement("span");
    name.className = "prompt-name";
    name.textContent = voice.name;
    meta.appendChild(name);
    const emails = document.createElement("span");
    emails.className = "voice-emails";
    emails.textContent = (voice.emails && voice.emails.length)
      ? voice.emails.join(" · ")
      : "sem e-mail";
    meta.appendChild(emails);
    row.appendChild(meta);

    const detail = document.createElement("span");
    detail.className = "voice-samples";
    detail.textContent = `${voice.samples} amostra${voice.samples === 1 ? "" : "s"}`;
    row.appendChild(detail);

    const edit = document.createElement("button");
    edit.className = "btn outline small";
    edit.textContent = "Editar";
    edit.addEventListener("click", () => openVoiceEditor(voice.id));
    row.appendChild(edit);

    const remove = document.createElement("button");
    remove.className = "btn outline small danger";
    remove.textContent = "Apagar";
    remove.addEventListener("click", async () => {
      if (remove.dataset.confirm !== "yes") {
        remove.dataset.confirm = "yes";
        remove.textContent = "Confirmar";
        setTimeout(() => {
          if (remove.isConnected) {
            remove.dataset.confirm = "";
            remove.textContent = "Apagar";
          }
        }, 6000);
        return;
      }
      try {
        await api("delete_voice", voice.id);
        await loadVoices();
      } catch (err) {
        setAlert("settings-error", "Não foi possível apagar a voz", errText(err));
      }
    });
    row.appendChild(remove);
    list.appendChild(row);
  }
}

function knownEmails() {
  const seen = new Map();
  const add = (email, name) => {
    const key = String(email || "").trim().toLowerCase();
    if (!key.includes("@")) return;
    if (!seen.has(key)) seen.set(key, { email: key, name: (name || "").trim() });
  };
  for (const event of (state.agenda && state.agenda.events) || []) {
    for (const person of event.attendees || []) add(person.email, person.name);
  }
  for (const voice of state.voices) {
    for (const email of voice.emails || []) add(email, voice.name);
  }
  return [...seen.values()].sort((a, b) => a.email.localeCompare(b.email));
}

function renderVoiceEmailEditor() {
  const box = el("voice-edit-emails");
  const pick = el("voice-edit-pick");
  if (!box || !pick) return;
  box.innerHTML = "";
  const selected = state.editingVoiceEmails;
  if (!selected.length) {
    const empty = document.createElement("span");
    empty.className = "voice-emails";
    empty.textContent = "Nenhum e-mail ainda.";
    box.appendChild(empty);
  }
  for (const email of selected) {
    const chip = document.createElement("div");
    chip.className = "email-chip";
    const label = document.createElement("span");
    label.textContent = email;
    chip.appendChild(label);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.title = "Remover";
    remove.setAttribute("aria-label", `Remover ${email}`);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      state.editingVoiceEmails = selected.filter((item) => item !== email);
      renderVoiceEmailEditor();
    });
    chip.appendChild(remove);
    box.appendChild(chip);
  }

  pick.innerHTML = "";
  pick.appendChild(new Option("Escolher da agenda…", ""));
  for (const person of knownEmails()) {
    if (selected.includes(person.email)) continue;
    const label = person.name ? `${person.name} — ${person.email}` : person.email;
    pick.appendChild(new Option(label, person.email));
  }
}

function addVoiceEmail(raw) {
  const email = String(raw || "").trim().toLowerCase();
  setAlert("voice-edit-error", "");
  if (!email) return;
  if (!email.includes("@") || email.includes(" ")) {
    setAlert("voice-edit-error", "Informe um e-mail válido");
    return;
  }
  if (!state.editingVoiceEmails.includes(email)) {
    state.editingVoiceEmails = [...state.editingVoiceEmails, email].sort();
  }
  el("voice-edit-email").value = "";
  el("voice-edit-pick").value = "";
  renderVoiceEmailEditor();
}

async function openVoiceEditor(voiceId) {
  const voice = state.voices.find((item) => item.id === voiceId);
  if (!voice) return;
  state.editingVoice = voice.id;
  state.editingVoiceEmails = [...(voice.emails || [])];
  setAlert("voice-edit-error", "");
  el("voice-edit-dialog-title").textContent = `Editar "${voice.name}"`;
  el("voice-edit-name").value = voice.name;
  renderVoiceEmailEditor();
  el("voice-edit-dialog").showModal();
  el("voice-edit-name").focus();
}

function closeVoiceEditor() {
  state.editingVoice = null;
  state.editingVoiceEmails = [];
  const dialog = el("voice-edit-dialog");
  if (dialog.open) dialog.close();
}

async function onSaveVoiceProfile() {
  const profileId = state.editingVoice;
  if (!profileId) return;
  const button = el("voice-edit-save");
  button.disabled = true;
  setAlert("voice-edit-error", "");
  try {
    await api("rename_voice", profileId, el("voice-edit-name").value, state.editingVoiceEmails);
    closeVoiceEditor();
    await loadVoices();
  } catch (err) {
    setAlert("voice-edit-error", "Não foi possível salvar a voz", errText(err));
  } finally {
    button.disabled = false;
  }
}

async function openVoiceDialog(seg) {
  const o = state.open;
  if (!o || !seg.speaker_key || o.meeting.status === "finalizing") return;
  state.enrollingSpeaker = seg;
  setAlert("voice-error", "");
  try {
    await loadVoices();
  } catch (err) {
    setAlert("meeting-error", "Não foi possível carregar as vozes", errText(err));
    return;
  }

  el("voice-speaker-label").textContent =
    `${seg.speaker}: confirme a pessoa para cadastrar esta voz.`;
  const select = el("voice-candidate");
  select.innerHTML = "";
  select.appendChild(new Option("Digite um nome ou escolha uma sugestão", ""));
  for (const voice of state.voices) {
    const option = new Option(`${voice.name} — voz já cadastrada`, `profile:${voice.id}`);
    option.dataset.name = voice.name;
    option.dataset.email = (voice.emails && voice.emails[0]) || "";
    select.appendChild(option);
  }
  const attendees = (o.meeting.event && o.meeting.event.attendees) || [];
  for (const [index, person] of attendees.entries()) {
    const name = person.name || person.email;
    if (!name) continue;
    const option = new Option(`${name} — convidado`, `attendee:${index}`);
    option.dataset.name = person.name || person.email || "";
    option.dataset.email = person.email || "";
    select.appendChild(option);
  }
  el("voice-name").value = seg.voice_profile_id
    ? (state.voices.find((voice) => voice.id === seg.voice_profile_id) || {}).name || seg.speaker
    : "";
  const existing = state.voices.find((voice) => voice.id === seg.voice_profile_id);
  if (existing) select.value = `profile:${existing.id}`;
  el("voice-email").value = (existing && existing.emails && existing.emails[0]) || "";
  el("voice-dialog").showModal();
  el("voice-name").focus();
}

function closeVoiceDialog() {
  state.enrollingSpeaker = null;
  el("voice-dialog").close();
}

function onVoiceCandidateChange() {
  const option = el("voice-candidate").selectedOptions[0];
  if (!option || !option.value) return;
  el("voice-name").value = option.dataset.name || "";
  el("voice-email").value = option.dataset.email || "";
}

async function onSaveVoice() {
  const o = state.open;
  const seg = state.enrollingSpeaker;
  if (!o || !seg) return;
  const option = el("voice-candidate").selectedOptions[0];
  const profileId = option && option.value.startsWith("profile:")
    ? option.value.slice("profile:".length)
    : "";
  const button = el("voice-save");
  button.disabled = true;
  setAlert("voice-error", "");
  try {
    await api(
      "enroll_voice",
      o.id,
      seg.speaker_key,
      el("voice-name").value,
      el("voice-email").value,
      profileId,
    );
    closeVoiceDialog();
    await loadVoices();
    // o nome vale para as falas já na tela, então o chat recomeça do zero
    resetChat();
    await pollChat();
    if (!o.live) {
      // fora do ar vivo o daemon regera o resumo com o nome novo
      o.settling = SETTLE_TICKS;
      await refreshOpenMeeting();
      if (state.tab === "resumo") await loadSummary();
    }
  } catch (err) {
    setAlert("voice-error", "Não foi possível cadastrar esta voz", errText(err));
  } finally {
    button.disabled = false;
  }
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

/** Os perfis cadastrados, mais "nenhum" — que é o padrão e não é erro. */
function myVoiceOptions() {
  return [
    ["", "— nenhuma escolhida"],
    ...state.voices.map((voice) => [voice.id, voice.name]),
  ];
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
  el("set-final-on").checked = s.transcribe_final;
  el("set-diarize-default").checked = s.diarize_default;
  el("set-auto-summarize").checked = s.auto_summarize;
  el("set-openai-url").value = s.openai_base_url || "";
  el("set-openai-key").value = s.openai_api_key || "";
  el("set-openai-model").value = s.openai_model || "";
  el("set-groq-key").value = s.groq_api_key || "";
  el("set-gog-account").value = s.gog_account || "";
  el("set-language").value = s.language;
  fillSelect("set-my-voice", myVoiceOptions(), s.my_voice || "");
  renderProviderFields();
  renderFinalFields();
}

/** Os campos da API só existem quando o provedor que os usa está escolhido. */
/** Com a transcrição final desligada, o motor dela não decide nada — e um
    <select> aceso sobre coisa que não roda é convite a ajustar o que não tem
    efeito. */
function renderFinalFields() {
  const on = el("set-final-on").checked;
  el("set-final-backend").disabled = !on;
  el("set-final-backend").closest("section").classList.toggle("off", !on);
}

function renderProviderFields() {
  el("openai-fields").classList.toggle("hidden", el("set-summary-provider").value !== "openai_api");
  el("groq-fields").classList.toggle("hidden", el("set-final-backend").value !== "groq");
  el("gog-fields").classList.toggle("hidden", el("set-calendar-provider").value !== "gog");
}

async function loadSettings() {
  state.backends = await window.pywebview.api.backend_options();
  state.settings = await api("get_settings");
  await Promise.all([loadPrompts(), loadVoices()]);
  renderConfigSummary();
}

function showSettingsTab(name) {
  state.settingsTab = name;
  for (const [key, id] of Object.entries(SETTINGS_TABS)) el(id).classList.toggle("hidden", key !== name);
  for (const tab of document.querySelectorAll(".set-tab")) {
    tab.classList.toggle("selected", tab.dataset.setTab === name);
  }
  el("settings-save").classList.toggle("hidden", name === "vozes");
}

async function openSettings() {
  flushNotes();
  setAlert("settings-error", "");
  el("settings-note").textContent = "";
  closePromptEditor(); // edição pendente não atravessa uma saída da tela
  showSettingsTab(state.settingsTab);
  await Promise.all([loadPrompts(), loadVoices()]);
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
      transcribe_final: el("set-final-on").checked,
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
      my_voice: el("set-my-voice").value,
      language: el("set-language").value,
    });
    renderSettings(); // o daemon é quem diz o que ficou valendo
    renderVoices(); // o selo de "sou eu" mudou de linha
    // ligar/desligar a passada final muda se o botão Retranscrever existe
    if (state.open) renderTranscriptBar();
    renderConfigSummary();
    // provedor/conta mudaram: a lista da home precisa refletir já
    refreshAgenda(true);
    // "minha voz" decide quem se junta a "Você" na ficha, e é o nome do
    // cumprimento da home — os dois estão desatualizados agora
    await loadProfile();
    if (state.open) renderPeople();
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

/** Abre o formulário avulso e leva o cursor pra dentro dele: quem clicou no
    link já decidiu que vai gravar algo que não está na agenda. */
function onAdHoc() {
  revealAdHoc(true);
  el("start-card").scrollIntoView({ block: "nearest", behavior: "smooth" });
  el("name").focus();
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

async function onOpenUrl(url) {
  try {
    await api("open_url", url);
  } catch (err) {
    setAlert("start-error", "Não foi possível abrir o link", errText(err));
  }
}

/** Exporta a transcrição como markdown e abre a pasta onde ela caiu: um
    arquivo escrito que ninguém encontra é o mesmo que nenhum arquivo. */
async function onExportTranscript() {
  const o = state.open;
  if (!o) return;
  const button = el("tr-export");
  button.disabled = true;
  try {
    await api("export_transcript", o.id);
    await api("open_folder", o.id);
    button.textContent = "Exportada";
    setTimeout(() => {
      button.textContent = "Exportar";
    }, 2000);
  } catch (err) {
    setAlert("meeting-error", "Não foi possível exportar", errText(err));
  } finally {
    button.disabled = false;
  }
}

async function onManageVoices() {
  await openSettings();
  showSettingsTab("vozes");
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
  el("adhoc-link").addEventListener("click", onAdHoc);
  el("stop-btn").addEventListener("click", onStop);
  el("finalize-btn").addEventListener("click", onFinalize);
  el("tr-export").addEventListener("click", onExportTranscript);
  el("mv-diarize").addEventListener("change", onDiarizeChange);
  el("files-tab").addEventListener("click", onOpenFolder);
  el("manage-voices").addEventListener("click", onManageVoices);
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
  el("search").addEventListener("input", onSearchInput);
  el("browse-btn").addEventListener("click", openBrowse);

  for (const tab of document.querySelectorAll(".tab[data-tab]")) {
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
  }

  // ---- acervo: uma barra de filtros para as duas superfícies
  el("bv-q").addEventListener("input", (event) => {
    state.browse.query = event.target.value;
    renderFilters();
    scheduleBrowse();
  });
  el("bv-range").addEventListener("change", (event) => {
    state.browse.rangeDays = event.target.value;
    renderFilters();
    runBrowse();
  });
  el("bv-tag-add").addEventListener("change", (event) => {
    if (event.target.value) toggleTag(event.target.value);
  });
  el("bv-people-add").addEventListener("change", (event) => {
    if (event.target.value) togglePerson(event.target.value);
  });
  el("bv-clear").addEventListener("click", clearBrowseFilters);
  for (const button of el("bv-modes").querySelectorAll(".seg-btn")) {
    button.addEventListener("click", () => setBrowseMode(button.dataset.mode));
  }
  for (const col of el("bv-table-head").querySelectorAll(".mt-col[data-sort]")) {
    col.addEventListener("click", () => onSortColumn(col.dataset.sort));
  }
  el("bv-prev").addEventListener("click", () => shiftWeek(-1));
  el("bv-next").addEventListener("click", () => shiftWeek(1));
  el("bv-this").addEventListener("click", () => {
    state.browse.weekStart = isoDay(weekStartOf(new Date()));
    renderBrowse();
    runBrowse();
  });

  // Ctrl+K vai para a busca de qualquer tela — é o atalho que o pé da tabela
  // promete, e o único que o app tem.
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      el("search").focus();
      el("search").select();
    }
  });

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
  el("sum-regen").addEventListener("click", onRegenerateSummary);
  el("sum-copy").addEventListener("click", onCopySummary);
  el("sum-delete").addEventListener("click", onDeleteSummary);

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
  el("voice-candidate").addEventListener("change", onVoiceCandidateChange);
  el("voice-save").addEventListener("click", onSaveVoice);
  el("voice-cancel").addEventListener("click", closeVoiceDialog);
  el("voice-close").addEventListener("click", closeVoiceDialog);
  el("voice-dialog").addEventListener("close", () => {
    state.enrollingSpeaker = null;
  });
  el("voice-dialog").addEventListener("click", (event) => {
    if (event.target === el("voice-dialog")) closeVoiceDialog();
  });
  for (const id of ["voice-name", "voice-email"]) {
    el(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        onSaveVoice();
      }
    });
  }
  el("voice-edit-save").addEventListener("click", onSaveVoiceProfile);
  el("voice-edit-cancel").addEventListener("click", closeVoiceEditor);
  el("voice-edit-close").addEventListener("click", closeVoiceEditor);
  el("voice-edit-add").addEventListener("click", () => addVoiceEmail(el("voice-edit-email").value));
  el("voice-edit-pick").addEventListener("change", () => {
    if (el("voice-edit-pick").value) addVoiceEmail(el("voice-edit-pick").value);
  });
  el("voice-edit-dialog").addEventListener("close", () => {
    state.editingVoice = null;
    state.editingVoiceEmails = [];
  });
  el("voice-edit-dialog").addEventListener("click", (event) => {
    if (event.target === el("voice-edit-dialog")) closeVoiceEditor();
  });
  el("voice-edit-name").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      onSaveVoiceProfile();
    }
  });
  el("voice-edit-email").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addVoiceEmail(el("voice-edit-email").value);
    }
  });
  el("set-final-backend").addEventListener("change", renderProviderFields);
  el("set-final-on").addEventListener("change", renderFinalFields);

  await loadSettings();
  await loadVoices(); // a ficha da reunião mostra e-mail de voz cadastrada
  await loadProfile();
  await loadHistory();
  await refreshAgenda(false);
  loadFacets(); // o contador de "Todas as reuniões" na barra lateral
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
