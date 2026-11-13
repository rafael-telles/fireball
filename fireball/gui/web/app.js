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
const SPEAKER_COLORS = ["#8fb4ff", "#c3a6ff", "#6fd3b8", "#f0c674", "#7dd3fc", "#f9a8d4"];

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
  // versão/canal/última conferência de atualização, do status do daemon
  version: "",
  appimage: "",
  update: null,
  updateTimer: null, // confirmação de "Baixar e atualizar" pendente
  updateNotesKey: "", // qual changelog já foi montado (para não reabrir à toa)
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
  ask: "view-ask",
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
  sobre: "set-pane-sobre",
};

const ICONS = {
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  video: '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="m16 10 6-3v10l-6-3"/>',
  pin: '<path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
  repeat: '<path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
};

/** Ícone de traço, na cor do texto em volta. */
function icon(name, size = 15) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name];
  return svg;
}

function showView(name) {
  closeVoiceMenu();
  state.view = name;
  document.body.dataset.view = name;
  for (const [key, id] of Object.entries(VIEWS)) el(id).classList.toggle("hidden", key !== name);
  // A reunião é a única tela que não mora no scroller: ela tem colunas próprias
  // (nota + transcrição). Com ela aberta, o scroller sai de cena inteiro.
  const rolagem = document.querySelector(".nota-scroll");
  if (rolagem) rolagem.classList.toggle("hidden", name === "meeting");
  el("new-meeting-btn").classList.toggle("on", name === "home");
  el("nav-perguntar").classList.toggle("on", name === "ask");
  el("browse-btn").classList.toggle("on", name === "browse");
  el("ask-input").placeholder =
    name === "meeting" ? "Perguntar sobre esta reunião" : "Perguntar sobre todas as reuniões";
}

// ------------------------------------------------------------------ ponte

// Toda chamada ao daemon volta como {ok, result} ou {ok:false, error, kind}.
async function api(name, ...args) {
  const res = await window.pywebview.api[name](...args);
  if (!res || res.ok !== true) throw new Error((res && res.error) || "falha falando com o daemon");
  return res.result;
}

/* O cartão do rodapé da sidebar dizia "Nada gravando agora" para sempre — o id
   existia no HTML e ninguém escrevia nele. Ele é o único lugar da sidebar que
   fala do estado, então passa a sair do mesmo status que pinta o resto. */
function pintarEstadoLocal() {
  const alvo = el("local-note");
  if (!alvo) return;
  const ativo = state.active;
  // o cartão "pronto para gravar" sai de cena quando o da gravação entra
  el("local-card").classList.toggle("hidden", Boolean(ativo));
  if (!ativo) {
    alvo.textContent = "Microfone + áudio do sistema";
    return;
  }
  const desde = ativo.started_at ? timeLabel(ativo.started_at) : "";
  alvo.textContent =
    ativo.status === "paused"
      ? "Gravação pausada"
      : desde
        ? `Gravando desde ${desde}`
        : "Gravando agora";
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
  if (name === "Você") return "var(--me)";
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
  wrap.dataset.seq = seg.seq;
  if (seg.speaker_key) wrap.dataset.key = seg.speaker_key;
  // Só a própria fala vai para a direita. "Sala 1" também vem do microfone,
  // mas é outra pessoa na sala: do lado de lá ela se confundia com "você"
  // (mesma bolha, mesma cor). O que o leitor quer saber é quem falou.
  if (speaker === "Você") wrap.classList.add("mine");
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
      name.title = "Trocar a voz desta fala";
      name.setAttribute("aria-label", `Quem é ${speaker}?`);
      name.setAttribute("aria-haspopup", "menu");
      name.setAttribute("aria-expanded", "false");
      name.appendChild(icon("chevron", 12));
      name.addEventListener("click", () => openVoiceMenu(seg, name));
    }
    wrap.appendChild(name);
    if (canEnroll && ANON_SLOT.test(speaker)) {
      const ask = document.createElement("span");
      ask.className = "who-ask";
      ask.textContent = "quem é?";
      wrap.appendChild(ask);
    }
  }

  const row = document.createElement("div");
  row.className = "bubble-row";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = seg.text || "";
  row.appendChild(bubble);
  wrap.appendChild(row);

  // o horário e as ações do trecho dividem a linha de cima, como no Granola.
  // As ações moram aqui, e não ao lado da bolha: em fluxo ali elas ocupavam
  // ~130px e espremiam toda fala a dois terços da coluna.
  const stamp = segmentStamp(seg);
  stamp.appendChild(segmentActions(seg));
  wrap.appendChild(stamp);
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
  edit.title = "Mexe só na transcrição — o áudio original não muda.";
  edit.addEventListener("click", (event) => startEdit(event.target.closest(".msg"), seg));
  box.appendChild(edit);

  const remove = document.createElement("button");
  remove.className = "seg-action danger";
  remove.textContent = "✕ excluir";
  remove.title = "Mexe só na transcrição — o áudio original não muda.";
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

/** O selo do cabeçalho da reunião em andamento: estado e tempo corrido. */
function liveLabel(m) {
  const what = m.status === "paused" ? "Pausado" : m.status === "stopping" ? "Parando" : "Ao vivo";
  return `${what} · ${elapsedSince(m.started_at)}`;
}

function metaLine(o) {
  const m = o.meeting;
  const parts = [];
  if (o.live) {
    parts.push(`Começou ${timeLabel(m.started_at)}`);
    const event = m.event || {};
    if (event.conference_url) parts.push(conferenceName(event.conference_url));
    const guests = (event.attendees || []).length;
    if (guests) parts.push(`${guests} pessoas`);
    return parts.join(" · ");
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
  if (o.live) el("mv-elapsed").textContent = liveLabel(m);
  el("mv-elapsed").classList.toggle("dim", m.status === "paused");
  el("mv-crumbs").classList.toggle("hidden", o.live);
  el("view-meeting").classList.toggle("ao-vivo", Boolean(o.live));
  if (!o.live) {
    const days = daysAgo(m.started_at);
    el("crumb-when").textContent =
      days === 0 ? "Hoje" : days === 1 ? "Ontem" : days === null ? "" : longDate(m.started_at);
  }

  // parar dispara o resumo quando ele é automático, e o botão diz isso
  const stop = el("stop-btn");
  stop.classList.toggle("hidden", !o.live);
  stop.disabled = m.status === "stopping";
  const resume = state.settings && state.settings.auto_summarize !== false;
  stop.textContent = m.status === "stopping" ? "Parando…" : resume ? "Parar e resumir" : "Parar";

  const pause = el("pause-btn");
  const pausable = o.live && (m.status === "recording" || m.status === "paused");
  pause.classList.toggle("hidden", !pausable);
  pause.classList.toggle("is-paused", m.status === "paused");
  pause.disabled = false;
  pause.textContent = m.status === "paused" ? "Retomar" : "Pausar";

  // apagar no cabeçalho: fora da gravação (o áudio está aberto) e fora da
  // finalização (há um job lendo a pasta)
  const apagar = el("delete-meeting-btn");
  apagar.classList.toggle("hidden", o.live || m.status === "finalizing");

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
  // o que não cabe nos 400px da coluna vai só para o `title`: a data de
  // geração é útil, mas a reunião já tem a data no cabeçalho da nota
  const detalhe = [];
  if (o.live) {
    bits.push(
      m.transcribe_live === false
        ? "Só gravando — ao vivo desligada"
        : "Transcrevendo ao vivo"
    );
    if (m.backend) bits.push(m.backend);
  } else if (!meta) {
    bits.push("Carregando…");
  } else if (meta.kind === "final") {
    bits.push("Transcrição final");
    if (meta.backend) bits.push(meta.backend);
    if (meta.generated_at) detalhe.push(`gerada em ${dateLabel(meta.generated_at)}`);
    bits.push(`${meta.segments} ${meta.segments === 1 ? "segmento" : "segmentos"}`);
    if (meta.diarized) bits.push("com falantes separados");
  } else if (meta.kind === "live") {
    bits.push("Transcrição em tempo real");
    if (meta.backend) bits.push(meta.backend);
    bits.push(`${meta.segments} ${meta.segments === 1 ? "segmento" : "segmentos"}`);
  } else {
    bits.push("Sem transcrição");
  }
  // sem isto, quem desligou a passada final não entende por que o botão sumiu
  if (!enabled && !o.live) bits.push("final desligada");
  el("tr-meta").textContent = bits.join(" · ");
  el("tr-meta").title = [...bits, ...detalhe].join(" · ");

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
  // só a procedência: que o campo é editável, o title dele já diz
  el("rename-note").textContent =
    m.name_source === "ai"
      ? "nome escrito pela IA"
      : m.name_source === "calendar"
        ? "nome da agenda"
        : "";

  renderTags(m);
  renderPeople();
  renderDescription(m);

  const day = longDate(m.started_at);
  const from = timeLabel(m.started_at);
  const to = m.ended_at ? timeLabel(m.ended_at) : null;
  // "11:28 – 11:28" não informa nada: o fim só entra quando diz outra hora
  // que a de início. Reunião de segundos (ou encerrada no mesmo minuto) fica
  // com a hora de começo, e a duração real já está na linha de metadados.
  const faixa = !to
    ? `${from} — em andamento`
    : to === from
      ? from
      : `${from} – ${to}`;
  el("mv-when").textContent = [day, faixa].filter(Boolean).join(" · ");
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
  // "2 detectadas" era um número sem substantivo: ninguém sabia de quê. O chip
  // fica ao lado da data e conta quem passou pela reunião.
  el("mv-people-count").textContent = detected
    ? `${detected} ${detected === 1 ? "pessoa" : "pessoas"}`
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
  if (kind === "mine") return "var(--me)";
  if (kind === "other") return "var(--speaker)";
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
  renderSpeakerShare();
}

/** "Quem falou" no topo da transcrição: uma barra só, cada pessoa na cor
    que ela tem nas falas. Os grupos são os da ficha, então quem aparece no
    microfone e pelo nome reconhecido soma numa fatia só. */
function renderSpeakerShare() {
  const o = state.open;
  const box = el("spk-share");
  const stats = o && o.stats;
  const groups = stats && stats.timed
    ? participantGroups().filter((group) => group.seconds > 0).sort((a, b) => b.seconds - a.seconds)
    : [];
  box.classList.toggle("hidden", !groups.length);
  if (!groups.length) return;

  const total = groups.reduce((sum, group) => sum + group.seconds, 0);
  el("spk-note").textContent =
    `${spanLabel(stats.speech_seconds)} de fala · ${spanLabel(stats.silence_seconds)} de silêncio`;
  const bar = el("spk-bar");
  const legend = el("spk-legend");
  bar.innerHTML = "";
  legend.innerHTML = "";
  const parts = [];
  groups.forEach((group, index) => {
    const share = group.seconds / total;
    const pct = Math.round(share * 100);
    const color = groupColor(group);
    const label = groupLabel(group);
    parts.push(`${label} ${pct}%`);

    const slice = document.createElement("span");
    slice.className = "spk-slice";
    slice.dataset.i = index;
    slice.style.width = `${share * 100}%`;
    slice.style.background = color;
    bar.appendChild(slice);

    const item = document.createElement("span");
    item.className = "spk-item";
    item.dataset.i = index;
    const swatch = document.createElement("i");
    swatch.style.background = color;
    item.append(swatch, `${label} ${pct}%`);
    // passar por um nome apaga as outras fatias: a barra diz de quem é cada cor
    item.addEventListener("pointerenter", () => {
      for (const node of bar.children) node.classList.toggle("off", node.dataset.i !== String(index));
    });
    item.addEventListener("pointerleave", () => {
      for (const node of bar.children) node.classList.remove("off");
    });
    legend.appendChild(item);
  });
  bar.setAttribute("aria-label", `Tempo de fala: ${parts.join(", ")}`);
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
  // A nota é UM documento: as seções (anotações, resumo, ficha) ficam
  // empilhadas na mesma coluna, como no Granola. Não há aba para esconder —
  // `name` só diz qual delas pediu a abertura, e todas carregam juntas.
  for (const tab of document.querySelectorAll(".tab[data-tab]")) {
    tab.classList.toggle("selected", tab.dataset.tab === name);
  }
  loadNotes();
  renderSummary();
  loadSummary();
  renderStats();
  loadStats();
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
  resetChat(id); // a conversa é desta reunião, não da janela

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
  renderLiveCard();
  pintarEstadoLocal();
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
  renderRecent();
}

function renderLiveCard() {
  const card = el("live-card");
  const active = state.active;
  card.classList.toggle("hidden", !active);
  if (!active) return;
  const paused = active.status === "paused";
  card.classList.toggle("paused", paused);
  const stopping = active.status === "stopping";
  el("live-card-label").textContent = paused ? "Pausado" : stopping ? "Parando…" : "Gravando";
  el("live-card-time").textContent = elapsedSince(active.started_at);
  el("live-card-name").textContent = nameOf(active);
  el("live-card-name").classList.toggle("unnamed", !active.name);
  el("live-card-stop").disabled = stopping;

  // os botões são só ícone: o que dizem vai no título e no rótulo acessível
  const pause = el("live-card-pause");
  pause.classList.toggle("hidden", !(active.status === "recording" || paused));
  pause.classList.toggle("is-paused", paused);
  pause.title = paused ? "Retomar" : "Pausar";
  pause.setAttribute("aria-label", pause.title);
}

function renderHome() {
  const active = state.active;
  el("home-kicker").textContent = active
    ? `${active.status === "paused" ? "Pausada" : "Gravando agora"} · ${nameOf(active)}`
    : "";
  el("home-kicker").classList.toggle("accent", Boolean(active));

  const first = ((state.profile && state.profile.name) || "").trim().split(/\s+/)[0] || "";
  const hello = greetingFor(new Date().getHours());
  el("home-greeting").textContent = first ? `${hello}, ${first}.` : `${hello}.`;

  renderAgenda();
  renderRecent();
}

const RECENT_COUNT = 3;

/** A primeira frase do resumo, sem markdown: é o que cabe num cartão. */
function summaryGist(markdown) {
  const text = String(markdown || "")
    .split("\n")
    .map((line) => line.replace(/^[#>*\-\d.\s]+/, "").replace(/[*_`[\]]/g, "").trim())
    .find((line) => line.length > 30);
  if (!text) return "";
  const cut = text.match(/^.{30,180}?[.!?](\s|$)/);
  return cut ? cut[0].trim() : `${text.slice(0, 160).trim()}…`;
}

async function loadRecentGists(rows) {
  state.gists = state.gists || {};
  const missing = rows.filter((row) => !(row.id in state.gists) && row.name);
  if (!missing.length) return;
  for (const row of missing) state.gists[row.id] = "";
  await Promise.all(
    missing.map(async (row) => {
      try {
        const data = await api("summary", row.id);
        state.gists[row.id] = summaryGist(data.summary && data.summary.markdown);
      } catch (err) {
        state.gists[row.id] = "";
      }
    })
  );
  renderRecent();
}

function renderRecent() {
  const box = el("home-recent");
  if (!box) return;
  const rows = [...(state.rows || [])]
    .filter((row) => !LIVE_STATUSES.has(row.status))
    .sort((a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0))
    .slice(0, RECENT_COUNT);
  state.gists = state.gists || {};
  const key = JSON.stringify(
    rows.map((row) => [row.id, row.name, row.status, row.tags, state.gists[row.id], daysAgo(row.started_at)])
  );
  if (key === state.recentKey) return;
  state.recentKey = key;
  box.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "recent-empty";
    empty.textContent = "As reuniões gravadas aparecem aqui.";
    box.appendChild(empty);
    return;
  }
  state.gists = state.gists || {};
  for (const row of rows) {
    const card = document.createElement("button");
    card.className = "recent-card";
    card.addEventListener("click", () => openMeeting(row.id, row));
    const pending = !row.name || row.status === "finalizing" || row.summary_status === "running";
    if (pending && !row.name) card.classList.add("pending");

    const when = document.createElement("span");
    when.className = "recent-when";
    const days = daysAgo(row.started_at);
    const day = days === 0 ? "Hoje" : days === 1 ? "Ontem" : shortDate(row.started_at);
    when.textContent = [`${day}, ${timeLabel(row.started_at)}`, durationLabel(row)].filter(Boolean).join(" · ");
    card.appendChild(when);

    if (!row.name) {
      const note = document.createElement("span");
      note.className = "recent-running";
      note.append(icon("refresh", 14));
      const text = document.createElement("span");
      text.className = "shimmer";
      text.textContent =
        row.status === "finalizing"
          ? "Transcrição final rodando — a IA nomeia ao terminar"
          : "Sem nome — a IA nomeia quando houver resumo";
      note.appendChild(text);
      card.appendChild(note);
      box.appendChild(card);
      continue;
    }

    const title = document.createElement("span");
    title.className = "recent-title";
    title.textContent = nameOf(row);
    card.appendChild(title);

    const gist = state.gists[row.id];
    if (gist) {
      const body = document.createElement("span");
      body.className = "recent-gist";
      body.textContent = gist;
      card.appendChild(body);
    }
    if ((row.tags || []).length) {
      const tags = document.createElement("span");
      tags.className = "recent-tags";
      for (const tag of row.tags.slice(0, 3)) {
        const chip = document.createElement("span");
        chip.className = "tag-pill";
        chip.textContent = tag;
        tags.appendChild(chip);
      }
      card.appendChild(tags);
    }
    box.appendChild(card);
  }
  loadRecentGists(rows);
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
  const agenda = state.agenda;

  // o status chega a cada 2s e redesenha a home; refazer o cartão sem mudança
  // nenhuma repetia a animação de entrada e soltava o hover de quem estava nele
  const ready = agenda && !["off", "loading", "error"].includes(agenda.status);
  const upcoming = ready ? upcomingEvents() : [];
  const key = JSON.stringify([
    agenda && agenda.status,
    agenda && agenda.error,
    Boolean(state.active),
    upcoming.map((event) => [event.id, event.start, event.end, event.title]),
    upcoming[0] && startsInLabel(upcoming[0].start),
    new Date().toDateString(),
  ]);
  if (key === state.agendaKey && card.childElementCount) return;
  state.agendaKey = key;
  card.innerHTML = "";
  rest.innerHTML = "";

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
  card.className = "next-card rise d1";
  // o brilho que segue o mouse: a posição vai em variáveis, o desenho é do CSS
  card.addEventListener("pointermove", (ev) => {
    const box = card.getBoundingClientRect();
    card.style.setProperty("--mx", `${ev.clientX - box.left}px`);
    card.style.setProperty("--my", `${ev.clientY - box.top}px`);
  });

  const top = document.createElement("div");
  top.className = "next-top";
  const dot = document.createElement("span");
  dot.className = "ring-dot";
  const when = document.createElement("span");
  when.className = "next-when";
  const starts = startsInLabel(event.start);
  when.textContent = `Próxima · ${starts.charAt(0).toLowerCase()}${starts.slice(1)}`;
  top.append(dot, when);
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

  const meta = document.createElement("div");
  meta.className = "next-meta";
  const planned = plannedLabel(event);
  meta.appendChild(
    metaBit(
      "clock",
      event.end ? `${timeLabel(event.start)} – ${timeLabel(event.end)}` : timeLabel(event.start),
      planned && `${planned} prevista`
    )
  );
  if (event.conference_url) meta.appendChild(metaBit("video", conferenceName(event.conference_url)));
  else if (event.location) meta.appendChild(metaBit("pin", event.location));
  const repeat = recurrenceLabel(event);
  if (repeat) meta.appendChild(metaBit("repeat", repeat));

  // sem você na lista: quem está lendo sabe que vai à própria reunião, e o
  // que a contagem precisa dizer é quantos *outros* estarão lá
  const attendees = (event.attendees || []).filter(
    (person) => !person.self && (person.name || person.email)
  );
  if (attendees.length) {
    const people = document.createElement("span");
    people.className = "next-bit";
    people.title = attendees.map((person) => person.name || person.email).join(", ");
    const stack = document.createElement("span");
    stack.className = "face-stack";
    for (const person of attendees.slice(0, 3)) stack.appendChild(faceOf(person.name || person.email));
    if (attendees.length > 3) stack.appendChild(faceOf(`+${attendees.length - 3}`, true));
    people.append(
      stack,
      `${attendees.length} ${attendees.length === 1 ? "convidado" : "convidados"}`
    );
    meta.appendChild(people);
  }
  card.appendChild(meta);

  const actions = document.createElement("div");
  actions.className = "next-actions";
  const record = document.createElement("button");
  record.className = "btn cta big";
  record.append(icon("mic", 16), "Gravar esta reunião");
  record.disabled = busy;
  record.title = busy ? "Já tem uma reunião em andamento" : "Nome e participantes vêm da agenda";
  record.addEventListener("click", () => onStartFromEvent(event.id));
  actions.appendChild(record);

  if (event.conference_url) {
    const join = document.createElement("button");
    join.className = "btn outline big";
    join.textContent = "Entrar na chamada";
    join.title = `Abrir no ${conferenceName(event.conference_url)}`;
    join.addEventListener("click", () => onOpenUrl(event.conference_url));
    actions.appendChild(join);
  }
  card.appendChild(actions);
  return card;
}

/** Um pedaço da linha de detalhes do cartão: ícone, texto e, se houver, o
    complemento que só cabe no title. */
function metaBit(name, text, detail) {
  const bit = document.createElement("span");
  bit.className = "next-bit";
  bit.append(icon(name, 15), text);
  if (detail) bit.title = detail;
  return bit;
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

/** Linha compacta do resto do dia. `dim` deixa o dia seguinte mais apagado:
    ele é informação, não o que se vai gravar agora. */
function agendaRow(event, dim) {
  const busy = Boolean(state.active);
  const row = document.createElement("div");
  row.className = "agenda-row";
  if (dim) row.classList.add("dim");

  const when = document.createElement("div");
  when.className = "agenda-hour";
  when.textContent = event.end
    ? `${timeLabel(event.start)}–${timeLabel(event.end)}`
    : timeLabel(event.start) || "—";
  const planned = plannedLabel(event);
  if (planned) when.title = `${planned} prevista`;

  const mid = document.createElement("div");
  mid.className = "grow";
  const title = document.createElement("div");
  title.className = "agenda-title";
  title.textContent = event.title || "(sem título)";
  mid.appendChild(title);

  const guests = (event.attendees || []).filter((person) => !person.self);
  const bits = [];
  if (event.location) bits.push(event.location);
  const repeat = recurrenceLabel(event);
  if (repeat) bits.push(repeat);
  if (bits.length) {
    const sub = document.createElement("div");
    sub.className = "agenda-loc";
    sub.textContent = bits.join(" · ");
    mid.appendChild(sub);
  }

  const people = document.createElement("span");
  people.className = "agenda-people";
  const count = guests.length + 1;
  people.textContent = guests.length ? `${count} pessoas` : "";
  people.title = guests.map((person) => person.name || person.email).join(", ");

  const go = document.createElement("button");
  go.className = "btn outline small reveal";
  go.textContent = "Gravar";
  go.disabled = busy;
  go.title = busy ? "Já tem uma reunião em andamento" : `Iniciar gravação: ${event.title || "evento"}`;
  go.addEventListener("click", () => onStartFromEvent(event.id));

  row.append(when, mid, people, go);
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
  const first = ((state.profile && state.profile.name) || "").trim().split(/\s+/)[0] || "";
  el("ws-name").textContent = first || "Fireball";
  el("ws-avatar").textContent = initialsOf(first || "Fireball").slice(0, 1);
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
  } catch (err) {
    setBanner(errText(err));
    return;
  }

  // Pasta de dados fora do ar (disco desmontado) é aviso de tela inteira, não
  // nota de configuração: com ela ausente o Fireball *recusa* gravar, e quem
  // está na home precisa saber disso antes de clicar em iniciar.
  const dados = status.data_dir;
  setBanner(
    dados && dados.disponivel === false
      ? `O disco das gravações não está montado (${dados.path}) — nada será gravado até ele voltar.`
      : ""
  );

  // versão e última conferência de atualização viajam no mesmo status: o daemon
  // cacheia a conferência (6h), então este poll de 2s não abre conexão nenhuma
  state.version = status.version || state.version;
  state.appimage = status.appimage || "";
  if (status.update) state.update = status.update;
  renderUpdateNotice();
  if (state.view === "settings") renderUpdate();

  // Resumo rodando em alguma reunião significa nome e tags prestes a mudar —
  // e é a lista, não a reunião aberta, que mostra os dois para todas elas.
  if (state.rows.some((row) => row.summary_status === "running")) loadHistory();

  const detail = status.active;
  const wasActive = state.active && state.active.id;
  state.active = detail ? detail.meeting : null;
  renderLiveCard();
  pintarEstadoLocal();
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

  el("bv-title").textContent = week ? weekTitle(b.weekStart) : "Acervo";
  el("bv-modes").dataset.mode = b.mode;
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
    ? "Blocos na posição e no tamanho reais da gravação"
    : "Ctrl+K vai para a busca";

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

    // duas tags e a contagem do resto: a coluna empilhava todas e cada linha
    // ficava da altura das tags dela
    const tags = document.createElement("div");
    tags.className = "mt-tags";
    const allTags = row.tags || [];
    for (const tag of allTags.slice(0, 2)) {
      const chip = document.createElement("span");
      chip.className = "tag";
      if (state.browse.tags.includes(tag)) chip.classList.add("on");
      chip.textContent = tag;
      tags.appendChild(chip);
    }
    if (allTags.length > 2) {
      const more = document.createElement("span");
      more.className = "tag more";
      more.textContent = `+${allTags.length - 2}`;
      more.title = allTags.slice(2).join(", ");
      tags.appendChild(more);
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

/* A nota é UMA superfície: o texto que se lê é o texto que se edita.

   Antes havia duas — o markdown renderizado (`#notes-view`) e um textarea que
   aparecia no clique — e a troca de um pelo outro no meio da escrita era o que
   mais atrapalhava. Agora é um `contenteditable` só: clicar em qualquer lugar
   é editar, não trocar de modo.

   O arquivo continua **markdown**, porque é o que a CLI, o Claude e o prompt do
   resumo leem. Então a conversão acontece nas duas pontas do documento:
   `renderNoteDoc` traz markdown para o DOM editável na abertura, `noteMarkdown`
   devolve o DOM para markdown no salvamento. É o mesmo subconjunto nos dois
   sentidos (título, parágrafo, lista, citação, negrito, itálico, código,
   link) — e por isso a nota não muda de forma entre abrir e salvar. */

async function loadNotes() {
  const o = state.open;
  if (!o || state.notes.loadedFor === o.id) return;
  const box = el("notes");
  try {
    const text = await api("notes", o.id);
    if (state.open !== o) return;
    renderNoteDoc(text, box);
    state.notes.loadedFor = o.id;
    // o que está salvo é o markdown do que a tela mostra: comparar com o
    // arquivo cru daria "alterado" a cada abertura por causa de espaço em branco
    state.notes.saved = noteMarkdown(box);
    atualizarNotaVazia();
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
  const box = el("notes");
  if (!box) return;
  const text = noteMarkdown(box);
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
  atualizarNotaVazia();
  setNotesStatus("editando…");
  clearTimeout(state.notes.timer);
  state.notes.timer = setTimeout(flushNotes, NOTES_SAVE_MS);
}

/** O documento está sem nada escrito? Decide o placeholder (CSS, `.vazia`). */
function notaVazia() {
  const box = el("notes");
  if (!box) return true;
  if (box.querySelector("ul, ol, pre, blockquote, h1, h2, h3, a, img")) return false;
  return !box.textContent.replace(/[\u200b\s]/g, "");
}

function atualizarNotaVazia() {
  const box = el("notes");
  if (box) box.classList.toggle("vazia", notaVazia());
}

/* A barra do editor formata **o documento**, não injeta markdown: o que você
   vê é o que vai ser salvo. No salvamento, `noteMarkdown` transforma o negrito
   do DOM em `**negrito**`. `styleWithCSS=false` faz o Chromium escrever
   `<b>`/`<i>` em vez de `<span style>`, que é o que o serializador entende. */
function applyMarkdown(kind) {
  const box = el("notes");
  if (!box) return;
  box.focus();
  document.execCommand("styleWithCSS", false, false);
  switch (kind) {
    case "bold":
      document.execCommand("bold");
      break;
    case "italic":
      document.execCommand("italic");
      break;
    case "code":
      envolverNaSelecao("code");
      break;
    case "h2":
      document.execCommand("formatBlock", false, "h2");
      break;
    case "quote":
      document.execCommand("formatBlock", false, "blockquote");
      break;
    case "ul":
      document.execCommand("insertUnorderedList");
      break;
    case "ol":
      document.execCommand("insertOrderedList");
      break;
    case "task":
      document.execCommand("insertText", false, "- [ ] ");
      break;
    default:
      return;
  }
  desembrulharBlocos(box);
  onNotesInput();
}

/** O execCommand de lista às vezes escreve `<p><ul><li>…</li></ul></p>`: o
    parágrafo em volta é inválido e sobra na serialização. Aqui ele é aberto.
    O mesmo vale para `<span style>`, que o editor deixa ao desligar a lista —
    formatação de verdade vem em `<b>`/`<i>`, não em estilo inline. */
function desembrulharBlocos(box) {
  for (const p of [...box.querySelectorAll("p")]) {
    if (!p.querySelector("ul, ol, pre, blockquote, h1, h2, h3")) continue;
    p.replaceWith(...p.childNodes);
  }
  for (const span of [...box.querySelectorAll("span[style]")]) {
    span.replaceWith(...span.childNodes);
  }
}

/** Envolve a seleção num elemento — o execCommand não tem "code". */
function envolverNaSelecao(tag) {
  const sel = window.getSelection();
  const texto = sel ? sel.toString() : "";
  const node = document.createElement(tag);
  node.textContent = texto || "\u200b";
  document.execCommand("insertHTML", false, node.outerHTML);
}

// ---- markdown ↔ documento editável

/** Links markdown viram âncora; sem href, ficam como texto (âncora sem
    endereço não teria como ser corrigida dentro do documento). */
function notaInlineMarkdown(text, parent) {
  const padrao = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*|(?<![\w])_[^_]+_(?![\w])|\[[^\]]+\]\([^)\s]+\))/g;
  let ultimo = 0;
  let achado;
  while ((achado = padrao.exec(text)) !== null) {
    if (achado.index > ultimo) {
      parent.appendChild(document.createTextNode(text.slice(ultimo, achado.index)));
    }
    const token = achado[0];
    let node;
    if (token.startsWith("**")) {
      node = document.createElement("strong");
      node.textContent = token.slice(2, -2);
    } else if (token.startsWith("`")) {
      node = document.createElement("code");
      node.textContent = token.slice(1, -1);
    } else if (token.startsWith("[")) {
      const partes = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
      node = document.createElement("a");
      node.textContent = partes[1];
      node.setAttribute("href", partes[2]);
      node.setAttribute("target", "_blank");
    } else {
      node = document.createElement("em");
      node.textContent = token.slice(1, -1);
    }
    parent.appendChild(node);
    ultimo = achado.index + token.length;
  }
  if (ultimo < text.length) parent.appendChild(document.createTextNode(text.slice(ultimo)));
}

/** Markdown → DOM editável. Mesmas regras de `renderMarkdown` (o do resumo),
    sem as marcas de proveniência: `[12]` e `==` são coisas do resumo, e na nota
    viram botão/lupa impossíveis de editar. */
function renderNoteDoc(markdown, box) {
  box.innerHTML = "";
  let lista = null;
  let codigo = null;
  const fecharLista = () => {
    lista = null;
  };

  for (const bruto of String(markdown || "").split("\n")) {
    const linha = bruto.trimEnd();
    if (codigo) {
      if (/^```/.test(linha)) {
        const pre = document.createElement("pre");
        pre.textContent = codigo.join("\n");
        box.appendChild(pre);
        codigo = null;
      } else {
        codigo.push(bruto);
      }
      continue;
    }
    if (/^```/.test(linha)) {
      fecharLista();
      codigo = [];
      continue;
    }
    if (!linha.trim()) {
      fecharLista();
      continue;
    }

    const titulo = /^(#{1,3})\s+(.*)$/.exec(linha);
    const item = /^\s*[-*]\s+(.*)$/.exec(linha);
    const numerado = /^\s*\d+[.)]\s+(.*)$/.exec(linha);
    const citacao = /^>\s?(.*)$/.exec(linha);

    if (titulo) {
      fecharLista();
      const node = document.createElement(`h${Math.min(3, titulo[1].length)}`);
      notaInlineMarkdown(titulo[2], node);
      box.appendChild(node);
      continue;
    }
    if (item || numerado) {
      const alvo = item ? "UL" : "OL";
      if (!lista || lista.tagName !== alvo) {
        lista = document.createElement(item ? "ul" : "ol");
        box.appendChild(lista);
      }
      const li = document.createElement("li");
      notaInlineMarkdown((item || numerado)[1], li);
      lista.appendChild(li);
      continue;
    }
    fecharLista();
    if (citacao) {
      const node = document.createElement("blockquote");
      notaInlineMarkdown(citacao[1], node);
      box.appendChild(node);
      continue;
    }
    const para = document.createElement("p");
    notaInlineMarkdown(linha, para);
    box.appendChild(para);
  }
  if (codigo) {
    const pre = document.createElement("pre");
    pre.textContent = codigo.join("\n");
    box.appendChild(pre);
  }
}

/** Texto de um nó inline do documento, na forma markdown. */
function notaInline(node) {
  if (node.nodeType === Node.TEXT_NODE) {
    return (node.nodeValue || "").replace(/\u00a0/g, " ").replace(/\u200b/g, "");
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  const dentro = [...node.childNodes].map(notaInline).join("");
  switch (node.tagName) {
    case "BR":
      return "\n";
    case "B":
    case "STRONG":
      return dentro.trim() ? `**${dentro.trim()}**` : dentro;
    case "I":
    case "EM":
      return dentro.trim() ? `*${dentro.trim()}*` : dentro;
    case "CODE":
      return dentro ? `\`${dentro}\`` : "";
    case "A":
      return `[${dentro}](${node.getAttribute("href") || ""})`;
    default:
      return dentro;
  }
}

/** DOM editável → markdown. Cada bloco vira um parágrafo de markdown separado
    por linha em branco, o que é o que `renderNoteDoc` espera de volta. */
function noteMarkdown(root) {
  const blocos = [];
  const guardar = (texto) => {
    if (texto) blocos.push(texto);
  };
  // o navegador às vezes aninha um bloco dentro de um parágrafo
  // (`<p><ul>…`, que o execCommand de lista produz): aí quem manda são os
  // blocos de dentro, e não o texto do parágrafo
  const temBlocoDentro = (node) =>
    Boolean(node.querySelector && node.querySelector("ul, ol, pre, blockquote, h1, h2, h3"));

  const bloco = (node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      // texto solto direto no contenteditable (acontece ao digitar sem bloco)
      guardar(notaInline(node).trim());
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;

    const tag = node.tagName;
    if (tag === "H1" || tag === "H2" || tag === "H3") {
      guardar(`${"#".repeat(Number(tag[1]))} ${notaInline(node).trim()}`);
      return;
    }
    if (tag === "UL" || tag === "OL") {
      let n = 0;
      const itens = [];
      for (const li of node.children) {
        if (li.tagName !== "LI") continue;
        n += 1;
        itens.push(`${tag === "OL" ? `${n}. ` : "- "}${notaInline(li).trim()}`);
      }
      guardar(itens.join("\n"));
      return;
    }
    if (tag === "BLOCKQUOTE" && !temBlocoDentro(node)) {
      guardar(
        notaInline(node)
          .trim()
          .split("\n")
          .map((linha) => `> ${linha}`)
          .join("\n")
      );
      return;
    }
    if (tag === "PRE") {
      const texto = node.textContent.replace(/\n$/, "");
      if (texto.trim()) guardar(`\`\`\`\n${texto}\n\`\`\``);
      return;
    }
    if (temBlocoDentro(node)) {
      for (const filho of node.childNodes) bloco(filho);
      return;
    }
    // P, DIV (que o Chromium cria ao apertar Enter) e o que mais aparecer:
    // um bloco por linha, com as quebras internas preservadas
    guardar(notaInline(node).trim());
  };

  for (const node of root.childNodes) bloco(node);

  return blocos
    .join("\n\n")
    .replace(/[ \t]+$/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
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
    audio.pause();
    return;
  }

  if (!playerReady()) {
    bar.classList.add("hidden");
    none.classList.remove("hidden");
    audio.pause();
    // o .wav sai quando a gravação termina (mistura de mic + sistema, ou só
    // mic quando o monitor do sistema não abriu); não havendo nenhum dos
    // dois, não há o que tocar.
    el("player-none-note").textContent = "Esta reunião não deixou áudio gravado.";
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
  const paused = el("audio").paused;
  const button = el("play-btn");
  button.classList.toggle("is-playing", !paused);
  button.title = paused ? "Tocar" : "Pausar";
  button.setAttribute("aria-label", button.title);
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
  // ==texto== = veio das notas de quem gravou | [12] = fala que sustenta o ponto
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|==[^=]+==|\[\d+\])/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
    const token = match[0];
    if (/^\[\d+\]$/.test(token)) {
      const marca = document.createElement("button");
      marca.className = "citacao";
      marca.type = "button";
      marca.dataset.seq = token.slice(1, -1);
      marca.textContent = "⌕";
      marca.title = "Ver a fala que sustenta este ponto";
      parent.appendChild(marca);
      last = match.index + token.length;
      continue;
    }
    if (token.startsWith("==")) {
      const marca = document.createElement("mark");
      marca.className = "de-notas";
      marca.textContent = token.slice(2, -2);
      parent.appendChild(marca);
      last = match.index + token.length;
      continue;
    }
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
  // o ícone gira enquanto o resumo sai; o rótulo diz se é o primeiro ou mais um
  button.replaceChildren(icon("refresh", 13), running ? "Gerando…" : list.length ? "Regerar" : "Gerar");
  button.firstChild.classList.toggle("spin", running);
  button.title = running
    ? "Gerando o resumo…"
    : list.length
      ? "Gerar outro resumo com o prompt escolhido"
      : "Gerar o resumo com o prompt escolhido";

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
    // lista vazia não escreve nada: o estado vazio mora no corpo do resumo,
    // senão a mesma frase aparece duas vezes na mesma dobra
    box.innerHTML = "";
  }
  // com um resumo só, o chip repetiria o nome do resumo que já está logo abaixo
  box.classList.toggle("single", !running && list.length <= 1);
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

  // Sem preferência salva, a vista segue a reunião: com resumo, resumo; sem
  // resumo, as notas (é onde se escreve). Quando o resumo termina de gerar, a
  // nota vira o resumo sozinha — e o clique em qualquer aba salva a preferência,
  // que a partir daí manda.
  let salvo = null;
  try {
    salvo = localStorage.getItem(VISTA_KEY);
  } catch (err) {
    salvo = null;
  }
  if (salvo !== "cru" && salvo !== "doc") {
    const quer = current ? "doc" : "cru";
    const esta = el("nota-corpo").classList.contains("cru") ? "cru" : "doc";
    if (quer !== esta) vistaNota(quer, false);
  }
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
  title.textContent = "Sem resumo ainda";
  const note = document.createElement("div");
  note.className = "slab-note";
  note.textContent =
    "Gerar, na linha das abas, usa o prompt escolhido ao lado. Cada prompt " +
    "guarda o resumo dele; o nome só é trocado se ninguém tiver dado um à mão.";
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
  const limites = [];
  if (s.aviso_silencio_minutos) limites.push(`${s.aviso_silencio_minutos}min sem falar`);
  if (s.aviso_minutos) limites.push(`${s.aviso_minutos}min gravando`);
  const avisos = limites.length ? `avisos: ${limites.join(" / ")}` : "sem avisos";
  el("config-summary-text").textContent =
    `${live} · ${final} · ${diarize} · ${s.language} · ${avisos}`;
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
  for (const person of voiceCandidates(o)) {
    const option = new Option(
      person.profileId
        ? person.guest ? `${person.name} — voz já cadastrada, convidado` : `${person.name} — voz já cadastrada`
        : `${person.name} — convidado`,
      person.key,
    );
    option.dataset.name = person.name;
    option.dataset.email = person.email;
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

/** Quem pode ser esta voz: as vozes já cadastradas e os convidados do evento.

    O convidado que já tem voz cadastrada entra uma vez só, apontando para o
    perfil: escolhê-lo como "convidado" criaria uma segunda voz da mesma
    pessoa. */
function voiceCandidates(o) {
  const attendees = (o.meeting.event && o.meeting.event.attendees) || [];
  const guests = new Map();
  for (const [index, person] of attendees.entries()) {
    const name = (person.name || person.email || "").trim();
    if (!name) continue;
    const email = (person.email || "").toLowerCase();
    const voice = state.voices.find(
      (item) =>
        (email && (item.emails || []).some((one) => one.toLowerCase() === email)) ||
        item.name.trim().toLowerCase() === name.toLowerCase(),
    );
    if (voice) guests.set(voice.id, person);
    else guests.set(`attendee:${index}`, person);
  }
  const list = [];
  for (const voice of state.voices) {
    const guest = guests.get(voice.id);
    list.push({
      key: `profile:${voice.id}`,
      profileId: voice.id,
      name: voice.name,
      email: (voice.emails && voice.emails[0]) || (guest && guest.email) || "",
      guest: Boolean(guest),
    });
  }
  for (const [key, person] of guests) {
    if (!String(key).startsWith("attendee:")) continue;
    list.push({ key, profileId: "", name: person.name || person.email, email: person.email || "", guest: true });
  }
  return list;
}

// --------------------------------------------- trocar a voz pela transcrição

/* O nome de quem fala, na transcrição, abre um menu com as pessoas possíveis.
   Escolher uma cadastra a voz daquele locutor: o nome vale para as falas que
   já estão na tela e para as próximas dele, ao vivo inclusive. Quem não está
   na lista vai para o diálogo completo, onde se digita nome e e-mail. */
const voiceMenu = { node: null, anchor: null };

function closeVoiceMenu() {
  if (voiceMenu.node) voiceMenu.node.remove();
  if (voiceMenu.anchor) voiceMenu.anchor.setAttribute("aria-expanded", "false");
  voiceMenu.node = null;
  voiceMenu.anchor = null;
  document.removeEventListener("pointerdown", onVoiceMenuOutside, true);
  document.removeEventListener("keydown", onVoiceMenuKey, true);
}

function onVoiceMenuOutside(event) {
  if (voiceMenu.node && !voiceMenu.node.contains(event.target) && event.target !== voiceMenu.anchor) {
    closeVoiceMenu();
  }
}

function onVoiceMenuKey(event) {
  if (event.key === "Escape") {
    const anchor = voiceMenu.anchor;
    closeVoiceMenu();
    if (anchor) anchor.focus();
  }
}

async function openVoiceMenu(seg, anchor) {
  const o = state.open;
  if (!o || !seg.speaker_key || o.meeting.status === "finalizing") return;
  if (voiceMenu.anchor === anchor) {
    closeVoiceMenu();
    return;
  }
  closeVoiceMenu();
  try {
    await loadVoices();
  } catch (err) {
    setAlert("meeting-error", "Não foi possível carregar as vozes", errText(err));
    return;
  }
  if (state.open !== o) return;

  const menu = document.createElement("div");
  menu.className = "voice-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Quem é esta voz");

  const head = document.createElement("div");
  head.className = "vm-head";
  const slot = seg.speaker_key.startsWith("mic:") ? "Sala" : "Remoto";
  const slotName = `${slot} ${Number(seg.speaker_key.split(":")[1]) + 1}`;
  const tag = document.createElement("span");
  tag.className = "vm-slot";
  tag.textContent = slotName;
  head.append("Quem está falando?", tag);
  menu.appendChild(head);

  const current = seg.voice_profile_id;
  // quem foi convidado para esta reunião vem antes do resto das vozes cadastradas
  const people = voiceCandidates(o).sort((a, b) => Number(b.guest) - Number(a.guest));
  const list = document.createElement("div");
  list.className = "vm-list";
  if (people.length > 8) {
    const find = document.createElement("input");
    find.className = "vm-find";
    find.type = "text";
    find.placeholder = "Filtrar pessoas…";
    find.setAttribute("aria-label", "Filtrar pessoas");
    find.addEventListener("input", () => {
      const needle = find.value.trim().toLowerCase();
      for (const item of list.children) {
        item.classList.toggle("hidden", Boolean(needle) && !item.dataset.name.includes(needle));
      }
    });
    find.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      const first = [...list.children].find((item) => !item.classList.contains("hidden"));
      if (first) first.click();
    });
    menu.appendChild(find);
  }
  menu.appendChild(list);
  for (const person of people) {
    const item = document.createElement("button");
    item.className = "vm-opt";
    item.setAttribute("role", "menuitemradio");
    const chosen = Boolean(current && person.profileId === current);
    item.setAttribute("aria-checked", chosen ? "true" : "false");
    const face = faceOf(person.name);
    face.style.color = speakerColor(person.name);
    const label = document.createElement("span");
    label.className = "vm-name";
    label.textContent = person.name;
    item.append(face, label);
    if (!person.profileId) {
      const hint = document.createElement("span");
      hint.className = "vm-hint";
      hint.textContent = "convidado";
      item.appendChild(hint);
    }
    if (chosen) {
      const mark = icon("check", 14);
      mark.classList.add("vm-check");
      item.appendChild(mark);
    }
    item.dataset.name = person.name.toLowerCase();
    item.addEventListener("click", () => assignVoice(seg, person));
    list.appendChild(item);
  }
  if (!people.length) {
    const empty = document.createElement("div");
    empty.className = "vm-empty";
    empty.textContent = "Nenhuma voz cadastrada nem convidado no evento.";
    menu.appendChild(empty);
  }

  const other = document.createElement("button");
  other.className = "vm-opt vm-other";
  other.setAttribute("role", "menuitem");
  const plus = document.createElement("span");
  plus.className = "vm-plus";
  plus.appendChild(icon("plus", 11));
  other.append(plus, "Outra pessoa…");
  other.addEventListener("click", () => {
    closeVoiceMenu();
    openVoiceDialog(seg);
  });
  menu.appendChild(other);

  const note = document.createElement("div");
  note.className = "vm-note";
  note.textContent = o.live
    ? "Vale para as falas que já vieram e para as próximas desta voz."
    : "Vale para todas as falas desta voz nesta reunião.";
  const privacy = document.createElement("div");
  privacy.className = "vm-note dim";
  privacy.textContent = "O Fireball guarda só a assinatura da voz, nunca o áudio.";
  menu.append(note, privacy);

  document.body.appendChild(menu);
  // o menu vive no body, em posição fixa: dentro do painel ele seria cortado
  // pela rolagem da transcrição nas últimas falas
  const box = anchor.getBoundingClientRect();
  const top = Math.min(box.bottom + 6, window.innerHeight - menu.offsetHeight - 12);
  const left = Math.min(box.left - 6, window.innerWidth - menu.offsetWidth - 12);
  menu.style.top = `${Math.max(12, top)}px`;
  menu.style.left = `${Math.max(12, left)}px`;

  voiceMenu.node = menu;
  voiceMenu.anchor = anchor;
  anchor.setAttribute("aria-expanded", "true");
  document.addEventListener("pointerdown", onVoiceMenuOutside, true);
  document.addEventListener("keydown", onVoiceMenuKey, true);
  const first = menu.querySelector(".vm-find, .vm-opt");
  if (first) first.focus();
}

async function assignVoice(seg, person) {
  const o = state.open;
  if (!o) return;
  const slot = seg.speaker;
  closeVoiceMenu();
  try {
    await api("enroll_voice", o.id, seg.speaker_key, person.name, person.email, person.profileId);
  } catch (err) {
    setAlert("meeting-error", "Não foi possível cadastrar esta voz", errText(err));
    return;
  }
  await afterVoiceSaved(o, seg.speaker_key);
  voiceToast(
    o.live
      ? `${slot} agora é ${person.name} — e o que vier dessa voz também`
      : `${slot} agora é ${person.name} em todas as falas dessa voz`
  );
}

/** Depois de cadastrar: o nome vale para as falas já na tela, então o chat
    recomeça do zero; fora do ar vivo o daemon também regera o resumo. */
async function afterVoiceSaved(o, speakerKey) {
  await loadVoices();
  resetChat();
  await pollChat();
  for (const node of el("chat").querySelectorAll(".msg")) {
    if (node.dataset.key === speakerKey) node.classList.add("flash");
  }
  if (!o.live) {
    o.settling = SETTLE_TICKS;
    await refreshOpenMeeting();
    if (state.tab === "resumo") await loadSummary();
  }
}

function voiceToast(text) {
  const old = document.querySelector(".voice-toast");
  if (old) old.remove();
  const toast = document.createElement("div");
  toast.className = "voice-toast";
  toast.setAttribute("role", "status");
  const mark = icon("check", 15);
  mark.classList.add("draw");
  const label = document.createElement("span");
  label.textContent = text;
  const bar = document.createElement("span");
  bar.className = "toast-bar";
  toast.append(mark, label, bar);
  el("painel-tr").appendChild(toast);
  setTimeout(() => toast.remove(), 3600);
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
    await afterVoiceSaved(o, seg.speaker_key);
    voiceToast(`${seg.speaker} agora é ${el("voice-name").value.trim()}`);
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
  el("set-aviso-silencio-minutos").value = s.aviso_silencio_minutos;
  el("set-aviso-minutos").value = s.aviso_minutos;
  el("set-openai-url").value = s.openai_base_url || "";
  el("set-openai-key").value = s.openai_api_key || "";
  el("set-openai-model").value = s.openai_model || "";
  el("set-groq-key").value = s.groq_api_key || "";
  el("set-gog-account").value = s.gog_account || "";
  el("set-language").value = s.language;
  el("set-data-dir").value = s.data_dir || "";
  fillSelect("set-my-voice", myVoiceOptions(), s.my_voice || "");
  renderProviderFields();
  renderFinalFields();
  renderUpdate();
  state.settingsBase = settingsSnapshot();
  markSettings();
}

// Os campos que o Salvar manda: são eles que dizem se há o que salvar.
const SETTINGS_FIELDS = [
  "set-live-on", "set-live-backend", "set-final-on", "set-final-backend", "set-diarize-default",
  "set-groq-key", "set-language", "set-summary-provider", "set-openai-url", "set-openai-key",
  "set-openai-model", "set-auto-summarize", "set-aviso-silencio-minutos", "set-aviso-minutos",
  "set-summary-prompt", "set-calendar-provider", "set-gog-account", "set-my-voice", "set-check-updates",
  "set-data-dir",
];

function settingsSnapshot() {
  const snap = {};
  for (const id of SETTINGS_FIELDS) {
    const node = el(id);
    snap[id] = node.type === "checkbox" ? node.checked : node.value;
  }
  return snap;
}

/** A barra de baixo conta o que mudou desde o último salvar, e as linhas que
    dependem de um interruptor desligado esmaecem junto com ele. */
function markSettings() {
  for (const row of document.querySelectorAll("#view-settings [data-depends]")) {
    const on = el(row.dataset.depends).checked;
    row.classList.toggle("off", !on);
    for (const field of row.querySelectorAll("select, input")) field.disabled = !on;
  }
  const base = state.settingsBase;
  if (!base) return;
  const now = settingsSnapshot();
  const dirty = SETTINGS_FIELDS.filter((id) => base[id] !== now[id]).length;
  const bar = el("save-bar");
  bar.classList.toggle("dirty", dirty > 0);
  if (dirty) bar.classList.remove("saved");
  el("settings-save").disabled = !dirty;
  el("settings-discard").disabled = !dirty;
  if (dirty) {
    el("settings-note").textContent =
      dirty === 1 ? "1 alteração não salva" : `${dirty} alterações não salvas`;
  } else if (!bar.classList.contains("saved")) {
    el("settings-note").textContent = "Tudo salvo.";
  }
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

const SUMMARY_PROVIDER_NOTE = {
  claude_code: "Usa o Claude Code da máquina: claude no PATH, com login feito. Não pede chave.",
  openai_api:
    "Qualquer API compatível com OpenAI — OpenAI, OpenRouter, Groq, Ollama, llama.cpp, LM Studio.",
};

function renderProviderFields() {
  const summary = el("set-summary-provider").value;
  el("openai-fields").classList.toggle("hidden", summary !== "openai_api");
  el("groq-fields").classList.toggle("hidden", el("set-final-backend").value !== "groq");
  el("gog-fields").classList.toggle("hidden", el("set-calendar-provider").value !== "gog");
  el("summary-provider-note").textContent = SUMMARY_PROVIDER_NOTE[summary] || "";
}

/* ------------------------------------------------------- atualização

   O daemon é quem confere (TTL de 6h, chave em settings.json) e quem troca o
   AppImage; a janela só desenha o que vem em `get_status` e manda os dois
   cliques. Nada de fetch para o GitHub daqui: a página roda em http local e o
   daemon é o dono do estado — inclusive de saber se há reunião gravando. */

function updateInfo() {
  return state.update && typeof state.update === "object" ? state.update : null;
}

/** O changelog da versão, do corpo da release que o daemon já trouxe.

    Renderizador próprio, e não o `renderMarkdown` da nota: aquele trata `[12]`
    como lupa de citação e `==texto==` como marca das notas da reunião — num
    changelog isso viraria botão que não leva a lugar nenhum. Aqui vale o
    subconjunto que release usa de verdade: título, lista, parágrafo, cerca de
    código, negrito e código inline. */
function renderUpdateNotes() {
  const wrap = el("update-notes-wrap");
  const box = el("update-notes");
  const info = updateInfo();
  const texto = (info && !info.error && info.notes) || "";
  wrap.classList.toggle("hidden", !texto);
  box.innerHTML = "";
  if (!texto) return;

  // Abre sozinho quando é isto que você vai instalar; recolhido quando é só o
  // registro da versão que já está rodando. A chave evita que o poll de 2s
  // reabra o bloco que a pessoa acabou de fechar.
  const chave = `${info.latest || ""}|${info.current || ""}|${info.applied_at || ""}`;
  if (chave !== state.updateNotesKey) {
    state.updateNotesKey = chave;
    wrap.open = Boolean(info.newer);
  }

  el("update-notes-title").textContent = info.applied_at
    ? `O que veio na ${info.current}`
    : info.newer
      ? `Novidades da ${info.latest}`
      : `O que tem na ${info.current}`;

  const inline = (text, parent) => {
    for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
      if (!part) continue;
      if (part.startsWith("**") && part.endsWith("**")) {
        const node = document.createElement("strong");
        node.textContent = part.slice(2, -2);
        parent.appendChild(node);
      } else if (part.startsWith("`") && part.endsWith("`")) {
        const node = document.createElement("code");
        node.textContent = part.slice(1, -1);
        parent.appendChild(node);
      } else {
        parent.appendChild(document.createTextNode(part));
      }
    }
  };

  let lista = null;
  let cerca = null;
  for (const linha of texto.split("\n")) {
    if (/^\s*```/.test(linha)) {
      lista = null;
      if (cerca) {
        cerca = null;
      } else {
        cerca = document.createElement("pre");
        box.appendChild(cerca);
      }
      continue;
    }
    if (cerca) {
      cerca.textContent += (cerca.textContent ? "\n" : "") + linha;
      continue;
    }
    if (!linha.trim()) {
      lista = null;
      continue;
    }
    const titulo = /^#{1,6}\s+(.*)$/.exec(linha);
    if (titulo) {
      lista = null;
      const node = document.createElement("h4");
      inline(titulo[1], node);
      box.appendChild(node);
      continue;
    }
    const item = /^\s*[-*]\s+(.*)$/.exec(linha);
    if (item) {
      if (!lista) {
        lista = document.createElement("ul");
        box.appendChild(lista);
      }
      const li = document.createElement("li");
      inline(item[1], li);
      lista.appendChild(li);
      continue;
    }
    lista = null;
    const paragrafo = document.createElement("p");
    inline(linha, paragrafo);
    box.appendChild(paragrafo);
  }
}

/** O aviso da barra lateral: só existe quando há versão nova de verdade. */
function renderUpdateNotice() {
  const card = el("update-card");
  if (!card) return;
  const info = updateInfo();
  const novo = Boolean(info && info.newer && !info.error && !info.applied_at);
  card.classList.toggle("idle", !novo);
  const badge = el("set-update-badge");
  badge.classList.toggle("hidden", !novo);
  if (!novo) return;
  badge.textContent = info.latest;
  el("update-card-title").textContent = `Fireball ${info.latest} disponível`;
  el("update-card-note").textContent = `Você está na ${info.current} — clique para atualizar`;
}

function renderUpdate() {
  const info = updateInfo();
  const aplicar = el("update-apply");
  el("app-version").textContent = state.version || (info && info.current) || "—";
  el("app-channel").textContent = state.appimage
    ? ` · AppImage em ${state.appimage}`
    : " · rodando do código-fonte (para atualizar, git pull)";
  if (state.settings) el("set-check-updates").checked = Boolean(state.settings.check_updates);
  renderUpdateNotes();

  if (!info) {
    el("update-state").textContent = "Ainda não conferi a release mais nova.";
    aplicar.disabled = true;
    return;
  }
  if (info.error) {
    el("update-state").textContent = `A última conferência falhou: ${info.error}`;
    aplicar.disabled = true;
    return;
  }
  const quando = info.checked_at ? `, conferido às ${timeLabel(info.checked_at)}` : "";
  if (info.applied_at) {
    el("update-state").textContent = `Atualizado para a ${info.current}${quando} — o Fireball reiniciou na versão nova.`;
    aplicar.disabled = true;
    return;
  }
  el("update-state").textContent = info.newer
    ? `Publicada a ${info.latest}; você está na ${info.current}${quando}.`
    : `Você está na versão mais nova (${info.current}${quando}).`;
  // sem AppImage não existe arquivo para trocar: o botão fica morto em vez de
  // prometer uma atualização que terminaria em erro
  aplicar.disabled = !(info.newer && state.appimage);
  aplicar.textContent = "Baixar e atualizar";
}

async function onUpdateCheck() {
  const button = el("update-check");
  button.disabled = true;
  el("update-note").textContent = "";
  setAlert("settings-error", "");
  try {
    state.update = await api("update_check", true);
    renderUpdate();
    renderUpdateNotice();
  } catch (err) {
    setAlert("settings-error", "Não consegui conferir a atualização", errText(err));
  } finally {
    button.disabled = false;
  }
}

/** Trocar o AppImage é em dois toques, como excluir: o segundo clique no mesmo
    botão confirma, e o primeiro desiste sozinho se ninguém confirmar. */
function resetUpdateApply() {
  const button = el("update-apply");
  button.classList.remove("confirming");
  button.textContent = "Baixar e atualizar";
  button.disabled = false;
  clearTimeout(state.updateTimer);
  state.updateTimer = null;
  el("update-note").textContent = "";
}

async function onUpdateApply() {
  const button = el("update-apply");
  if (!button.classList.contains("confirming")) {
    button.classList.add("confirming");
    button.textContent = "Confirmar atualização";
    el("update-note").textContent = "Baixa o AppImage novo e reinicia o Fireball.";
    state.updateTimer = setTimeout(resetUpdateApply, 10000);
    return;
  }
  clearTimeout(state.updateTimer);
  state.updateTimer = null;
  button.disabled = true;
  button.textContent = "Baixando…";
  el("update-note").textContent = "Baixando o AppImage novo…";
  setAlert("settings-error", "");
  try {
    const res = await api("update_apply");
    el("update-note").textContent = res.updated
      ? `Trocado para a ${res.version} — o Fireball está reiniciando.`
      : res.reason || "Já está na versão mais nova.";
    // o daemon sai para o AppImage trocado valer, e aí não há o que restaurar
    // na tela: ela volta junto com o daemon novo, na versão nova
    if (!res.restarting) resetUpdateApply();
  } catch (err) {
    resetUpdateApply();
    setAlert("settings-error", "Não consegui atualizar", errText(err));
  }
}

async function loadSettings() {
  state.backends = await window.pywebview.api.backend_options();
  state.settings = await api("get_settings");
  await Promise.all([loadPrompts(), loadVoices()]);
  renderConfigSummary();
}

const SETTINGS_TITLES = {
  transcricao: ["Transcrição", "O que roda durante a reunião, o que roda depois, e em que idioma."],
  resumo: ["Resumo e prompts", "Quem escreve o resumo, quando, e com que pedido."],
  agenda: ["Agenda", "De onde vêm as próximas reuniões da tela inicial."],
  vozes: ["Vozes", "Quem o Fireball já sabe reconhecer pela voz."],
  sobre: ["Sobre", "Versão e atualização do Fireball."],
};

function showSettingsTab(name) {
  state.settingsTab = name;
  const [title, sub] = SETTINGS_TITLES[name] || SETTINGS_TITLES.transcricao;
  el("set-title").textContent = title;
  el("set-sub").textContent = sub;
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
  // A tela abre com o que já se sabe e só depois busca prompt e vozes: antes,
  // uma falha nesses dois deixava a engrenagem sem efeito nenhum, sem aviso.
  // a tela aparece primeiro; o que depende de dado chega depois (ou avisa)
  showView("settings");
  try {
    renderSettings(); // sempre do estado conhecido, nunca do que ficou na tela
    await Promise.all([loadPrompts(), loadVoices()]);
    renderSettings();
  } catch (err) {
    setAlert("settings-error", "Não consegui carregar tudo de Configurações", errText(err));
  }
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
      aviso_silencio_minutos: Number(el("set-aviso-silencio-minutos").value),
      aviso_minutos: Number(el("set-aviso-minutos").value),
      openai_base_url: el("set-openai-url").value,
      openai_api_key: el("set-openai-key").value,
      openai_model: el("set-openai-model").value,
      groq_api_key: el("set-groq-key").value,
      calendar_provider: el("set-calendar-provider").value,
      gog_account: el("set-gog-account").value,
      my_voice: el("set-my-voice").value,
      language: el("set-language").value,
      data_dir: el("set-data-dir").value.trim(),
      check_updates: el("set-check-updates").checked,
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
    const bar = el("save-bar");
    bar.classList.add("saved");
    el("settings-note").textContent = "Salvo — vale a partir da próxima reunião";
    setTimeout(() => {
      bar.classList.remove("saved");
      markSettings();
    }, 2600);
  } catch (err) {
    setAlert("settings-error", "Não foi possível salvar", errText(err));
  } finally {
    markSettings();
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
    de texto antes de apagar — e desiste sozinho se ninguém confirmar.

    Dois botões, um fluxo: o do cabeçalho (sempre visível) e o do fim da ficha,
    que mora dentro de "Detalhes da reunião" — e essa ficha só existe na vista
    de resumo. Sem o do cabeçalho, não havia como apagar uma reunião aberta na
    vista de notas. */
const BOTOES_EXCLUIR = ["delete-meeting-btn", "delete-btn"];

function resetDelete() {
  for (const id of BOTOES_EXCLUIR) {
    const button = el(id);
    if (!button) continue;
    button.classList.remove("confirming");
    button.textContent = button.dataset.label || "Excluir reunião";
    button.disabled = false;
  }
  const nota = el("delete-note");
  if (nota) nota.textContent = "";
  clearTimeout(state.deleteTimer);
  state.deleteTimer = null;
}

async function onDelete(event) {
  const o = state.open;
  if (!o) return;
  const button = (event && event.currentTarget) || el("delete-btn");
  if (!button) return;
  if (!button.dataset.label) button.dataset.label = button.textContent.trim();

  if (!button.classList.contains("confirming")) {
    // o outro botão desiste: dois "confirmar" na tela pedindo coisas diferentes
    // não é confirmação
    resetDelete();
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
    goHome();
  } catch (err) {
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
    // nome escrito à mão não tem procedência a exibir (name_source: "user")
    setTimeout(() => (el("rename-note").textContent = ""), 2000);
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

// ------------------------------------------------------------------ boot

function tick() {
  // o relógio anda localmente, sem ida ao daemon: só o polling de status
  // (2s) descobre mudança de verdade.
  if (state.active) el("live-card-time").textContent = elapsedSince(state.active.started_at);
  if (state.open && state.open.live) {
    el("mv-elapsed").textContent = liveLabel(state.open.meeting);
    if (state.tab === "transcricao") renderLiveBar();
  }
}

// ---------------------------------------------------- painel da transcrição

/* No Granola a transcrição não é aba: é um painel que abre ao lado da nota,
   pelo botão de onda na barra de baixo. Aqui ele começa aberto — durante uma
   reunião a legenda é o que se olha — e a escolha fica no aparelho. */
const PAINEL_KEY = "fireball.transcricao.painel";

function painelTranscricao(aberto) {
  const painel = el("painel-tr");
  if (!painel) return;
  const quer = aberto === undefined ? !painel.classList.contains("open") : Boolean(aberto);
  painel.classList.toggle("open", quer);
  const botao = el("btn-transcricao");
  if (botao) botao.classList.toggle("on", quer);
  try {
    localStorage.setItem(PAINEL_KEY, quer ? "1" : "0");
  } catch (err) {
    // sem storage, o painel volta aberto na próxima janela — nada quebra
  }
}

function painelTranscricaoInicial() {
  let guardado = null;
  try {
    guardado = localStorage.getItem(PAINEL_KEY);
  } catch (err) {
    guardado = null;
  }
  painelTranscricao(guardado === null ? true : guardado === "1");
}

// ------------------------------------------------------------------- perguntar

/* A conversa é por reunião e vive na janela: o daemon responde uma pergunta por
   vez, sem guardar sessão. O histórico vai junto no pedido (as últimas voltas),
   que é o que faz "e ele?" funcionar depois de "quem falou do ts?". */
const chat = { meetingId: null, history: [], busy: false };

function abrirChat(aberto) {
  const painel = el("chat-panel");
  if (!painel) return;
  const quer = aberto === undefined ? painel.classList.contains("hidden") : Boolean(aberto);
  painel.classList.toggle("hidden", !quer);
  // um painel por vez na coluna da direita: com o chat aberto a transcrição
  // sai de cena, e voltar para ela é fechar o chat
  const reuniao = el("view-meeting");
  if (reuniao) reuniao.classList.toggle("chat-aberto", quer);
  if (quer) {
    const campo = el("ask-input");
    if (campo) campo.focus();
  }
}

function resetChat(meetingId) {
  if (chat.meetingId === meetingId) return;
  chat.meetingId = meetingId;
  chat.history = [];
  chat.busy = false;
  const log = el("ask-log");
  if (!log) return;
  log.innerHTML = "";
  const vazio = document.createElement("div");
  vazio.className = "cp-empty";
  vazio.textContent =
    "Pergunte sobre o que foi dito nesta reunião. A resposta usa a transcrição, " +
    "suas anotações e o resumo — e diz quem falou.";
  log.appendChild(vazio);
}

function chatMensagem(papel, texto, logId = "ask-log") {
  const log = el(logId);
  if (!log) return null;
  const vazio = log.querySelector(".cp-empty");
  if (vazio) vazio.remove();
  const bloco = document.createElement("div");
  bloco.className = `cp-msg ${papel === "assistant" ? "bot" : "me"}`;
  const quem = document.createElement("span");
  quem.className = "who";
  quem.textContent = papel === "assistant" ? "Resposta" : "Você";
  bloco.appendChild(quem);
  if (papel === "assistant") {
    // o markdown vai num filho próprio: `renderMarkdown` limpa o nó em que
    // escreve, e escritos no próprio bloco apagavam o rótulo "Resposta" —
    // a resposta ficava sem quem, colada na pergunta anterior
    const corpo = document.createElement("div");
    corpo.className = "cp-corpo";
    renderMarkdown(texto || "", corpo);
    bloco.appendChild(corpo);
  } else {
    bloco.appendChild(document.createTextNode(texto));
  }
  log.appendChild(bloco);
  log.scrollTop = log.scrollHeight;
  if (logId !== "ask-log") bloco.scrollIntoView({ block: "end" });
  return bloco;
}

// ------------------------------------------- perguntar ao acervo inteiro

const acervo = { history: [], busy: false };
const ROTULO_FONTE = /\[(R\d+(?:\s*[,;]\s*R\d+)*)\]/g;

function goAsk() {
  flushNotes();
  state.open = null;
  abrirChat(false);
  showView("ask");
  el("ask-sugs").classList.toggle("hidden", acervo.history.length > 0);
  el("ask-input").focus();
}

/* Os rótulos [R3] da resposta viram links para a reunião. A tabela rótulo →
   reunião vale só para a resposta que a trouxe: a próxima pergunta escolhe
   outras reuniões e numera de novo. */
function ligarFontes(bloco, fontes) {
  const porRotulo = new Map((fontes || []).map((f) => [f.label, f]));
  const citadas = new Map();
  const andador = document.createTreeWalker(bloco, NodeFilter.SHOW_TEXT);
  const nos = [];
  while (andador.nextNode()) nos.push(andador.currentNode);
  for (const no of nos) {
    const texto = no.nodeValue;
    ROTULO_FONTE.lastIndex = 0;
    if (!ROTULO_FONTE.test(texto)) continue;
    const frag = document.createDocumentFragment();
    let resto = 0;
    texto.replace(ROTULO_FONTE, (inteiro, lista, pos) => {
      frag.append(texto.slice(resto, pos));
      for (const rotulo of lista.split(/\s*[,;]\s*/)) {
        const fonte = porRotulo.get(rotulo);
        if (!fonte) continue;
        citadas.set(fonte.id, fonte);
        frag.append(botaoFonte(fonte, String([...citadas.keys()].indexOf(fonte.id) + 1)));
      }
      resto = pos + inteiro.length;
      return inteiro;
    });
    frag.append(texto.slice(resto));
    no.replaceWith(frag);
  }
  if (!citadas.size) return [];
  const lista = document.createElement("div");
  lista.className = "fontes";
  [...citadas.values()].forEach((fonte, i) => {
    const linha = botaoFonte(fonte, String(i + 1));
    linha.classList.add("fonte-linha");
    linha.append(` ${fonte.name} · ${dateLabel(fonte.started_at)}`);
    lista.appendChild(linha);
  });
  bloco.appendChild(lista);
  return [...citadas.keys()];
}

function botaoFonte(fonte, numero) {
  const botao = document.createElement("button");
  botao.type = "button";
  botao.className = "fonte";
  botao.title = fonte.name;
  const marca = document.createElement("span");
  marca.className = "fonte-n";
  marca.textContent = numero;
  botao.appendChild(marca);
  botao.addEventListener("click", () => openMeeting(fonte.id));
  return botao;
}

function semRotulos(texto, fontes) {
  const nomes = new Map((fontes || []).map((f) => [f.label, f.name]));
  return (texto || "").replace(ROTULO_FONTE, (_, lista) =>
    `(${lista.split(/\s*[,;]\s*/).map((r) => nomes.get(r) || r).join("; ")})`,
  );
}

async function perguntarAcervo(pergunta) {
  if (acervo.busy) return;
  if (state.view !== "ask") goAsk();
  el("ask-sugs").classList.add("hidden");
  acervo.busy = true;
  chatMensagem("user", pergunta, "ask-log-all");
  const pensando = chatMensagem("assistant", "procurando nas reuniões…", "ask-log-all");
  if (pensando) pensando.classList.add("pensando");
  const botao = el("ask-send");
  if (botao) botao.disabled = true;
  try {
    const resposta = await api("ask_library", pergunta, acervo.history);
    if (pensando) pensando.remove();
    const bloco = chatMensagem("assistant", resposta.answer || "(resposta vazia)", "ask-log-all");
    const citadas = bloco ? ligarFontes(bloco, resposta.sources) : [];
    acervo.history.push({ role: "user", content: pergunta });
    acervo.history.push({
      role: "assistant",
      content: semRotulos(resposta.answer, resposta.sources),
      sources: citadas,
    });
    setBanner("");
  } catch (err) {
    if (pensando) pensando.remove();
    chatMensagem("assistant", `Não consegui responder: ${errText(err)}`, "ask-log-all");
  } finally {
    acervo.busy = false;
    if (botao) botao.disabled = false;
  }
}

async function perguntar(texto) {
  const pergunta = (texto || "").trim();
  const aberto = state.open;
  if (!pergunta) return;
  if (!aberto) return perguntarAcervo(pergunta);
  if (chat.busy) return;

  abrirChat(true);
  resetChat(aberto.id);
  chat.busy = true;
  chatMensagem("user", pergunta);
  const pensando = chatMensagem("assistant", "pensando…");
  if (pensando) pensando.classList.add("pensando");
  const botao = el("ask-send");
  if (botao) botao.disabled = true;

  try {
    const resposta = await api("ask", aberto.id, pergunta, chat.history);
    if (pensando) pensando.remove();
    chatMensagem("assistant", resposta.answer || "(resposta vazia)");
    chat.history.push({ role: "user", content: pergunta });
    chat.history.push({ role: "assistant", content: resposta.answer || "" });
    setBanner("");
  } catch (err) {
    if (pensando) pensando.remove();
    chatMensagem("assistant", `Não consegui responder: ${errText(err)}`);
  } finally {
    chat.busy = false;
    if (botao) botao.disabled = false;
  }
}

function onPerguntar() {
  const campo = el("ask-input");
  const texto = campo ? campo.value : "";
  if (campo) campo.value = "";
  perguntar(texto);
}

// ------------------------------------------- notas: o documento é o editor

/* Não existe mais "ver" ↔ "editar": a nota é um `contenteditable`
   (ver a seção "notas" em loadNotes). Aqui só ficam os fios que o navegador
   não faz sozinho — colar texto puro e manter o placeholder em dia. */
function ligarNotaEditavel() {
  const box = el("notes");
  if (!box) return;
  // colar HTML de fora traria fonte, cor e tabela para dentro do markdown: o
  // que entra é o texto, e quem formata é a barra do editor
  box.addEventListener("paste", (event) => {
    const texto = (event.clipboardData || window.clipboardData).getData("text/plain");
    if (!texto) return;
    event.preventDefault();
    document.execCommand("insertText", false, texto);
  });
  atualizarNotaVazia();
}

/* O título do documento já tinha `cursor: text` e não respondia a clique
   nenhum: renomear exigia abrir "Detalhes da reunião" e achar o campo lá
   dentro, longe do nome que se quer trocar. Clicar aqui abre a ficha e põe o
   cursor no campo, com o texto selecionado para sobrescrever de uma vez. */
function ligarTituloEditavel() {
  const titulo = el("mv-name");
  if (!titulo) return;
  titulo.title = "Clique para renomear";
  titulo.addEventListener("click", () => {
    const campo = el("mv-rename");
    if (!campo) return;
    const ficha = campo.closest("details");
    if (ficha) ficha.open = true;
    campo.focus();
    campo.select();
  });
}

// ------------------------------------- notas cruas ↔ resumo, e a citação

/* O Granola mostra as duas coisas no MESMO lugar, uma por vez: as notas de quem
   gravou e, depois, o texto gerado. Nunca lado a lado. O botão de três linhas
   no cabeçalho alterna — e a escolha fica no aparelho. */
const VISTA_KEY = "fireball.nota.vista";

const VISTAS = ["doc", "cru", "det"];

function vistaNota(modo, persistir = true) {
  const corpo = el("nota-corpo");
  if (!corpo) return;
  const vista = VISTAS.includes(modo) ? modo : "doc";
  corpo.classList.toggle("cru", vista === "cru");
  corpo.classList.toggle("det", vista === "det");
  // Detalhes é a ficha que antes ficava recolhida no pé do resumo
  if (vista === "det") document.querySelector(".doc-det").open = true;
  for (const botao of document.querySelectorAll("#vista-nota button[data-vista]")) {
    const ativo = botao.dataset.vista === vista;
    botao.setAttribute("aria-pressed", ativo ? "true" : "false");
    botao.setAttribute("aria-selected", ativo ? "true" : "false");
  }
  if (persistir) {
    try {
      localStorage.setItem(VISTA_KEY, vista);
    } catch (err) {
      // sem storage a vista volta ao resumo na próxima janela
    }
  }
}

function ligarVistaNota() {
  let salvo = null;
  try {
    salvo = localStorage.getItem(VISTA_KEY);
  } catch (err) {
    salvo = null;
  }
  // A abertura não grava preferência: quem nunca escolheu recebe a vista que
  // combina com a reunião (resumo se já existe, notas se ainda não)
  const guardado = VISTAS.includes(salvo) ? salvo : "doc";
  vistaNota(guardado, Boolean(salvo));
  const grupo = el("vista-nota");
  if (grupo) {
    grupo.addEventListener("click", (ev) => {
      const botao = ev.target.closest("button[data-vista]");
      if (botao) vistaNota(botao.dataset.vista);
    });
  }
}

/* A lupa ao lado do ponto abre a fala que o sustenta. A transcrição é buscada
   uma vez por reunião e guardada na janela — o resumo pode ter vinte lupas, e
   vinte chamadas ao daemon para ler o mesmo arquivo seria desperdício. */
const citacao = { id: null, segmentos: null };

async function abrirCitacao(botao) {
  const aberto = state.open;
  if (!aberto) return;
  const seq = Number(botao.dataset.seq);
  if (citacao.id !== aberto.id || !citacao.segmentos) {
    try {
      citacao.segmentos = await api("transcript", aberto.id, 0);
      citacao.id = aberto.id;
    } catch (err) {
      setBanner(errText(err));
      return;
    }
  }
  const segmento = (citacao.segmentos || []).find((s) => Number(s.seq) === seq);
  document.querySelectorAll(".cit-pop").forEach((n) => n.remove());
  const card = document.createElement("div");
  card.className = "cit-pop";
  const quem = (segmento && (segmento.speaker || segmento.speaker_key)) || "fala não encontrada";
  const quando = segmento ? clock(segmento.start || 0) : "";
  const titulo = document.createElement("div");
  titulo.className = "cit-h";
  titulo.textContent = [quando, quem].filter(Boolean).join(" · ");
  const texto = document.createElement("p");
  texto.textContent = segmento ? segmento.text : "esta fala não está mais na transcrição";
  card.append(titulo, texto);
  const fechar = document.createElement("button");
  fechar.className = "cit-x";
  fechar.type = "button";
  fechar.textContent = "✕";
  fechar.addEventListener("click", () => card.remove());
  card.appendChild(fechar);

  const caixa = botao.getBoundingClientRect();
  card.style.left = `${Math.min(window.innerWidth - 340, caixa.left + 18)}px`;
  card.style.top = `${caixa.bottom + window.scrollY + 6}px`;
  document.body.appendChild(card);
  setTimeout(() => {
    const fora = (ev) => {
      if (!card.contains(ev.target)) {
        card.remove();
        document.removeEventListener("mousedown", fora);
      }
    };
    document.addEventListener("mousedown", fora);
  }, 0);
}

document.addEventListener("click", (ev) => {
  const lupa = ev.target.closest && ev.target.closest(".citacao");
  if (lupa) {
    ev.preventDefault();
    ev.stopPropagation();
    abrirCitacao(lupa);
  }
});

// -------------------------------------------------------- modo da transcrição

/* Documento é o padrão: texto corrido numa coluna estreita, que é o que se lê
   depois. Chat mantém o balão de mensageiro, melhor para varrer quem falou por
   último. É preferência de tela, não do daemon — por isso mora no localStorage
   e não no settings.json: o daemon não precisa saber disso e a janela volta na
   mesma escolha. Sem storage (file:// restrito), vale só nesta janela. */
const MODO_KEY = "fireball.transcricao.modo";

function modoTranscricao() {
  try {
    return localStorage.getItem(MODO_KEY) === "chat" ? "chat" : "doc";
  } catch (err) {
    return "doc";
  }
}

function aplicarModoTranscricao(modo) {
  const chat = el("chat");
  if (!chat) return;
  chat.classList.toggle("modo-doc", modo !== "chat");
  chat.classList.toggle("modo-chat", modo === "chat");
  for (const botao of document.querySelectorAll("#tr-modo button[data-modo]")) {
    const ativo = botao.dataset.modo === modo;
    botao.classList.toggle("on", ativo);
    botao.setAttribute("aria-pressed", ativo ? "true" : "false");
  }
}

function ligarModoTranscricao() {
  const grupo = el("tr-modo");
  if (grupo) {
    grupo.addEventListener("click", (ev) => {
      const botao = ev.target.closest("button[data-modo]");
      if (!botao) return;
      try {
        localStorage.setItem(MODO_KEY, botao.dataset.modo);
      } catch (err) {
        // sem storage a escolha vale só nesta janela; não é motivo de banner
      }
      aplicarModoTranscricao(botao.dataset.modo);
    });
  }
  aplicarModoTranscricao(modoTranscricao());
}

async function boot() {
  el("start-btn").addEventListener("click", onStart);
  el("adhoc-link").addEventListener("click", onAdHoc);
  el("stop-btn").addEventListener("click", onStop);
  el("finalize-btn").addEventListener("click", onFinalize);
  el("tr-export").addEventListener("click", onExportTranscript);
  ligarModoTranscricao();
  ligarNotaEditavel();
  ligarVistaNota();
  ligarTituloEditavel();

  // casco do Granola: painel de transcrição, chat e o "＋ Nova nota"
  painelTranscricaoInicial();
  el("btn-transcricao").addEventListener("click", () => painelTranscricao());
  el("nav-perguntar").addEventListener("click", goAsk);
  for (const sugestao of document.querySelectorAll(".ask-sug")) {
    sugestao.addEventListener("click", () => perguntar(sugestao.textContent));
  }
  el("cp-close").addEventListener("click", () => abrirChat(false));
  el("nova-nota-btn").addEventListener("click", () => {
    // o formulário mora na home: sem esta volta, clicar aqui dentro de uma
    // reunião não mostrava nada
    goHome();
    onAdHoc();
  });
  el("ask-send").addEventListener("click", onPerguntar);
  // um campo de pergunta só: o do chat era o mesmo pedido em dois lugares
  for (const id of ["ask-input"]) {
    el(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        onPerguntar();
      }
    });
    el(id).addEventListener("focus", () => {
      if (state.open) abrirChat(true);
    });
  }
  el("mv-diarize").addEventListener("change", onDiarizeChange);
  el("files-tab").addEventListener("click", onOpenFolder);
  el("delete-btn").addEventListener("click", onDelete);
  el("delete-meeting-btn").addEventListener("click", onDelete);
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
  el("update-card").addEventListener("click", async () => {
    await openSettings();
    showSettingsTab("sobre");
  });
  el("update-check").addEventListener("click", onUpdateCheck);
  el("update-apply").addEventListener("click", onUpdateApply);
  el("config-link").addEventListener("click", openSettings);
  el("settings-back").addEventListener("click", goHome);
  el("settings-save").addEventListener("click", onSaveSettings);
  el("settings-discard").addEventListener("click", () => renderSettings());
  el("view-settings").addEventListener("input", markSettings);
  el("view-settings").addEventListener("change", markSettings);
  el("recent-all").addEventListener("click", openBrowse);
  el("crumb-acervo").addEventListener("click", openBrowse);
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

  ligarRelogios();
}

/* Os três relógios da tela: o status (que descobre reunião nova), o chat da
   reunião ao vivo e o cronômetro local. Com a janela escondida — minimizada,
   atrás de outra ou em outra área de trabalho — não há nada para atualizar, e
   cada volta custa layout num renderer que vive semanas. Ao voltar, uma volta
   de cada um antes de religar evita mostrar a tela velha. */
const RELOGIOS = [
  { ms: POLL_STATUS_MS, fn: () => refreshStatus() },
  { ms: POLL_CHAT_MS, fn: () => {
      if (state.view === "meeting" && state.open && state.open.live) pollChat();
    } },
  { ms: 1000, fn: () => tick() },
];

function ligarRelogios() {
  RELOGIOS.forEach((relogio) => {
    if (!relogio.id) relogio.id = setInterval(relogio.fn, relogio.ms);
  });
}

function desligarRelogios() {
  RELOGIOS.forEach((relogio) => {
    if (relogio.id) {
      clearInterval(relogio.id);
      relogio.id = null;
    }
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    desligarRelogios();
    return;
  }
  RELOGIOS.forEach((relogio) => relogio.fn());
  ligarRelogios();
});

/* Uma resposta para uma chamada desta página pode chegar depois de a página
   ser trocada: o pywebview entrega o resultado com um `evaluate_js`, e o
   Chromium pode rodar esse JS já no documento novo, onde o callback da chamada
   não existe — vira `TypeError` e traceback no log do daemon (ver
   fireball/gui/pagina.py, que tem o mesmo remendo do outro lado). O proxy
   devolve no-op para o que não existe e deixa passar o que está registrado. */
function perdoarRespostasAtrasadas() {
  const api = window.pywebview;
  if (!api || !api._returnValuesCallbacks) return;
  const registro = api._returnValuesCallbacks;
  api._returnValuesCallbacks = new Proxy(registro, {
    get: (alvo, chave) => alvo[chave] ?? new Proxy({}, { get: () => () => {} }),
  });
}

if (window.pywebview && window.pywebview._returnValuesCallbacks) perdoarRespostasAtrasadas();
window.addEventListener("pywebviewready", perdoarRespostasAtrasadas);

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot);
