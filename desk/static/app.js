const view = document.querySelector("#view");
const nav = document.querySelector("#nav");

const PHASE = {
  starting: "садится",
  waiting: "ищет стол",
  playing: "играет сейчас",
  stopping: "доиграет",
  switching: "смена агента",
  stopped: "остановлен",
};

let variant = "3";
let points = "101";
let bots = [];
let profiles = [];
let selectedId = "";
let page = "";
let botPageId = "";
let dayLogin = "";
let cursors = {};
let query = "";
let formatFilter = "all";
let agentNames = ["fallen", "strong", "strong2", "heuristic", "random"];
const revealed = new Set();
let teachTrickN = null; // выбранная прошлая взятка для разбора
let historyTab = "open"; // open | packs (mobile)
let historyFilter = { bot: "", status: "", from: "", to: "" };
let historyOffset = 0;
let packsOffset = 0;
let packsQuery = "";
let historyGamesCache = [];
let packJobId = "";
let historyViewer = null; // { meta, events, handIndex, mode, recon, decisions }
// Учение: приватное трио (хост + 2 гостя)
let trainSeats = ["", "", ""]; // logins; [0] = host
let trainPassword = "";
let trainAgent = "";
let trainPoints = "101";
let trainFocusLogin = "";
let trainBusy = false;
let trainLastPassword = "";
let trainError = "";

function cardName(token) {
  const ranks = { 7: "7", 8: "8", 9: "9", 10: "10", J: "валет", Q: "дама", K: "король", A: "туз" };
  const suits = { clubs: "треф", diamonds: "бубен", hearts: "червей", spades: "пик" };
  const match = String(token).match(/^(7|8|9|10|J|Q|K|A)_(clubs|diamonds|hearts|spades)$/);
  return match ? `${ranks[match[1]]} ${suits[match[2]]}` : token;
}

const NICK_TONES = ["blue", "red", "yellow", "violet"];

function nickTone(name) {
  const raw = String(name || "").trim();
  if (!raw || /^мы$/i.test(raw)) return "us";
  let hash = 0;
  for (let i = 0; i < raw.length; i += 1) hash = (hash * 31 + raw.charCodeAt(i)) >>> 0;
  return NICK_TONES[hash % NICK_TONES.length];
}

function formatJournalBody(body) {
  // «мы: туз треф» / «FEAR1995: валет пик» / «Simion2305 (бот): 9 червей»
  const play = String(body || "").match(/^([^:]+):\s+(.+)$/);
  if (!play) return escapeHtml(body);
  const who = play[1].trim();
  const card = play[2].trim();
  const tone = nickTone(who);
  return `<b class="nick tone-${tone}">${escapeHtml(who)}</b> <span class="play-arrow">→</span> <span class="play-card">${escapeHtml(card)}</span>`;
}

function ru(line) {
  if (/[А-Яа-яЁё]/.test(line)) return line;
  const whole = [
    [/^\[Audit\] (.+) already exists; recording to (.+) instead -- one recording holds one player's view\.$/, (_, from, to) => `[запись] ${from} уже есть, пишу в ${to}. В одном файле вид одного игрока.`],
    [/^\[Bot\] Agent: (.+)$/, (_, name) => `[бот] агент: ${name}`],
    [/^\[Bot\] 3-player build: only tables of three\.$/, () => "[бот] сборка на троих: только столы из трёх игроков."],
    [/^\[Bot\] 3-player tables to (51|101) only\.$/, (_, n) => `[бот] только столы до ${n}.`],
    [/^\[Bot\] 3-player tables to 51 or 101\.$/, () => "[бот] столы до 51 и до 101."],
    [/^\[Bot\] AUDIT mode on: recording frames to (.+)$/, (_, path) => `[бот] запись включена, кадры в ${path}`],
    [/^\[SDK\] Launching background Node\.js bridge on port (\d+)\.\.\.$/, (_, port) => `[связь] запускаю мост, порт ${port}`],
    [/^\[Bridge\] Daemon listening on (.+)$/, (_, url) => `[мост] слушаю ${url}`],
    [/^\[Bridge\] Python Agent connected\.$/, () => "[мост] агент подключился."],
    [/^\[SDK\] Connected to bridge daemon\. Releasing join request\.\.\.$/, () => "[связь] мост на связи, ищу стол."],
    [/^\[Bridge\] Checking for an ongoing active game\.\.\.$/, () => "[мост] смотрю, нет ли уже партии."],
    [/^\[Bridge\] No active game\. Searching public tables to (51|101)\.\.\.$/, (_, n) => `[мост] партии нет. Ищу стол до ${n}.`],
    [/^\[Bridge\] No active game\. Searching public tables\.\.\.$/, () => "[мост] партии нет. Ищу открытый стол."],
    [/^\[Bridge\] Creating a table\.\.\.$/, () => "[мост] создаю стол."],
    [/^\[Bridge\] Reserving seat at table (.+)\.\.\.$/, (_, table) => `[мост] занимаю место за столом ${table}`],
    [/^\[Bridge\] Connecting Colyseus client to (.+)\.\.\.$/, (_, url) => `[мост] подключаюсь к ${url}`],
    [/^\[Bot\] New table \(session (\d+)\); clearing per-table state\.$/, (_, n) => `[бот] новый стол, сессия ${n}`],
    [/^\[SDK Error\]: No public open tables available\.$/, () => "[связь] открытых столов нет."],
    [/^\[SDK Error\]: (.+)$/, (_, text) => `[связь] ошибка: ${ru(text)}`],
    [/^\[SDK\] could not join a table \((.+)\); looking for another table in (\d+)s\. \((\d+) played so far\)$/, (_, why, wait, played) => `[связь] сесть не удалось (${ru(why)}). Следующая попытка через ${wait} с. Сыграно: ${played}.`],
    [/^\[SDK\] (.+); looking for another table in (\d+)s\. \((\d+) played so far\)$/, (_, why, wait, played) => `[связь] ${ru(why)} Следующая попытка через ${wait} с. Сыграно: ${played}.`],
    [/^\[SDK\] (.+); rejoining now\.$/, (_, why) => `[связь] ${ru(why)} Сажусь снова.`],
    [/^\[SDK\] Session ended: (.+)\. Not rejoining\.$/, (_, why) => `[связь] сессия закрыта (${ru(why)}). Снова не сажусь.`],
    [/^\[SDK\] Joined Game Room: (.+) \| Player ID: (.+)$/, (_, room) => `[связь] сел в комнату ${room}`],
    [/^\[Bot Action\] -> PASS$/, () => "[ход] пас"],
    [/^\[Trick\] (.+) plays (\S+)$/, (_, who, card) => {
      const us = /^мы$/i.test(who);
      return `[ход] ${us ? "мы" : who}: ${cardName(card)}`;
    }],
    [/^\[Bot Action\] -> PLAY CARD: (\S+)(?: \(.+\))?$/, (_, card) => `[ход] мы: ${cardName(card)}`],
    [/^\[Bot Action\] -> CHOOSE SUIT \((\d+)\)$/, (_, suit) => `[ход] выбираю масть ${suit}`],
    [/^\[Bot Action\] -> ACCEPT FACE-UP \(Suit: (\d+)\)$/, (_, suit) => `[ход] беру открытую карту, масть ${suit}`],
    [/^\[Bot Action\] -> SHOW COMBINATION: (.+)$/, (_, what) => `[ход] объявляю комбинацию: ${what}`],
    [/^\[Bot Action\] -> SWAP SEVEN \((.+)\)$/, (_, what) => `[ход] меняю семёрку козыря (${what})`],
  [/^No public open tables to (51|101)\.?$/, (_, n) => `открытых столов до ${n} нет`],
  [/^No public open tables available\.?$/, () => "открытых столов нет"],
  [/^No public open tables to (51|101) — через (\d+)с открою стол до \1$/, (_, n, wait) => `столов до ${n} нет — через ${wait}с открою свой`],
  [/^No public open tables available — через (\d+)с открою стол до (51|101)$/, (_, wait, n) => `столов нет — через ${wait}с открою стол до ${n}`],
  ];
  for (const [pattern, render] of whole) {
    const match = line.match(pattern);
    if (match) return render(...match);
  }
  return line
    .replace(/^\[AUDIT:VIOLATION\]/, "[запись] нарушение")
    .replace(/^\[AUDIT:INFO\]/, "[запись]")
    .replace(/^\[AUDIT\]\[ERROR\]/, "[запись] ошибка")
    .replace(/^\[AUDIT\]/, "[запись]")
    .replace(/card conservation: accounted (\d+)\/(\d+)/g, "карты: учтено $1 из $2")
    .replaceAll("[Bot][SEAT LOST]", "[бот] место потеряно")
    .replaceAll("[Bot][CRITICAL]", "[бот] критично")
    .replaceAll("[Bot][ERROR]", "[бот] ошибка")
    .replaceAll("[Bot][WARN]", "[бот] внимание")
    .replaceAll("[Bot Action]", "[ход]")
    .replaceAll("[Bot]", "[бот]")
    .replaceAll("[Bridge]", "[мост]")
    .replaceAll("[SDK Error]", "[связь] ошибка")
    .replaceAll("[SDK]", "[связь]")
    .replaceAll("[Sync][WARN]", "[синхр] внимание")
    .replaceAll("[Sync]", "[синхр]")
    .replaceAll("[MSG]", "[сообщение]")
    .replaceAll("[Audit]", "[запись]")
    .replace(/trick (\d+) ok/g, "взятка $1 сыграна");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

function clock(startedAt, endAt) {
  const end = endAt || Date.now() / 1000;
  const seconds = Math.max(0, Math.floor(end - startedAt));
  const parts = [Math.floor(seconds / 3600), Math.floor((seconds % 3600) / 60), seconds % 60];
  return parts.map((part) => String(part).padStart(2, "0")).join(":");
}

function uptime(bot) {
  const key = `belot-uptime:${bot.id}:${Math.floor(Number(bot.startedAt) || 0)}`;
  if (bot.running) {
    try { localStorage.removeItem(key); } catch { /* private mode */ }
    return clock(bot.startedAt);
  }
  const fromServer = Number(bot.stoppedAt) || 0;
  if (fromServer) {
    try { localStorage.setItem(key, String(fromServer)); } catch { /* private mode */ }
    return clock(bot.startedAt, fromServer);
  }
  let saved = 0;
  try { saved = Number(localStorage.getItem(key)) || 0; } catch { /* private mode */ }
  if (!saved) {
    saved = Date.now() / 1000;
    try { localStorage.setItem(key, String(saved)); } catch { /* private mode */ }
  }
  return clock(bot.startedAt, saved);
}

function sum(key) {
  return bots.reduce((total, bot) => total + (bot.stats[key] || 0), 0);
}

function icon(name) {
  const paths = {
    spade: '<path d="M12 2c2 3 5 5 5 9a5 5 0 0 1-4 4.9V18h2v2H9v-2h2v-2.1A5 5 0 0 1 7 11c0-4 3-6 5-9z"/>',
    table: '<path d="M4 10h16v2H4zm2 3h2v5H6zm10 0h2v5h-2z"/>',
    chart: '<path d="M5 19V9h3v10zm5 0V5h3v14zm5 0v-7h3v7z"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 3l1.2 2.4 2.6.4-1.8 1.9.4 2.6L12 9.2 9.6 10.3l.4-2.6L8.2 5.8l2.6-.4zM12 21l-1.2-2.4-2.6-.4 1.8-1.9-.4-2.6L12 14.8l2.4 1.1-.4 2.6 1.8 1.9-2.6.4zM3 12l2.4-1.2.4-2.6L7.7 9.8 10.3 9l-1.1 2.4.4 2.6-2.6.4zM21 12l-2.4 1.2-.4 2.6-1.9-1.8L13.7 15l1.1-2.4-.4-2.6 2.6-.4z"/>',
    bot: '<rect x="6" y="8" width="12" height="10" rx="2"/><path d="M12 8V5M9 12h.01M15 12h.01M9 16h6"/>',
    rocket: '<path d="M14 10c2-3 3-6 3-6s-3 1-6 3c-2 1-3 3-3 3l3 3s2-1 3-3z"/><path d="M8 10l-2 2 2 1 1 2 2-2"/><path d="M9 15c-1 2-2 3-4 4 1-2 2-3 4-4"/>',
    user: '<circle cx="12" cy="8" r="3"/><path d="M5 19c1.5-3 3.8-4 7-4s5.5 1 7 4"/>',
    users: '<circle cx="6" cy="9" r="2"/><circle cx="12" cy="8" r="2"/><circle cx="18" cy="9" r="2"/><path d="M3 17c.7-2.2 2.1-3.2 4-3.2S10.3 14.8 11 17M8 17c.6-2 1.8-3 3.5-3s2.9 1 3.5 3M14 17c.6-2 1.7-3 3.2-3"/>',
    key: '<circle cx="8" cy="12" r="3"/><path d="M11 12h9l-2 2 2 2-2 2"/>',
    lock: '<rect x="6" y="11" width="12" height="8" rx="1"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    eye: '<path d="M2 12s4-6 10-6 10 6 10 6-4 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="2.5"/>',
    doc: '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5"/>',
    trash: '<path d="M5 7h14M9 7V5h6v2M8 7l1 13h6l1-13"/>',
    copy: '<rect x="8" y="8" width="10" height="12" rx="1"/><path d="M6 16V5h9"/>',
    search: '<circle cx="11" cy="11" r="5"/><path d="M15 15l4 4"/>',
    out: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M16 13v6H5V8h6"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="1.5"/>',
    play: '<path d="M8 5v14l11-7z" fill="currentColor" stroke="none"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name] || ""}</svg>`;
}

function people(count) {
  return `<span class="people">${"<i></i>".repeat(count)}</span>`;
}

function crowd(count) {
  const one = `<svg class="fig" viewBox="0 0 12 16" aria-hidden="true"><circle cx="6" cy="3.1" r="2.15"/><path d="M1.4 15.2c.35-3.5 1.7-5.3 4.6-5.3s4.25 1.8 4.6 5.3z"/></svg>`;
  return `<span class="crowd">${one.repeat(count)}</span>`;
}

function route() {
  const hash = location.hash || "#/";
  if (hash.startsWith("#/bot/")) return { page: "bot", id: decodeURIComponent(hash.slice(6)) };
  if (hash.startsWith("#/day/")) return { page: "day", id: decodeURIComponent(hash.slice(6)) };
  if (hash === "#/day") return { page: "day", id: "" };
  if (hash === "#/bots") return { page: "bots", id: "" };
  if (hash === "#/train") return { page: "train", id: "" };
  if (hash === "#/stats") return { page: "stats", id: "" };
  if (hash === "#/history") return { page: "history", id: "" };
  return { page: "home", id: "" };
}

function drawNav() {
  const here = route().page;
  const items = [
    ["home", "#/", "table", "Стол"],
    ["bots", "#/bots", "bot", "Боты"],
    ["train", "#/train", "key", "Учение"],
    ["day", "#/day", "chart", "Сегодня"],
    ["history", "#/history", "doc", "История"],
    ["stats", "#/stats", "chart", "Статистика"],
  ];
  document.querySelector(".brand").textContent = "♠ belotmd_agent";
  nav.innerHTML = items.map(([id, href, glyph, label]) => `<a href="${href}" class="${here === id || (here === "bot" && id === "bots") ? "is-on" : ""}">${icon(glyph)} ${label}</a>`).join("");
  const playing = bots.some((bot) => bot.running && bot.phase === "playing");
  const alive = bots.some((bot) => bot.running);
  document.querySelector("#status-title").textContent = "Бот подключен";
  document.querySelector("#status-note").textContent = playing ? "Играет" : alive ? "Ищет стол" : "Готов к игре";
}

function seatButtons(compact) {
  if (compact) {
    return `
      <button type="button" class="seat compact ${variant === "3" ? "is-on" : ""}" data-variant="3">${people(3)}<b>На троих</b><i class="mark"></i></button>
      <button type="button" class="seat compact ${variant === "4" ? "is-on" : ""}" data-variant="4">${people(4)}<b>На четверых</b><i class="mark"></i></button>`;
  }
  return `
    <button type="button" class="seat ${variant === "3" ? "is-on" : ""}" data-variant="3">
      <span class="top">${people(3)}<i class="mark"></i></span>
      <b>На троих</b><small>belotmd_agent · fallen / strong / strong2 / heuristic / random</small>
    </button>
    <button type="button" class="seat ${variant === "4" ? "is-on" : ""}" data-variant="4">
      <span class="top">${people(4)}<i class="mark"></i></span>
      <b>На четверых</b><small>belotmd рядом · без strong</small>
    </button>`;
}

function pointsHtml() {
  const options = [["all", "Все"], ["51", "До 51"], ["101", "До 101"]];
  return `
    <div class="points-row" data-points-row ${variant === "3" ? "" : "hidden"}>
      <span class="cap">Партия до</span>
      <div class="points">${options.map(([value, label]) => `<button type="button" class="point ${points === value ? "is-on" : ""}" data-points="${value}">${label}</button>`).join("")}</div>
    </div>`;
}

function homeHtml() {
  return `
    <h1>Стол</h1>
    <p class="lead">Один профиль — один агент. На «Статистика» видно, кто выиграл больше.</p>
    <div class="layout">
      <form class="panel" id="form">
        <div class="section-label">${icon("users")}<div><h2>Какой стол</h2><small>Выберите количество игроков</small></div></div>
        <div class="seats">${seatButtons(false)}</div>
        ${pointsHtml()}
        <div class="section-label">${icon("key")}<div><h2>Учётные данные</h2><small>Сохраняются в profiles.json на этом ПК — в следующий раз выбираете профиль</small></div></div>
        <label><span class="cap">Сохранённый профиль</span><span class="box"><span class="glyph">${icon("user")}</span><select id="profile-pick"><option value="">Новый профиль…</option></select></span></label>
        <div class="fields">
          <label><span class="cap">Логин</span><span class="box"><span class="glyph">${icon("user")}</span><input name="login" placeholder="логин" required autocomplete="username"></span></label>
          <label><span class="cap">Пароль</span><span class="box"><span class="glyph">${icon("lock")}</span><input name="password" type="password" placeholder="пароль" required autocomplete="current-password"><button class="eye" type="button" data-eye aria-label="Показать пароль">${icon("eye")}</button></span></label>
        </div>
        <div class="profile-actions">
          <label class="check"><input name="saveProfile" type="checkbox" checked> Сохранить профиль после посадки</label>
          <button class="ghost" type="button" id="profile-delete" disabled>${icon("trash")} Удалить профиль</button>
        </div>
        <div class="section-label">${icon("bot")}<div><h2>Настройки бота</h2><small>Strong по умолчанию. Разным логинам — разные агенты, потом сравнение.</small></div></div>
        <label><span class="cap">Бот</span><span class="box"><span class="glyph">${icon("bot")}</span><select name="agent" id="agent"></select></span></label>
        <label class="check"><input name="once" type="checkbox"> Одна партия, потом встать</label>
        <p class="error" id="error"></p>
        <p class="warn" id="warn"></p>
        <div class="actions">
          <button class="go" type="submit">Сесть за стол</button>
          <button class="stop" type="button" id="stand" ${selectedId ? "" : "disabled"}>Встать</button>
          <div class="ready"><i></i><span><b id="ready-title">Готов к игре</b><small id="ready-note">Бот подключен и ожидает посадки за выбранный стол.</small></span></div>
        </div>
      </form>
      <section class="panel home-feed">
        <div class="journal-head">
          <div><h2>${icon("doc")} История ботов</h2><small class="quiet" id="journal-who">Логин · победы · счёт сейчас</small></div>
          <button class="ghost" type="button" id="clear" ${bots.length ? "" : "disabled"}>${icon("trash")} Очистить</button>
        </div>
        <div class="bot-blocks" id="bot-blocks"></div>
      </section>
    </div>`;
}

function accountOptions() {
  const seen = new Map();
  bots.forEach((bot) => seen.set(bot.login, bot));
  return [...seen.values()];
}

function fillAccounts() {
  const select = document.querySelector("#quick-account");
  if (!select) return;
  const accounts = accountOptions();
  const current = select.value;
  select.innerHTML = accounts.map((bot) => `<option value="${bot.id}">${escapeHtml(bot.login)}</option>`).join("") || `<option value="">Сначала добавьте бота</option>`;
  if ([...select.options].some((option) => option.value === current)) select.value = current;
  const selected = bots.find((bot) => bot.id === select.value);
  const busy = Boolean(selected && bots.some((bot) => bot.running && bot.login.toLowerCase() === selected.login.toLowerCase()));
  const button = document.querySelector("#quick-start");
  if (button) button.disabled = accounts.length === 0 || busy;
  const note = document.querySelector("#quick-error");
  if (note) {
    if (busy) note.textContent = "Этот логин уже сидит за столом. Сначала остановите его.";
    else if (note.dataset.busy === "1") note.textContent = "";
    note.dataset.busy = busy ? "1" : "";
  }
}

function botsHtml() {
  const running = bots.filter((bot) => bot.running).length;
  const accounts = accountOptions();
  return `
    <div class="bots-top">
      <div><h1>Мои боты</h1><p class="lead">Управляйте игровыми аккаунтами.</p></div>
      <div class="meter"><b id="meter-label">${running} из 100 ботов запущено</b><div class="bar"><i id="meter-bar" style="width:${running ? Math.max(running, 4) : 0}%"></i></div></div>
      <a class="add" href="#/">+ Добавить бота</a>
    </div>
    <div class="bots-mid">
      <section class="panel launch">
        <div class="launch-title">
          <h2>${icon("rocket")} Быстрый запуск</h2>
          <small class="quiet">Запустите бота в один клик.</small>
        </div>
        <div class="launch-row">
          <label><span class="cap">Аккаунт</span>
            <span class="box"><span class="glyph">${icon("user")}</span>
            <select id="quick-account">${accounts.map((bot) => `<option value="${bot.id}">${escapeHtml(bot.login)}</option>`).join("") || `<option value="">Сначала добавьте бота</option>`}</select>
            </span>
          </label>
          <div>
            <span class="cap">Формат стола</span>
            <div class="compact-seats">${seatButtons(true)}</div>
          </div>
          <div class="launch-go">
            <button class="go" type="button" id="quick-start" ${accounts.length ? "" : "disabled"}>${icon("spade")} Сесть за стол</button>
          </div>
        </div>
        ${pointsHtml()}
        <p class="launch-note"><i></i>Бот будет запущен сразу после нажатия.</p>
        <p class="error" id="quick-error"></p>
      </section>
      <aside class="panel summary">
        <h2>${icon("chart")} Сводка</h2>
        <small class="quiet">Текущая активность</small>
        <div class="sum-row"><i class="sum-dot"></i><div><b id="sum-running">${running} из 100</b><small>Запущено ботов</small></div></div>
        <div class="sum-row"><span class="sum-people">${people(3)}</span><div><b id="sum-free">${100 - running}</b><small>Свободных слотов</small></div></div>
        <div class="sum-row">${icon("chart")}<div><b id="sum-hands">${sum("hands")}</b><small>Раундов за сессию</small></div></div>
      </aside>
    </div>
    <section class="panel roster">
      <div class="row-head">
        <div class="roster-title"><h2>${icon("bot")} Список ботов</h2><small class="quiet">Все ваши игровые аккаунты и их состояние.</small></div>
        <div class="filters">
          <label class="seek">${icon("search")}<input id="search" placeholder="Поиск по логину..." value="${escapeHtml(query)}"></label>
          <select id="format">${[["all", "Все форматы"], ["3", "На троих"], ["4", "На четверых"]].map(([value, label]) => `<option value="${value}" ${formatFilter === value ? "selected" : ""}>${label}</option>`).join("")}</select>
        </div>
      </div>
      <div class="table-wrap"><table class="roster-table">
        <thead><tr><th>Статус</th><th>Логин</th><th>Пароль</th><th>Время работы</th><th>Формат</th><th>Действия</th></tr></thead>
        <tbody id="bot-rows"></tbody>
      </table></div>
    </section>`;
}

const HAND_LINE = /\[стол\] раздача \d+ \| партия (\S+) \| очки (\S+) \| комбинации (\S+) \| капот (да|нет) \| места (.*) \| итог (игра|победа|поражение)/;
const remembered = new Map();

function blankMemory() {
  return { hands: 0, wins: 0, losses: 0, score: "", handPoints: "", combos: "", capot: "", seats: "", tracked: false };
}

function rememberLines(id, lines, replace) {
  const row = replace ? blankMemory() : (remembered.get(id) || blankMemory());
  for (const text of lines) {
    const match = String(text).match(HAND_LINE);
    if (!match) continue;
    row.tracked = true;
    row.hands += 1;
    row.score = match[1];
    row.handPoints = match[2];
    row.combos = match[3];
    row.capot = match[4];
    row.seats = match[5].trim();
    if (match[6] === "победа") row.wins += 1;
    else if (match[6] === "поражение") row.losses += 1;
  }
  remembered.set(id, row);
  return row;
}

function viewStats(bot) {
  const stats = { ...(bot.stats || {}) };
  const memory = remembered.get(bot.id);
  if (typeof stats.wins === "number") return { ...stats, tracked: true };
  if (memory?.tracked) {
    return {
      ...stats,
      hands: memory.hands,
      wins: memory.wins,
      losses: memory.losses,
      score: memory.score,
      handPoints: memory.handPoints,
      combos: memory.combos,
      capot: memory.capot,
      seats: memory.seats,
      tracked: true,
    };
  }
  return stats;
}

function dash(value, tracked) {
  if (!tracked) return "—";
  if (value === 0 || value) return value;
  return "—";
}

function resultPercent(stats) {
  const finished = (stats.wins || 0) + (stats.losses || 0);
  if (!stats.tracked || !finished) return null;
  return Math.round((stats.wins / finished) * 100);
}

function seatChips(seats) {
  const raw = String(seats || "").trim();
  if (!raw || raw === "—") return `<span class="seat-empty">Пока никого</span>`;
  return raw.split(/,\s*/).filter(Boolean).map((name) => {
    const us = /^мы$/i.test(name.trim());
    return `<span class="seat-chip ${us ? "us" : ""}">${escapeHtml(name.trim())}</span>`;
  }).join("");
}

function renderScoreRows(names, rows, expanded) {
  const collapsed = !expanded && rows.length > 6;
  const visible = collapsed ? rows.slice(-5) : rows;
  const hidden = collapsed ? rows.length - visible.length : 0;
  return `
    <table>
      <thead>
        <tr>${names.map((name) => {
          const short = name.length > 10 ? `${name.slice(0, 9)}…` : name;
          const us = /^мы$/i.test(name);
          return `<th class="${us ? "us" : ""}" title="${escapeHtml(name)}">${escapeHtml(short)}</th>`;
        }).join("")}</tr>
      </thead>
      <tbody>
        ${visible.map((row) => `<tr>${names.map((_, i) => {
          const cell = row[i] == null ? "—" : String(row[i]);
          const bolt = /^БТ-/i.test(cell) || /^BT-/i.test(cell);
          return `<td class="${bolt ? "bolt" : ""}">${escapeHtml(cell)}</td>`;
        }).join("")}</tr>`).join("")}
      </tbody>
    </table>
    ${collapsed ? `<button type="button" class="score-toggle" data-expand-score aria-label="Показать все ряды">▼</button>` : ""}
    ${!collapsed && hidden === 0 && rows.length > 6 ? `<button type="button" class="score-toggle" data-collapse-score aria-label="Свернуть">▲</button>` : ""}`;
}

function scoreTableHtml(stats, expanded = false) {
  const board = stats.scoreboard;
  if (!board || !Array.isArray(board.names) || !board.names.length) {
    return `
      <div class="results-seats">
        <span class="results-kicker">За столом</span>
        <div class="seat-row" data-stat="res-seats">${seatChips(stats.seats)}</div>
        <p class="seat-hint">Табло как на belot.md появится после следующей раздачи.</p>
      </div>`;
  }
  const names = board.names;
  const rows = Array.isArray(board.rows) ? board.rows : [];
  return `
    <div class="results-seats score-table-wrap" data-scoreboard="${encodeURIComponent(JSON.stringify(board))}" data-expanded="${expanded ? "1" : "0"}">
      <span class="results-kicker">Табло партии</span>
      <div class="score-table" data-stat="res-board">
        ${renderScoreRows(names, rows, expanded)}
      </div>
    </div>`;
}

function resultsBoard(stats, expanded = false) {
  const tracked = Boolean(stats.tracked);
  if (!tracked) {
    return `
      <div class="results-empty">
        <b>Ждём первую раздачу</b>
        <span>Счёт, имена за столом и % побед появятся после посадки.</span>
      </div>`;
  }
  const percent = resultPercent(stats);
  const finished = (stats.wins || 0) + (stats.losses || 0);
  const tone = percent == null ? "" : percent >= 50 ? "good" : percent > 0 ? "mid" : "bad";
  return `
    <div class="results-board">
      <div class="results-score">
        <span class="results-kicker">Счёт партии</span>
        <b data-stat="res-score">${escapeHtml(stats.score || "—")}</b>
      </div>
      <div class="results-rate ${tone}">
        <span class="results-kicker">Побед</span>
        <b data-stat="res-percent">${percent == null ? "—" : percent + "%"}</b>
        <small data-stat="res-finished">${finished ? `из ${finished} партий` : "ещё нет итогов"}</small>
      </div>
      ${scoreTableHtml(stats, expanded)}
    </div>`;
}

function phaseClass(phase) {
  if (phase === "playing") return "badge";
  if (phase === "stopping" || phase === "switching") return "badge wait";
  if (phase === "waiting" || phase === "starting") return "badge wait";
  return "badge stop";
}

function stopNote(bot) {
  if (!bot.stopping) return "";
  if (typeof bot.remaining === "number" && bot.remaining <= 0) {
    return "Партия на финише — освободит место сразу после неё.";
  }
  return "Остановится в конце раунда.";
}

function remainLabel(bot) {
  if (!bot.stopping || typeof bot.remaining !== "number") return "";
  if (bot.remaining <= 0) return "финиш";
  return `~${bot.remaining}`;
}

function statusCell(bot) {
  if (!bot.stopping) {
    return `<span class="online"><i></i>Онлайн</span>`;
  }
  const rem = remainLabel(bot);
  return `
    <div class="status-stack" title="${escapeHtml(stopNote(bot))}${rem && rem !== "финиш" ? ` Осталось ${rem}` : ""}">
      <span class="ending"><i></i>Доиграет</span>
      ${rem ? `<span class="remain-chip">${escapeHtml(rem)}</span>` : ""}
    </div>`;
}

function stopActs(bot) {
  if (bot.stopping) {
    return `
      <div class="stop-acts">
        <button class="resume-sq" type="button" data-continue="${bot.id}" title="Включить — бот продолжит играть">${icon("play")} Включить</button>
        <button class="cancel-stop" type="button" data-continue="${bot.id}" title="Отменить остановку">Отмена</button>
      </div>`;
  }
  return `<button class="stop-sq" type="button" data-stop="${bot.id}" ${bot.running ? "" : "disabled"} aria-label="Остановить после партии" title="Остановить после этой партии">${icon("stop")}</button>`;
}

function suitGlyph(token) {
  const match = String(token || "").match(/_(clubs|diamonds|hearts|spades)$/);
  return ({ clubs: "♣", diamonds: "♦", hearts: "♥", spades: "♠" })[match?.[1]] || "";
}

function rankGlyph(token) {
  const match = String(token || "").match(/^(7|8|9|10|J|Q|K|A)_/);
  const map = { J: "В", Q: "Д", K: "К", A: "Т" };
  return match ? (map[match[1]] || match[1]) : "?";
}

function isRedSuit(token) {
  return /_(diamonds|hearts)$/.test(String(token || ""));
}

function trumpLabel(trump) {
  if (trump === 0) return "♦ бубны";
  if (trump === 1) return "♥ черви";
  if (trump === 2) return "♣ трефы";
  if (trump === 3) return "♠ пики";
  return "—";
}

function seatPosClass(index, seats, me) {
  // Расклад относительно «мы» внизу.
  const rel = (index - me + seats) % seats;
  if (seats === 3) {
    if (rel === 0) return "live-bottom";
    if (rel === 1) return "live-right";
    return "live-left";
  }
  if (rel === 0) return "live-bottom";
  if (rel === 1) return "live-right";
  if (rel === 2) return "live-top";
  return "live-left";
}

function seatTone(seat, me, seats) {
  if (Number(seat) === Number(me)) return "us";
  const others = [];
  for (let i = 0; i < seats; i += 1) if (i !== Number(me)) others.push(i);
  const idx = others.indexOf(Number(seat));
  return NICK_TONES[Math.max(0, idx) % NICK_TONES.length];
}

function cardFaceHtml(token, caption = "", pick = false, tone = "") {
  const red = isRedSuit(token) ? " red" : "";
  const picked = pick ? " is-pick" : "";
  const player = tone ? ` player-${tone}` : "";
  return `<div class="live-card${red}${picked}${player}" data-card-token="${escapeHtml(token)}" title="${escapeHtml(cardName(token))}">
    <span class="rank">${escapeHtml(rankGlyph(token))}</span>
    <span class="suit">${suitGlyph(token)}</span>
    ${caption ? `<span class="cap">${escapeHtml(caption)}</span>` : ""}
  </div>`;
}

function liveTableHtml(bot) {
  const table = bot.table;
  if (!table) {
    return `
      <section class="panel area-table">
        <h2>${icon("table")} Живой стол</h2>
        <div class="live-empty">Пока нет снимка стола. Перезапустите этого бота (стоп 10с / сесть заново) — новые процессы шлют [LIVE].</div>
      </section>`;
  }
  const seats = Number(table.seats) || (table.players || []).length || 3;
  const me = Number(table.me) || 0;
  const players = table.players || [];
  const trick = table.trick || [];
  const hand = table.hand || [];
  const decision = table.decision;
  const scores = (table.scores || []).join(" : ") || "—";
  const active = table.active;
  const seatStyle = {
    "live-bottom": "left:50%;bottom:8px;transform:translateX(-50%)",
    "live-left": "left:8%;top:28%",
    "live-right": "right:8%;top:28%",
    "live-top": "left:50%;top:6%;transform:translateX(-50%)",
  };
  const seatsHtml = players.map((p) => {
    const cls = seatPosClass(p.seat, seats, me);
    const tone = seatTone(p.seat, me, seats);
    const on = active === p.seat ? " is-active" : "";
    const backs = p.us
      ? ""
      : `<div class="live-backs">${Array.from({ length: Math.min(8, Number(p.count) || 0) }, () => `<i class="live-back tone-${tone}"></i>`).join("")}</div>`;
    const comboChips = (p.combos || []).map((c) =>
      `<span class="live-combo tone-${tone}">${escapeHtml(c.text || c.label || "")}</span>`
    ).join("");
    return `<div class="live-seat ${cls}${on} tone-${tone}" style="${seatStyle[cls] || ""}">
      <div class="who"><b class="nick tone-${tone}">${escapeHtml(p.name || "игрок")}</b>${p.us ? " <small>мы</small>" : ""}${p.bot ? " <small>бот</small>" : ""}${p.declarer ? " <small>козырь</small>" : ""}</div>
      ${comboChips ? `<div class="live-combos">${comboChips}</div>` : ""}
      ${backs}
    </div>`;
  }).join("");
  const trickHtml = trick.length
    ? trick.map((row) => {
      const tone = seatTone(row.seat, me, seats);
      const cap = row.us ? "мы" : (row.name || "").slice(0, 8);
      return cardFaceHtml(row.card, cap, false, tone);
    }).join("")
    : `<span class="quiet">взятка пуста</span>`;
  const handHtml = hand.map((row) => cardFaceHtml(row.card, "", false, "us")).join("") || `<span class="quiet">рука пуста</span>`;
  const why = decision
    ? `${escapeHtml(decision.rule_id || "?")} · ${escapeHtml(decision.reason || decision.source || "")}`
    : "";
  const decls = table.declarations || [];
  const declsHtml = decls.length
    ? `<div class="live-decls">${decls.map((d) => {
        const tone = seatTone(d.seat, me, seats);
        return `<span class="live-decl-chip tone-${tone}"><b>${escapeHtml(d.name || "")}</b> ${escapeHtml(d.text || d.label || "")}</span>`;
      }).join("")}</div>`
    : `<div class="live-decls quiet">Объявлений пока нет (терц / полсотни / сто / каре / белла)</div>`;
  const trumpText = table.trumpName || trumpLabel(table.trump);
  const trumpState = table.trumpOpen ? "открыт" : "ещё закрыт";
  const whoTrump = table.declarerName ? ` · заказал ${table.declarerName}` : "";
  const recent = Array.isArray(table.recentTricks) ? table.recentTricks.slice().reverse() : [];
  if (teachTrickN != null && !recent.some((item) => Number(item.n) === Number(teachTrickN))) {
    teachTrickN = null;
  }
  const picked = teachTrickN != null
    ? recent.find((item) => Number(item.n) === Number(teachTrickN))
    : null;
  const historyHtml = recent.length
    ? `<div class="live-history">
        <div class="live-history-head">Последние взятки <small>клик по взятке — откроются поля для разбора</small></div>
        ${recent.map((item) => {
          const open = Number(item.n) === Number(teachTrickN) ? " is-open" : "";
          const cards = (item.cards || []).map((row) => {
            const tone = seatTone(row.seat, me, seats);
            const cap = row.us ? "мы" : (row.name || "").slice(0, 8);
            return cardFaceHtml(row.card, cap, false, tone);
          }).join("");
          const won = item.winnerName
            ? `<span class="live-history-won${item.usWon ? " is-us" : ""}">забрал ${escapeHtml(item.winnerName)}</span>`
            : "";
          return `<button type="button" class="live-history-row${open}" data-trick-pick="${escapeHtml(String(item.n ?? ""))}" title="Разобрать эту взятку">
            <span class="live-history-n">#${escapeHtml(String(item.n ?? "?"))}</span>
            <div class="live-history-cards">${cards}</div>
            ${won}
          </button>`;
        }).join("")}
      </div>`
    : `<div class="live-history is-empty"><div class="live-history-head">Последние взятки</div><span class="quiet">Пока нет закрытых — появятся после 1–3 взяток (нужен перезапуск бота после обновления).</span></div>`;
  const teachOpen = Boolean(picked);
  const teachLabel = teachOpen
    ? `Разбор взятки #${escapeHtml(String(picked.n))} · ${(picked.cards || []).map((c) => `${c.us ? "мы" : (c.name || "?").slice(0, 8)} ${cardName(c.card)}`).join(" · ")}`
    : "Сначала кликни прошлую взятку выше — появятся поля";
  return `
    <section class="panel area-table">
      <div class="live-table" data-live-table="${escapeHtml(bot.id)}">
        <div class="live-table-head">
          <h2>${icon("table")} Живой стол</h2>
          <div class="live-meta">
            <span class="live-pill live-pill-trump">козырь ${escapeHtml(trumpText)} · ${trumpState}${escapeHtml(whoTrump)}</span>
            <span class="live-pill">раздача ${escapeHtml(String(table.handId ?? "—"))}</span>
            <span class="live-pill">взятки ${escapeHtml(String(table.tricks ?? 0))}</span>
            <span class="live-pill">счёт ${escapeHtml(scores)}</span>
            <span class="live-pill">${escapeHtml(agentShort(table.agent || bot.agent))}</span>
            <span class="live-pill">уроков ${Number(bot.marks) || 0}</span>
          </div>
        </div>
        ${declsHtml}
        <div class="live-felt">
          ${seatsHtml}<div class="live-trick">${trickHtml}</div>
        </div>
        <div class="live-foot">
          <div class="live-hand" title="Наша рука — клик выберет карту для «как лучше»">${handHtml}</div>
          <div class="live-trump-big ${table.trump === 0 || table.trump === 1 ? "is-red" : ""} ${table.trump != null && table.trump !== "" ? "" : "is-empty"}" title="Козырь">
            <span class="live-trump-glyph">${table.trump === 0 ? "♦" : table.trump === 1 ? "♥" : table.trump === 2 ? "♣" : table.trump === 3 ? "♠" : "?"}</span>
            <span class="live-trump-name">${escapeHtml(trumpText)}</span>
            <span class="live-trump-state">${table.trumpOpen ? "открыт" : "закрыт"}${table.declarerName ? ` · ${escapeHtml(table.declarerName)}` : ""}</span>
          </div>
        </div>
        ${why ? `<div class="live-decision">Ход агента: ${why}</div>` : ""}
        ${historyHtml}
        <div class="live-teach${teachOpen ? " is-open" : ""}" data-teach-trick="${teachOpen ? escapeHtml(String(picked.n)) : ""}">
          <div class="live-teach-label">${teachLabel}</div>
          <textarea id="mark-note" placeholder="Что тупил бот на этой взятке? Например: надо было бить тузом, а не королём" ${teachOpen ? "" : "disabled"}></textarea>
          <div class="live-teach-row">
            <input id="mark-better" placeholder="Как лучше (текст)" ${teachOpen ? "" : "disabled"} />
            <input id="mark-better-card" placeholder="Карта лучше (клик по руке / карте взятки)" readonly ${teachOpen ? "" : "disabled"} />
            <button class="danger" type="button" data-mark="${escapeHtml(bot.id)}" ${teachOpen ? "" : "disabled"}>Отметить ошибку</button>
          </div>
          <small class="quiet">Сохраняется снимок стола + эта взятка + заметка в marks — потом на этом учим модель.</small>
        </div>
      </div>
    </section>`;
}

function botHtml(bot) {
  if (!bot) return `<section class="panel"><a class="crumb" href="#/bots">← Мои боты</a><p>Такого бота нет.</p></section>`;
  const stats = viewStats(bot);
  const hands = stats.tracked ? (stats.hands || 0) : "—";
  const finished = (stats.wins || 0) + (stats.losses || 0);
  const percent = stats.tracked && finished ? `${Math.round((stats.wins / finished) * 100)}%` : "—";
  const reconnect = bot.reconnect ?? !bot.once;
  const pause = bot.rejoinDelay ?? 5;
  const limit = bot.maxRounds || 0;
  return `
    <a class="crumb" href="#/bots">← Мои боты / ${escapeHtml(bot.login)}</a>
    <div class="bot-head">
      <h1>${icon("bot")} Бот ${escapeHtml(bot.login)}</h1>
      <span class="${phaseClass(bot.phase)}" data-phase>${PHASE[bot.phase]}</span>
      <span class="uptime">Время работы <b data-clock>${uptime(bot)}</b></span>
      ${bot.switching
        ? `<div class="stop-acts head-stop" style="margin-left:auto">
            <button class="cancel-stop" type="button" data-cancel-switch="${bot.id}">Отменить смену</button>
          </div>`
        : bot.stopping
        ? `<div class="stop-acts head-stop" style="margin-left:auto">
            <button class="resume-sq" type="button" data-continue="${bot.id}">${icon("play")} Включить</button>
            <button class="cancel-stop" type="button" data-continue="${bot.id}">Отмена</button>
          </div>`
        : `<button class="danger" type="button" data-stop="${bot.id}" ${bot.running ? "" : "disabled"} style="margin-left:auto">■ Остановить после партии</button>`}
    </div>
    ${bot.switching ? `<div class="stop-banner"><span class="ending"><i></i>Смена агента</span><span>Пауза ${Number(bot.switchIn) || 0}с · дальше ${escapeHtml(agentShort(bot.switchAgent || ""))}</span></div>` : ""}
    ${bot.stopping && !bot.switching ? `<div class="stop-banner"><span class="ending"><i></i>Доиграет</span><span>${escapeHtml(stopNote(bot))}${remainLabel(bot) && remainLabel(bot) !== "финиш" ? ` · осталось ${remainLabel(bot)}` : ""}</span></div>` : ""}
    <div class="detail">
      ${liveTableHtml(bot)}
      <section class="panel area-account">
        <h2>${icon("user")} Аккаунт</h2><small class="quiet">Данные игрового аккаунта.</small>
        <label><span class="cap">Логин</span><span class="box"><span class="glyph">${icon("user")}</span><input value="${escapeHtml(bot.login)}" readonly><button class="copy" type="button" data-copy="${escapeHtml(bot.login)}" aria-label="Копировать логин">${icon("copy")}</button></span></label>
        <label><span class="cap">Пароль</span><span class="box"><span class="glyph">${icon("lock")}</span><input type="${revealed.has(bot.id) ? "text" : "password"}" value="${escapeHtml(bot.password)}" readonly><button class="eye" type="button" data-reveal="${bot.id}" aria-label="Показать пароль">${icon("eye")}</button></span></label>
      </section>
      <section class="panel area-settings">
        <h2>${icon("gear")} Игровые настройки</h2><small class="quiet">Параметры работы бота.</small>
        <div class="pair">
          <label><span class="cap">Формат стола</span><span class="box"><span class="glyph">${icon("users")}</span><input value="${bot.variant === "3" ? "На троих" : "На четверых"}" readonly></span></label>
          <label><span class="cap">Сейчас</span><span class="box"><span class="glyph">${icon("bot")}</span><input value="${escapeHtml(agentLabel(bot.agent))}" readonly></span></label>
        </div>
        <div class="switch-agent" data-switch-wrap="${escapeHtml(bot.id)}">
          <label><span class="cap">Сменить агента</span><span class="box"><span class="glyph">${icon("bot")}</span>
            <select id="switch-agent" ${bot.switching ? "disabled" : ""}>
              ${(agentNames.length ? agentNames : [bot.agent]).map((name) =>
                `<option value="${escapeHtml(name)}" ${name === (bot.switchAgent || bot.agent) ? "selected" : ""}>${escapeHtml(agentLabel(name))}</option>`
              ).join("")}
            </select>
          </span></label>
          ${bot.switching
            ? `<div class="stop-acts">
                <span class="ending"><i></i>Пауза ${Number(bot.switchIn) || 0}с → ${escapeHtml(agentShort(bot.switchAgent || ""))}</span>
                <button class="cancel-stop" type="button" data-cancel-switch="${bot.id}">Отмена</button>
              </div>`
            : `<button class="go" type="button" data-switch-agent="${bot.id}">Стоп 10с и сменить</button>`}
          <small class="quiet">Бот сразу встанет, через 10 секунд сядет с новым агентом (место на сайте успеет освободиться).</small>
        </div>
        <div class="chips">
          ${bot.variant === "3" ? `<div class="chip">Партия до<b>${bot.points === "51" || bot.points === "101" ? bot.points : "51 и 101"}</b></div>` : ""}
          <div class="chip">Автоподключение<b>${reconnect ? "включено" : "выключено"}</b></div>
          <div class="chip">Пауза между партиями<b>${pause} сек</b></div>
          <div class="chip">Остановить после<b>${limit ? `${limit} раундов` : "без лимита"}</b></div>
        </div>
      </section>
      <section class="panel area-journal">
        <div class="journal-head"><div><h2>${icon("doc")} Журнал активности</h2><small class="quiet">Последние события бота.</small></div><button class="ghost" id="clear" type="button">${icon("trash")} Очистить</button></div>
        <ol class="log" id="log"></ol>
      </section>
      <section class="panel area-stats">
        <h2>${icon("chart")} Статистика</h2><small class="quiet">Результаты текущей сессии.</small>
        <div class="stats-row" style="margin-top:12px">
          <div class="stat"><b data-stat="hands">${hands}</b><span>Сыграно раундов</span></div>
          <div class="stat win"><b data-stat="wins">${dash(stats.wins, stats.tracked)}</b><span>Победы</span></div>
          <div class="stat loss"><b data-stat="losses">${dash(stats.losses, stats.tracked)}</b><span>Поражения</span></div>
          <div class="stat"><b data-stat="percent">${percent}</b><span>Процент побед</span></div>
          <div class="stat"><b class="compact" data-stat="score">${escapeHtml(stats.tracked ? (stats.score || "—") : "—")}</b><span>Счёт партии</span></div>
        </div>
        <div class="stats-row" style="margin-top:10px">
          <div class="stat"><b data-stat="tables">${stats.tables || 0}</b><span>Столов</span></div>
          <div class="stat"><b data-stat="lost">${stats.lostSeats || 0}</b><span>Место забрано</span></div>
          <div class="stat"><b class="compact" data-stat="hand">${escapeHtml(stats.tracked ? (stats.handPoints || "—") : "—")}</b><span>Очки раздачи</span></div>
          <div class="stat"><b class="compact" data-stat="combos">${escapeHtml(stats.tracked ? (stats.combos || "—") : "—")}</b><span>Комбинации</span></div>
          <div class="stat"><b data-stat="capot">${escapeHtml(stats.tracked ? (stats.capot || "—") : "—")}</b><span>Капот</span></div>
        </div>
      </section>
      <section class="panel area-results">
        <div class="results-head">
          <div>
            <h2>Последние результаты</h2>
            <small class="quiet">Текущая партия и последняя раздача</small>
          </div>
        </div>
        <div data-stat="note">${resultsBoard(stats)}</div>
      </section>
    </div>`;
}

function statsHtml() {
  const running = bots.filter((bot) => bot.running).length;
  return `
    <h1>Статистика</h1>
    <p class="lead">Сумма по профилям и сравнение агентов: кто чаще выигрывает на этом столе.</p>
    <div class="stats-row page-stats">
      <div class="stat"><b data-stat="hands">${sum("hands")}</b><span>Раундов</span></div>
      <div class="stat"><b data-stat="wins">${sum("wins")}</b><span>Побед</span></div>
      <div class="stat"><b data-stat="losses">${sum("losses")}</b><span>Поражений</span></div>
      <div class="stat"><b data-stat="tables">${sum("tables")}</b><span>Столов</span></div>
      <div class="stat"><b data-stat="lost">${sum("lostSeats")}</b><span>Потеряно мест</span></div>
      <div class="stat"><b data-stat="running">${running}</b><span>Сейчас запущено</span></div>
    </div>
    <section class="panel compare-panel">
      <div class="compare-head">
        <div class="section-label" style="margin:0">${icon("chart")}<div><h2>Сравнение агентов</h2><small>Разным логинам — разные агенты; здесь видно, кто сильнее</small></div></div>
        <div class="compare-lead" id="compare-best">Загрузка…</div>
      </div>
      <p class="compare-note" id="compare-note"></p>
      <div class="compare-grid" id="compare-rows"></div>
    </section>`;
}

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / (1024 * 1024)).toFixed(1)} MB`;
}

function historyOutcome(result) {
  const raw = String(result || "").trim().toLowerCase();
  if (raw === "победа" || raw === "win" || raw === "won") {
    return { html: ` · <span class="hist-outcome is-win">бот выиграл</span>`, tone: "win" };
  }
  if (raw === "поражение" || raw === "loss" || raw === "lost") {
    return { html: ` · <span class="hist-outcome is-loss">бот проиграл</span>`, tone: "loss" };
  }
  if (raw === "игра" || raw === "draw" || raw) {
    return { html: ` · <span class="hist-outcome is-draw">итог: ${escapeHtml(String(result))}</span>`, tone: "draw" };
  }
  return { html: "", tone: "" };
}

function historyCompareCard(row, best) {
  const isBest = best && row.agent === best;
  const err = row.errorRate == null ? "—" : `${row.errorRate}%`;
  const miss = row.missedTakePct == null ? "—" : `${row.missedTakePct}%`;
  const over = row.overpayPct == null ? "—" : `${row.overpayPct}%`;
  const win = row.winRate == null ? "—" : `${row.winRate}%`;
  const logins = (row.logins || []).map((x) => escapeHtml(x)).join(", ") || "—";
  return `<article class="hist-cmp-card${isBest ? " is-best" : ""}">
    ${isBest ? `<span class="hist-cmp-badge">меньше ошибок</span>` : ""}
    <header><b>${escapeHtml(row.agent || "?")}</b><small class="quiet">${logins}</small></header>
    <div class="hist-cmp-grid">
      <div><span>игр</span><b>${escapeHtml(String(row.games || 0))}</b></div>
      <div><span>побед %</span><b>${escapeHtml(win)}</b></div>
      <div><span>можно лучше</span><b class="tone-bad">${escapeHtml(err)}</b></div>
      <div><span>не взяли</span><b>${escapeHtml(miss)}</b></div>
      <div><span>переплата</span><b>${escapeHtml(over)}</b></div>
      <div><span>удачных</span><b class="tone-ok">${escapeHtml(row.goodPct == null ? "—" : `${row.goodPct}%`)}</b></div>
    </div>
    <small class="quiet">ходы: ${escapeHtml(String(row.ourMoves || 0))} · не взяли ${escapeHtml(String(row.missedTake || 0))} · переплата ${escapeHtml(String(row.overpay || 0))}</small>
  </article>`;
}

async function fillHistoryCompare() {
  const root = document.querySelector("#hist-compare");
  const verdict = document.querySelector("#hist-compare-verdict");
  if (!root) return;
  root.innerHTML = `<p class="quiet">Считаем fallen vs fallen2…</p>`;
  try {
    const response = await fetch("/api/history/compare?limit=80");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      root.innerHTML = `<p class="quiet">${escapeHtml(data.error || "Ошибка сравнения")}</p>`;
      return;
    }
    const rows = data.agents || [];
    if (verdict) verdict.textContent = data.verdict || data.note || "";
    root.innerHTML = rows.length
      ? rows.map((r) => historyCompareCard(r, data.best)).join("")
      : `<p class="quiet">Пока нет данных. Запусти ботов fallen и fallen2, доиграй партии, затем обнови.</p>`;
  } catch (_) {
    root.innerHTML = `<p class="quiet">Не удалось сравнить агентов</p>`;
  }
}

function dayBotCard(row) {
  const played = row.completed || 0; // только доигранные: W+L (+ничьи)
  const rate = row.winRate == null ? "—" : `${row.winRate}%`;
  const loginKey = String(row.login || "").toLowerCase();
  const live = bots.find((b) => b.running && String(b.login || "").toLowerCase() === loginKey);
  const agent = (live && live.agent) || row.agent || "";
  const running = row.running || 0;
  return `<a class="day-bot-card tone-${agentTone(agent)}" href="#/day/${encodeURIComponent(row.login || "")}">
    <header>
      <div>
        <b>${escapeHtml(row.login || "?")}</b>
        ${agent ? `<span class="agent-chip tone-${agentTone(agent)}">${escapeHtml(agentShort(agent))}</span>` : ""}
      </div>
      <span class="day-live ${live ? "on" : "off"}"><i></i>${live ? "сейчас вкл." : "выкл."}</span>
    </header>
    <div class="day-metrics">
      <div><span>игр сегодня</span><b>${escapeHtml(String(played))}</b></div>
      <div class="win"><span>выиграл</span><b>${escapeHtml(String(row.wins || 0))}</b></div>
      <div class="loss"><span>проиграл</span><b>${escapeHtml(String(row.losses || 0))}</b></div>
      <div><span>% побед</span><b>${escapeHtml(rate)}</b></div>
      <div><span>раздач</span><b>${escapeHtml(String(row.hands || 0))}</b></div>
      <div><span>сейчас играет</span><b>${escapeHtml(String(running))}</b></div>
    </div>
    <small class="quiet">${running ? `ещё ${escapeHtml(String(running))} не доиграно` : "все партии за сегодня закрыты"}</small>
  </a>`;
}

function dayGameRow(g) {
  const outcome = historyOutcome(g.result);
  return `<article class="history-file-row day-game-row">
    <div class="history-file-name">
      <b>${escapeHtml(g.file || g.gameId || "?")}</b>
      <small class="quiet">${fmtTs(g.startedAt)}${outcome.html} · раздач ${escapeHtml(String(g.hands ?? 0))}</small>
    </div>
    <span class="history-file-bot">
      ${g.agent ? `<span class="agent-chip tone-${agentTone(g.agent)}">${escapeHtml(agentShort(g.agent))}</span>` : ""}
    </span>
    <span class="history-file-status status-${escapeHtml(g.status || "")}">${escapeHtml(g.status || "—")}</span>
    <button class="ghost" type="button" data-view-game="${escapeHtml(String(g.gameId || ""))}" data-bot="">Открыть</button>
  </article>`;
}

function dayHtml() {
  if (dayLogin) {
    return `
      <a class="crumb" href="#/day">← Все боты за сегодня</a>
      <h1>История · ${escapeHtml(dayLogin)}</h1>
      <p class="lead">Сколько игр сегодня, сколько выиграл и проиграл этот бот.</p>
      <section class="panel" id="day-detail"><p class="quiet">Загрузка…</p></section>`;
  }
  return `
    <h1>Сегодня по ботам</h1>
    <p class="lead">Отдельная сводка каждого аккаунта за текущий день: игры, победы, поражения.</p>
    <div class="day-toolbar">
      <button class="ghost" type="button" id="day-refresh">Обновить</button>
      <span class="quiet" id="day-meta">—</span>
    </div>
    <div class="day-grid" id="day-grid"><p class="quiet">Загрузка…</p></div>`;
}

async function fillDayStats() {
  const detail = document.querySelector("#day-detail");
  const grid = document.querySelector("#day-grid");
  const meta = document.querySelector("#day-meta");
  const q = new URLSearchParams();
  if (dayLogin) q.set("login", dayLogin);
  try {
    const response = await fetch(`/api/history/day?${q}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const err = `<p class="quiet">${escapeHtml(data.error || "Ошибка")}</p>`;
      if (detail) detail.innerHTML = err;
      if (grid) grid.innerHTML = err;
      return;
    }
    const rows = data.bots || [];
    if (meta) {
      const t = data.totals || {};
      const done = rows.reduce((n, r) => n + (r.completed || 0), 0);
      const liveN = rows.reduce((n, r) => n + (r.running || 0), 0);
      meta.textContent = `${data.day || "сегодня"} · ${t.bots || 0} бот. · ${done} игр · ${t.wins || 0}W / ${t.losses || 0}L${liveN ? ` · ещё ${liveN} играет` : ""}`;
    }
    if (dayLogin && detail) {
      const row = rows[0];
      if (!row) {
        detail.innerHTML = `<p class="quiet">За сегодня у ${escapeHtml(dayLogin)} игр нет.</p>`;
        return;
      }
      detail.innerHTML = `
        ${dayBotCard(row).replace(/^<a /, "<div ").replace(/<\/a>$/, "</div>").replace(/href="[^"]*"/, "")}
        <h2 class="day-games-title">Игры сегодня</h2>
        <div class="history-list history-files day-games">
          ${(row.recent || []).length
            ? row.recent.map(dayGameRow).join("")
            : `<p class="quiet">Пока пусто</p>`}
        </div>`;
      return;
    }
    if (grid) {
      grid.innerHTML = rows.length
        ? rows.map(dayBotCard).join("")
        : `<p class="quiet">За сегодня ещё нет записанных игр. Запусти ботов — сводка появится здесь.</p>`;
    }
  } catch (err) {
    console.error("fillDayStats", err);
    if (detail) detail.innerHTML = `<p class="quiet">Не удалось загрузить день</p>`;
    if (grid) grid.innerHTML = `<p class="quiet">Не удалось загрузить день</p>`;
  }
}

function historyHtml() {
  return `
    <h1>История и опыт</h1>
    <p class="lead">A/B: часть ботов на <b>fallen</b>, часть на <b>fallen2</b>. Собираем опыт и смотрим, кто реже «не берёт» и переплачивает.</p>
    <section class="panel hist-compare-panel">
      <div class="hist-compare-head">
        <h2>Fallen vs Fallen2</h2>
        <button class="ghost" type="button" id="hist-compare-refresh">Обновить сравнение</button>
      </div>
      <p class="quiet" id="hist-compare-verdict">Нужны завершённые игры обоих агентов.</p>
      <div class="hist-compare-rows" id="hist-compare"><p class="quiet">Загрузка…</p></div>
    </section>
    <div class="history-tabs">
      <button type="button" class="ghost ${historyTab === "open" ? "is-on" : ""}" data-hist-tab="open">История ботов</button>
      <button type="button" class="ghost ${historyTab === "packs" ? "is-on" : ""}" data-hist-tab="packs">Собранный опыт</button>
    </div>
    <div class="history-layout">
      <section class="panel history-col ${historyTab === "open" ? "is-show" : ""}" id="history-open-col">
        <h2>Файлы игр</h2>
        <small class="quiet">Список файлов из <code>history/bots/</code>. «Собрать опыт» сразу упакует все готовые завершённые игры — без выбора.</small>
        <div class="history-actions">
          <button class="ghost" type="button" id="hist-refresh">Обновить список</button>
          <button class="go" type="button" id="hist-pack" disabled>Собрать опыт</button>
          <span class="quiet" id="hist-pack-hint">0 готовых</span>
        </div>
        <div class="history-progress quiet" id="hist-job"></div>
        <div class="history-file-head">
          <span>Файл</span><span>Бот</span><span>Игра</span><span>Руки</span><span>Размер</span><span></span>
        </div>
        <div class="history-list history-files" id="hist-games"><p class="quiet">Загрузка…</p></div>
        <div class="history-pager">
          <button class="ghost" type="button" id="hist-prev" ${historyOffset <= 0 ? "disabled" : ""}>←</button>
          <button class="ghost" type="button" id="hist-next">→</button>
        </div>
      </section>
      <section class="panel history-col ${historyTab === "packs" ? "is-show" : ""}" id="history-packs-col">
        <h2>Собранный опыт</h2>
        <small class="quiet">Все сборники сохраняются. Скачайте и передайте агенту-разработчику.</small>
        <div class="history-filters">
          <input id="packs-q" placeholder="Поиск" value="${escapeHtml(packsQuery)}" />
          <button class="ghost" type="button" id="packs-refresh">Обновить</button>
        </div>
        <div class="history-list" id="hist-packs"><p class="quiet">Загрузка…</p></div>
        <div class="history-pager">
          <button class="ghost" type="button" id="packs-prev" ${packsOffset <= 0 ? "disabled" : ""}>←</button>
          <button class="ghost" type="button" id="packs-next">→</button>
        </div>
      </section>
    </div>
    <section class="panel history-viewer ${historyViewer ? "is-open" : ""}" id="history-viewer">
      ${historyViewer ? renderHistoryViewer(historyViewer) : "<p class=\"quiet\">Откройте игру кнопкой «Посмотреть».</p>"}
    </section>`;
}

function renderHistoryViewer(data) {
  const meta = data.meta || {};
  const events = data.events || [];
  const mode = data.mode || "at_time";
  const hands = [];
  let cur = null;
  for (const ev of events) {
    if (ev.type === "hand_start") {
      cur = { handId: ev.handId, handNo: ev.handNo, trump: ev.trump, declarer: ev.declarer, ourHand: ev.ourHand, items: [] };
      hands.push(cur);
    } else if (cur) cur.items.push(ev);
    else if (["play", "trick_end", "decision", "hand_end", "analysis_note"].includes(ev.type)) {
      cur = { handId: ev.handId || "?", handNo: hands.length + 1, items: [ev] };
      hands.push(cur);
    }
  }
  const idx = Math.max(0, Math.min(Math.max(hands.length - 1, 0), data.handIndex || 0));
  const hand = hands[idx];
  const recon = (data.recon || []).find((r) => String(r.handId) === String(hand?.handId)) || null;
  const decs = (data.decisions || []).filter((d) => String(d.handId) === String(hand?.handId));
  const reconBlock = recon ? `
    <div class="history-recon">
      <b>Восстановление рук: ${escapeHtml(recon.status || "—")}</b>
      <div class="history-recon-seats">${Object.values(recon.seats || {}).map((s) => {
        const cards = mode === "post_reveal"
          ? (s.initialHand || []).map((c) => escapeHtml(String(c))).join(", ") || "—"
          : (Number(s.seat) === Number(meta.seat)
            ? (hand?.ourHand || s.initialHand || []).map((c) => escapeHtml(String(c))).join(", ") || "—"
            : "неизвестно");
        return `<div><span class="quiet">место ${escapeHtml(String(s.seat))}</span> · ${escapeHtml(s.status || "")}: ${cards}</div>`;
      }).join("")}</div>
    </div>` : "";
  const evalKey = mode === "post_reveal" ? "evalPostReveal" : "evalAtTime";
  return `
    <div class="history-viewer-head">
      <div>
        <h2>Игра ${escapeHtml(String(meta.gameId || data.gameId || ""))}</h2>
        <small class="quiet">${escapeHtml(meta.login || meta.botId || "")} · ${escapeHtml(meta.agent || "")} · ${escapeHtml(meta.status || "")} · ${escapeHtml(meta.result || "—")}</small>
      </div>
      <div class="history-viewer-nav">
        <button class="ghost ${mode === "at_time" ? "is-on" : ""}" type="button" data-view-mode="at_time">Что знал тогда</button>
        <button class="ghost ${mode === "post_reveal" ? "is-on" : ""}" type="button" data-view-mode="post_reveal">С раскрытыми картами</button>
        <button class="ghost" type="button" data-hand-nav="-1" ${idx <= 0 ? "disabled" : ""}>← раздача</button>
        <span>${hands.length ? idx + 1 : 0} / ${hands.length}</span>
        <button class="ghost" type="button" data-hand-nav="1" ${idx >= hands.length - 1 ? "disabled" : ""}>раздача →</button>
        <button class="ghost" type="button" id="hist-viewer-close">Закрыть</button>
      </div>
    </div>
    ${hand ? `<div class="history-hand">
      <div class="live-meta">
        <span class="live-pill">раздача ${escapeHtml(String(hand.handNo ?? hand.handId))}</span>
        <span class="live-pill">козырь ${escapeHtml(String(hand.trump ?? "—"))}</span>
        <span class="live-pill">заказал ${escapeHtml(String(hand.declarer ?? "—"))}</span>
        ${hand.ourHand ? `<span class="live-pill">наша рука ${hand.ourHand.length}</span>` : ""}
      </div>
      ${reconBlock}
      <ol class="history-events">${(hand.items || []).map((ev) => {
        if (ev.type === "play") {
          return `<li><b class="nick">${escapeHtml(ev.us ? "мы" : String(ev.seat))}</b> → ${escapeHtml(cardName(ev.cardToken || String(ev.card)))}</li>`;
        }
        if (ev.type === "trick_end") {
          return `<li class="quiet">взятка #${escapeHtml(String(ev.trickNo))} · ${escapeHtml(ev.label || "")} · ${escapeHtml(ev.winnerName || "")}${ev.points != null ? ` · ${escapeHtml(String(ev.points))} очков` : ""}</li>`;
        }
        if (ev.type === "decision") {
          const act = ev.confirmedAction ?? ev.chosenAction;
          return `<li>ход бота: ${escapeHtml(String(act))} · ${escapeHtml(ev.ruleId || ev.source || "")} · ${escapeHtml(ev.reason || "")}${ev.confirmed ? "" : " · <i>не подтверждён</i>"}</li>`;
        }
        if (ev.type === "hand_end") {
          return `<li>счёт ${(ev.scores || []).join(":")} · ${escapeHtml(ev.result || "")}</li>`;
        }
        if (ev.type === "analysis_note") {
          return `<li class="quiet">реконструкция: ${escapeHtml(ev.reconstructionStatus || "")}</li>`;
        }
        return "";
      }).filter(Boolean).join("") || "<li class=\"quiet\">нет событий</li>"}</ol>
      ${decs.length ? `<div class="history-evals"><b>Оценки (${mode === "post_reveal" ? "после раскрытия" : "на момент хода"})</b>${decs.filter((d) => d.kind !== "agent_decision" || mode === "at_time").slice(0, 40).map((d) => {
        const evl = d[evalKey] || {};
        return `<div class="history-eval-row"><span>#${escapeHtml(String(d.trickNo ?? "—"))} место ${escapeHtml(String(d.seat))} ${escapeHtml(cardName(d.cardToken || String(d.card || d.chosenAction || "")))}</span>
          <b class="eval-${escapeHtml(evl.label || "")}">${escapeHtml(evl.label || "—")}</b>
          <small class="quiet">${escapeHtml(evl.explanation || "")}</small>
          <details class="owner-mark-box"><summary>Отметка владельца</summary>
            <form class="owner-mark-form" data-owner-mark data-game-id="${escapeHtml(String(meta.gameId || data.gameId || ""))}" data-decision-id="${escapeHtml(String(d.decisionId || ""))}" data-hand-id="${escapeHtml(String(d.handId ?? ""))}">
              <label>Оценка <select name="ownerMark"><option value="">—</option><option value="правильно">правильно</option><option value="ошибка">ошибка</option><option value="спорно">спорно</option></select></label>
              <label>Предпочтительная карта <input name="ownerPreferredCard" placeholder="id или A♥" /></label>
              <label>Комментарий <input name="ownerNote" placeholder="заметка" /></label>
              <button type="submit" class="ghost">Сохранить</button>
              <small class="quiet owner-mark-status"></small>
            </form>
          </details>
        </div>`;
      }).join("")}</div>` : ""}
    </div>` : "<p class=\"quiet\">Нет раздач</p>"}`;
}

function visibleBots() {
  return bots.filter((bot) => {
    if (!bot.running) return false;
    const loginOk = bot.login.toLowerCase().includes(query.trim().toLowerCase());
    const formatOk = formatFilter === "all" || bot.variant === formatFilter;
    return loginOk && formatOk;
  });
}

function fillRows() {
  const body = document.querySelector("#bot-rows");
  if (!body) return;
  const rows = visibleBots();
  body.innerHTML = rows.length ? rows.map((bot) => {
    const hint = bot.stopping ? stopNote(bot) : "";
    return `
    <tr class="${bot.stopping ? "is-stopping" : ""}">
      <td class="status-cell">${statusCell(bot)}</td>
      <td class="login-cell">
        <span class="login-name">${escapeHtml(bot.login)}</span>
        ${hint ? `<span class="stop-hint">${escapeHtml(hint)}</span>` : ""}
      </td>
      <td><span class="pass-cell"><span class="${revealed.has(bot.id) ? "shown" : "pass"}">${revealed.has(bot.id) ? escapeHtml(bot.password) : "••••••"}</span><button class="eye" type="button" data-reveal="${bot.id}" aria-label="Показать пароль">${icon("eye")}</button></span></td>
      <td class="clock-cell">${uptime(bot)}</td>
      <td><span class="fmt ${bot.variant === "3" ? "v3" : "v4"}">${crowd(bot.variant === "3" ? 3 : 4)} ${bot.variant === "3" ? "На троих" : "На четверых"}${bot.variant === "3" ? (bot.points === "51" || bot.points === "101" ? ` · до ${bot.points}` : " · все") : ""}</span></td>
      <td class="acts"><a class="open-btn" href="#/bot/${bot.id}">${icon("out")} Открыть</a>
        ${stopActs(bot)}</td>
    </tr>`;
  }).join("") : `<tr><td colspan="6">Пока никто не сидит.</td></tr>`;
}

function agentLabel(name) {
  if (name === "fallen") return "Fallen — правила Fallen поверх strong";
  if (name === "strong") return "Strong — усиленная стратегия";
  if (name === "strong2") return "Strong2 — усиленная v2";
  if (name === "heuristic") return "Heuristic — по правилам";
  if (name === "random") return "Random — наугад";
  if (name === "openrouter") return "OpenRouter — ИИ";
  return name;
}

function agentShort(name) {
  if (name === "fallen") return "Fallen";
  if (name === "fallen2") return "Fallen2";
  if (name === "strong") return "Strong";
  if (name === "strong2") return "Strong2";
  if (name === "heuristic") return "Heuristic";
  if (name === "random") return "Random";
  if (name === "openrouter") return "OpenRouter";
  return String(name || "?");
}

function agentTone(name) {
  if (name === "fallen") return "fallen";
  if (name === "fallen2") return "fallen2";
  if (name === "strong") return "strong";
  if (name === "strong2") return "strong2";
  if (name === "heuristic") return "heuristic";
  if (name === "random") return "random";
  if (name === "openrouter") return "openrouter";
  return "other";
}

async function loadAgents() {
  const response = await fetch(`/api/agents?variant=${variant}`);
  const data = await response.json();
  agentNames = data.agents || agentNames;
  const select = document.querySelector("#agent");
  if (!select) return;
  const previous = select.value;
  select.replaceChildren(...(agentNames || ["random"]).map((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = agentLabel(name);
    return option;
  }));
  const preferred = previous || data.default || "strong";
  if ([...select.options].some((option) => option.value === preferred)) select.value = preferred;
}


function compareProfileHtml(profile, agentKey) {
  const tone = agentTone(agentKey);
  const status = profile.running
    ? (PHASE[profile.phase] || "онлайн")
    : "выкл.";
  return `
    <div class="compare-profile ${profile.running ? "is-on" : "is-off"}">
      <div class="compare-profile-who">
        <b>${escapeHtml(profile.login || "?")}</b>
        <span class="compare-profile-status ${profile.running ? "on" : "off"}"><i></i>${escapeHtml(status)}</span>
      </div>
      <div class="bot-block-meta">
        <span class="agent-chip tone-${tone}" title="${escapeHtml(agentLabel(agentKey))}">${escapeHtml(agentShort(agentKey))}</span>
        <span class="stat-chip win">побед ${profile.wins || 0}</span>
        ${(profile.losses || 0) ? `<span class="stat-chip loss">пор. ${profile.losses}</span>` : ""}
        ${(profile.hands || 0) ? `<span class="stat-chip deal">раздача ${profile.hands}</span>` : `<span class="stat-chip deal">раздача 0</span>`}
        ${profile.score ? `<span class="stat-chip score">счёт ${escapeHtml(profile.score)}</span>` : ""}
      </div>
    </div>`;
}

function compareCardHtml(row, best) {
  const tone = agentTone(row.agent);
  const isBest = best && row.agent === best;
  const rate = row.winRate == null ? "—" : `${row.winRate}%`;
  const rateTone = row.winRate == null ? "" : row.winRate >= 55 ? "good" : row.winRate >= 40 ? "mid" : "bad";
  const profiles = row.profileStats || [];
  const list = profiles.length
    ? profiles.map((profile) => compareProfileHtml(profile, row.agent)).join("")
    : `<p class="compare-empty">нет профилей</p>`;
  return `
    <article class="compare-card tone-${tone} ${isBest ? "is-best" : ""}">
      ${isBest ? `<span class="compare-crown">Лидер</span>` : ""}
      <header class="compare-card-head">
        <span class="agent-chip tone-${tone}" title="${escapeHtml(agentLabel(row.agent))}">${escapeHtml(agentShort(row.agent))}</span>
        <div class="compare-online"><i class="${row.running ? "on" : ""}"></i>${row.running} онлайн · ${row.profiles} проф.</div>
      </header>
      <div class="compare-rate ${rateTone}">
        <span>Побед</span>
        <b>${escapeHtml(rate)}</b>
      </div>
      <div class="compare-metrics">
        <div><span>Победы</span><b class="win">${row.wins}</b></div>
        <div><span>Поражения</span><b class="loss">${row.losses}</b></div>
        <div><span>Раздачи</span><b>${row.hands}</b></div>
        <div><span>Ошибки</span><b>${row.errors}</b></div>
      </div>
      <div class="compare-logins">${list}</div>
    </article>`;
}

async function fillCompare() {
  const body = document.querySelector("#compare-rows");
  if (!body) return;
  try {
    const response = await fetch("/api/compare");
    const data = await response.json();
    const note = document.querySelector("#compare-note");
    const best = document.querySelector("#compare-best");
    if (note) note.textContent = data.note || "";
    if (best) {
      best.innerHTML = data.best
        ? `<span class="agent-chip tone-${agentTone(data.best)}">${escapeHtml(agentShort(data.best))}</span><span>лидирует по победам</span>`
        : `<span class="compare-lead-wait">Пока нет лидера — ещё нет побед</span>`;
    }
    const rows = data.agents || [];
    body.innerHTML = rows.length
      ? rows.map((row) => compareCardHtml(row, data.best)).join("")
      : `<p class="compare-empty">Посадите профили с разными агентами и дождитесь итогов партий.</p>`;
  } catch (err) {
    body.innerHTML = `<p class="compare-empty">Не удалось загрузить сравнение</p>`;
  }
}

async function pollPackJob() {
  if (!packJobId) return;
  const node = document.querySelector("#hist-job");
  try {
    const response = await fetch(`/api/history/jobs/${encodeURIComponent(packJobId)}`);
    const job = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (node) node.textContent = job.error || "Задание недоступно";
      return;
    }
    const p = job.progress || {};
    if (node) {
      node.textContent = `${job.status}: проверено ${p.checked ?? 0}, обработано ${p.processed ?? 0}, осталось ${p.left ?? 0}, ситуаций ${p.situations ?? 0}`;
    }
    if (job.status === "done" || job.status === "done_cleanup_pending" || job.status === "error") {
      packJobId = "";
      fillHistory();
      return;
    }
    setTimeout(pollPackJob, 1200);
  } catch {
    setTimeout(pollPackJob, 2000);
  }
}

async function fillHistory() {
  const params = new URLSearchParams({
    packed: "open",
    offset: String(historyOffset),
    limit: "60",
  });
  try {
    // скан папок ботов → актуальный список файлов
    await fetch("/api/history/rebuild-index", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
    const gamesRes = await fetch(`/api/history/games?${params}`);
    const gamesData = await gamesRes.json().catch(() => ({}));
    const root = document.querySelector("#hist-games");
    if (!gamesRes.ok) {
      if (root) root.innerHTML = `<p class="quiet">${escapeHtml(gamesData.error || "Ошибка")}</p>`;
    } else {
      const rows = gamesData.games || [];
      historyGamesCache = rows;
      if (root) {
        root.innerHTML = rows.length ? rows.map((g) => {
          const id = String(g.gameId || "");
          const file = g.file || `game_${id}.jsonl`;
          const outcome = historyOutcome(g.result);
          return `<article class="history-file-row">
            <div class="history-file-name" title="${escapeHtml(file)}">
              <b>${escapeHtml(file)}</b>
              <small class="quiet">${fmtTs(g.startedAt)}${outcome.html} · раздач ${escapeHtml(String(g.hands ?? 0))}</small>
            </div>
            <span class="history-file-bot" title="${escapeHtml(g.botId || "")}">
              <b class="history-login">${escapeHtml(g.login || g.botId || "?")}</b>
              ${g.agent
                ? `<span class="agent-chip tone-${agentTone(g.agent)}" title="Агент в этой партии: ${escapeHtml(agentLabel(g.agent))}">${escapeHtml(agentShort(g.agent))}</span>`
                : `<small class="quiet">агент неизвестен</small>`}
            </span>
            <span class="history-file-status status-${escapeHtml(g.status || "")}">${escapeHtml(g.status || "—")}</span>
            <span class="history-file-status status-${escapeHtml(g.reconstructionStatus || "none")}">${escapeHtml(g.reconstructionStatus || "—")}</span>
            <span class="history-file-size">${fmtBytes(g.bytes)}</span>
            <button class="ghost" type="button" data-view-game="${escapeHtml(id)}" data-bot="${escapeHtml(g.botId || "")}">Открыть</button>
          </article>`;
        }).join("") : `<p class="quiet">Файлов пока нет. Файл пишется <b>во время матча</b> (не при выключении). Перезапусти бота после обновления стола — старые процессы историю не пишут.</p>`;
      }
      updatePackHint(rows);
    }
  } catch (err) {
    const root = document.querySelector("#hist-games");
    if (root) root.innerHTML = `<p class="quiet">Ошибка загрузки истории</p>`;
  }
  try {
    const packsRes = await fetch(`/api/history/packs?q=${encodeURIComponent(packsQuery)}&offset=${packsOffset}&limit=30`);
    const packsData = await packsRes.json().catch(() => ({}));
    const root = document.querySelector("#hist-packs");
    if (!packsRes.ok) {
      if (root) root.innerHTML = `<p class="quiet">${escapeHtml(packsData.error || "Ошибка")}</p>`;
    } else {
      const rows = packsData.packs || [];
      if (root) {
        root.innerHTML = rows.length ? rows.map((p) => `
          <article class="history-card">
            <div class="history-card-main">
              <b>${escapeHtml(p.filename || p.packId || "")}</b>
              <span class="quiet">${fmtTs(p.createdAt)} · #${escapeHtml(String(p.daySeq || ""))} · ${escapeHtml(p.status || "")}</span>
              <span>игр ${escapeHtml(String(p.games ?? 0))} · ботов ${escapeHtml(String(p.bots ?? 0))} · решений ${escapeHtml(String(p.decisions ?? 0))} · ситуаций ${escapeHtml(String(p.situations ?? 0))} · ${fmtBytes(p.bytes)}</span>
              <span class="quiet">период ${fmtTs(p.period?.from)} — ${fmtTs(p.period?.to)}</span>
            </div>
            <div class="history-card-acts">
              <button class="ghost" type="button" data-view-pack="${escapeHtml(p.packId || "")}">Посмотреть</button>
              <a class="go" href="/api/history/packs/${encodeURIComponent(p.packId || "")}/download" data-pack-dl="${escapeHtml(p.packId || "")}">Скачать</a>
              ${p.cleanupStatus && p.cleanupStatus !== "done" ? `<button class="ghost" type="button" data-pack-cleanup="${escapeHtml(p.packId || "")}">Дочистить</button>` : ""}
            </div>
          </article>`).join("") : `<p class="quiet">Сборников пока нет.</p>`;
      }
    }
  } catch (err) {
    const root = document.querySelector("#hist-packs");
    if (root) root.innerHTML = `<p class="quiet">Ошибка загрузки сборников</p>`;
  }
  await fillHistoryCompare();
  if (historyViewer) {
    const vis = document.querySelector("#history-viewer");
    if (vis) {
      vis.classList.add("is-open");
      vis.innerHTML = renderHistoryViewer(historyViewer);
    }
  }
}

function readyGames(rows) {
  const list = Array.isArray(rows) && rows.length ? rows : historyGamesCache;
  return list.filter((g) => String(g.status) === "completed" && !g.packedIn && g.gameId);
}

function updatePackHint(rows) {
  if (Array.isArray(rows) && rows.length) historyGamesCache = rows;
  const ready = readyGames(historyGamesCache);
  const hint = document.querySelector("#hist-pack-hint");
  const btn = document.querySelector("#hist-pack");
  const bots = new Set(ready.map((g) => g.botId));
  if (hint) {
    hint.textContent = ready.length
      ? `${ready.length} готовых → один пакет · ${bots.size} бот.`
      : "нет готовых игр";
  }
  if (btn) btn.disabled = ready.length === 0;
}

async function fetchAllReadyGameIds() {
  const ids = [];
  const seen = new Set();
  let offset = 0;
  const limit = 100;
  for (let page = 0; page < 50; page += 1) {
    const params = new URLSearchParams({
      status: "completed",
      packed: "open",
      offset: String(offset),
      limit: String(limit),
    });
    const response = await fetch(`/api/history/games?${params}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "Не удалось получить список игр");
    const rows = data.games || [];
    for (const g of rows) {
      const id = String(g.gameId || "");
      if (!id || seen.has(id) || g.packedIn || g.status !== "completed") continue;
      seen.add(id);
      ids.push(id);
    }
    if (rows.length < limit) break;
    offset += limit;
  }
  return ids;
}

function trainSquadBots() {
  const want = new Set(trainSeats.filter(Boolean).map((l) => l.toLowerCase()));
  if (!want.size) {
    return bots.filter((b) => b.running && b.tablePrivate && (b.tableMode === "create" || b.tableMode === "join"));
  }
  return bots.filter((b) => want.has(String(b.login || "").toLowerCase()) && (b.running || b.stopping));
}

function trainProfileOptions(selected, used) {
  const opts = [`<option value="">— профиль —</option>`];
  const sel = String(selected || "").toLowerCase();
  for (const p of profiles) {
    const login = String(p.login || "");
    if (!login) continue;
    const key = login.toLowerCase();
    const taken = used.has(key) && key !== sel;
    opts.push(
      `<option value="${escapeHtml(login)}" ${login === selected ? "selected" : ""} ${taken ? "disabled" : ""}>${escapeHtml(login)}</option>`
    );
  }
  return opts.join("");
}

function paintTrainSeatsOnly() {
  const used = new Set(trainSeats.filter(Boolean).map((l) => l.toLowerCase()));
  view.querySelectorAll("[data-train-seat]").forEach((select) => {
    const i = Number(select.dataset.trainSeat);
    const selected = trainSeats[i] || "";
    select.innerHTML = trainProfileOptions(selected, used);
  });
}

function trainHtml() {
  const squad = trainSquadBots();
  const used = new Set(trainSeats.filter(Boolean).map((l) => l.toLowerCase()));
  const labels = ["1 · хост (создаёт стол)", "2 · гость", "3 · гость"];
  const seatPicks = trainSeats.map((login, i) => `
    <label class="train-seat">
      <span class="cap">${labels[i]}</span>
      <span class="box"><span class="glyph">${icon(i === 0 ? "key" : "user")}</span>
        <select data-train-seat="${i}">${trainProfileOptions(login, used)}</select>
      </span>
    </label>`).join("");
  const agentOpts = (agentNames.length ? agentNames : ["fallen3", "fallen2", "fallen", "strong", "heuristic", "random"])
    .map((name) => `<option value="${escapeHtml(name)}" ${name === (trainAgent || name) ? "selected" : ""}>${escapeHtml(agentLabel(name))}</option>`)
    .join("");
  const pwd = trainPassword || trainLastPassword;
  const focus = squad.find((b) => b.login === trainFocusLogin) || squad.find((b) => b.tableMode === "create") || squad[0];
  if (focus && trainFocusLogin !== focus.login) trainFocusLogin = focus.login;
  const cards = squad.map((b) => {
    const t = b.table || {};
    const scores = (t.scores || []).join(" : ") || "—";
    const role = b.tableMode === "create" ? "хост" : "гость";
    const on = focus && focus.id === b.id ? " is-on" : "";
    return `<button type="button" class="train-card${on}" data-train-focus="${escapeHtml(b.login)}">
      <span class="train-role">${role}</span>
      <b>${escapeHtml(b.login)}</b>
      <small>${escapeHtml(PHASE[b.phase] || b.phase)} · ${escapeHtml(agentShort(b.agent))}</small>
      <span class="train-score">счёт ${escapeHtml(scores)}</span>
      <span class="train-meta">раздача ${escapeHtml(String(t.handId ?? "—"))} · взятки ${escapeHtml(String(t.tricks ?? 0))}</span>
      <a class="train-open" href="#/bot/${encodeURIComponent(b.id)}" onclick="event.stopPropagation()">открыть бота →</a>
    </button>`;
  }).join("") || `<div class="train-empty">Трио ещё не запущено. Выберите 3 профиля и нажмите «Сесть втроём».</div>`;
  return `
    <h1>Учение</h1>
    <p class="lead">Трое наших ботов за <b>приватным</b> столом: первый создаёт стол с паролем, двое подключаются. Смотрите ход, счёт и журналы — данные идут в историю для разбора.</p>
    <div class="train-layout">
      <form class="panel train-form" id="train-form">
        <div class="section-label">${icon("users")}<div><h2>Трое за столом</h2><small>Первый — хост. Только 3p, чужие не зайдут.</small></div></div>
        <div class="train-seats">${seatPicks}</div>
        <div class="fields train-fields">
          <label><span class="cap">Пароль стола</span><span class="box"><span class="glyph">${icon("lock")}</span>
            <input id="train-password" type="text" placeholder="пустой = сгенерируем" value="${escapeHtml(pwd)}" autocomplete="off">
          </span></label>
          <label><span class="cap">Агент</span><span class="box"><span class="glyph">${icon("bot")}</span>
            <select id="train-agent">${agentOpts}</select>
          </span></label>
        </div>
        <div class="points-row">
          <span class="cap">Партия до</span>
          <div class="points">
            ${[["51", "До 51"], ["101", "До 101"]].map(([v, label]) =>
              `<button type="button" class="point ${trainPoints === v ? "is-on" : ""}" data-train-points="${v}">${label}</button>`
            ).join("")}
          </div>
        </div>
        <p class="error" id="train-error">${escapeHtml(trainError)}</p>
        ${trainLastPassword ? `<p class="train-pwd-note">Пароль трио: <code>${escapeHtml(trainLastPassword)}</code></p>` : ""}
        <div class="actions">
          <button class="go" type="submit" id="train-start" ${trainBusy ? "disabled" : ""}>${icon("key")} Сесть втроём</button>
          <button class="stop" type="button" id="train-stop" ${squad.length ? "" : "disabled"}>Остановить трио</button>
        </div>
        <small class="quiet">История и decision-журналы пишутся как обычно — потом разбор missed/overpay и усиление агента.</small>
      </form>
      <section class="panel train-live">
        <div class="section-label">${icon("table")}<div><h2>Живой стол</h2><small>Клик по карточке — чей стол смотреть</small></div></div>
        <div class="train-cards" id="train-cards">${cards}</div>
        <div id="train-table-slot">${focus ? liveTableHtml(focus) : `<div class="live-empty">Нет снимка — запустите трио.</div>`}</div>
      </section>
    </div>`;
}

async function loadTrainProfiles() {
  try {
    const response = await fetch("/api/profiles");
    const data = await response.json();
    profiles = data.profiles || [];
  } catch {
    profiles = profiles || [];
  }
}

async function onTrainStart(event) {
  event.preventDefault();
  if (trainBusy) return;
  trainError = "";
  const seats = trainSeats.map((l) => String(l || "").trim());
  if (seats.some((l) => !l) || new Set(seats.map((l) => l.toLowerCase())).size !== 3) {
    trainError = "Выберите три разных профиля.";
    const err = document.querySelector("#train-error");
    if (err) err.textContent = trainError;
    return;
  }
  trainBusy = true;
  const btn = document.querySelector("#train-start");
  if (btn) btn.disabled = true;
  try {
    const response = await fetch("/api/train/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        seats: seats.map((login) => ({ login })),
        agent: trainAgent || (document.querySelector("#train-agent")?.value) || "fallen3",
        points: trainPoints,
        tablePassword: (document.querySelector("#train-password")?.value || "").trim(),
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      trainError = data.error || "Не удалось запустить трио.";
    } else {
      trainLastPassword = data.tablePassword || "";
      trainPassword = trainLastPassword;
      trainFocusLogin = seats[0];
    }
  } catch (err) {
    trainError = String(err.message || err);
  }
  trainBusy = false;
  await refresh();
  paint();
}

async function onTrainStop() {
  const logins = trainSeats.filter(Boolean);
  const fallback = trainSquadBots().map((b) => b.login);
  const list = logins.length === 3 ? logins : fallback;
  if (!list.length) return;
  await fetch("/api/train/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ logins: list }),
  });
  await refresh();
  if (page === "train") paint();
}

function paint() {
  cursors = {};
  if (page === "home") view.innerHTML = homeHtml();
  else if (page === "bots") view.innerHTML = botsHtml();
  else if (page === "bot") view.innerHTML = botHtml(bots.find((bot) => bot.id === botPageId));
  else if (page === "train") view.innerHTML = trainHtml();
  else if (page === "day") view.innerHTML = dayHtml();
  else if (page === "stats") view.innerHTML = statsHtml();
  else if (page === "history") view.innerHTML = historyHtml();
  else view.innerHTML = homeHtml();
  if (page === "history") fillHistory();
  if (page === "day") fillDayStats();
  document.querySelector("#day-refresh")?.addEventListener("click", () => fillDayStats());
  if (page === "train") {
    loadTrainProfiles().then(() => {
      // re-fill selects after profiles load without wiping password/agent
      if (page !== "train") return;
      const pwd = document.querySelector("#train-password")?.value;
      const agent = document.querySelector("#train-agent")?.value;
      if (pwd != null) trainPassword = pwd;
      if (agent) trainAgent = agent;
      paintTrainSeatsOnly();
    });
    loadAgents().then(() => {
      if (!trainAgent && agentNames.includes("fallen3")) trainAgent = "fallen3";
      else if (!trainAgent) trainAgent = agentNames[0] || "fallen3";
      const pick = document.querySelector("#train-agent");
      if (pick && agentNames.length) {
        const selected = trainAgent;
        pick.replaceChildren(...agentNames.map((name) => {
          const option = document.createElement("option");
          option.value = name;
          option.textContent = agentLabel(name);
          return option;
        }));
        if ([...pick.options].some((o) => o.value === selected)) pick.value = selected;
      }
    }).catch(() => {});
    const form = document.querySelector("#train-form");
    if (form) form.addEventListener("submit", onTrainStart);
    document.querySelector("#train-stop")?.addEventListener("click", onTrainStop);
  }
  if (page === "bot") {
    const current = bots.find((bot) => bot.id === botPageId);
    if (current?.variant) {
      const prev = variant;
      variant = current.variant;
      loadAgents().then(() => {
        variant = prev;
        const pick = document.querySelector("#switch-agent");
        if (pick && agentNames.length) {
          const selected = pick.value;
          pick.replaceChildren(...agentNames.map((name) => {
            const option = document.createElement("option");
            option.value = name;
            option.textContent = agentLabel(name);
            return option;
          }));
          if ([...pick.options].some((option) => option.value === selected)) pick.value = selected;
          else if (current.agent) pick.value = current.agent;
        }
      }).catch(() => {});
    }
  }
  const form = document.querySelector("#form");
  if (form) {
    form.addEventListener("submit", onStart);
    loadAgents();
    loadProfiles();
    const pick = document.querySelector("#profile-pick");
    if (pick) pick.addEventListener("change", () => applyProfile(pick.value));
    const del = document.querySelector("#profile-delete");
    if (del) del.addEventListener("click", deleteSelectedProfile);
    if (form.login) form.login.addEventListener("input", () => fillProfilePick());
  }
  fillRows();
  fillCompare();
  const search = document.querySelector("#search");
  if (search) {
    search.addEventListener("input", () => { query = search.value; fillRows(); });
    document.querySelector("#format").addEventListener("change", (event) => { formatFilter = event.target.value; fillRows(); });
  }
  const quick = document.querySelector("#quick-start");
  if (quick) quick.addEventListener("click", onQuick);
}

function visibleLine(text) {
  // PLAY CARD дублирует [Trick] после принятия сервером — в ленте только кто/карта.
  return !/card conservation|AUDIT:|\[Audit\]|\[Sync\]|\[MSG\]|suppressed: IndexError|табло \{|\[Bot Action\] -> PLAY CARD/.test(text);
}

function dotKind(text) {
  if (/Error|CRITICAL|SEAT LOST|ошибка|критично|потеряно/i.test(text)) return "bad";
  if (/looking for another|Searching public|ищет/i.test(text)) return "wait";
  if (/Bridge|SDK|мост|связь/.test(text)) return "info";
  return "";
}

function ensureFeedEmpty(log, message) {
  if (log.children.length) return;
  const item = document.createElement("li");
  item.className = "empty";
  item.textContent = message;
  log.append(item);
}

function botStatusLabel(bot) {
  if (bot.switching) {
    const left = Number(bot.switchIn) || 0;
    return left > 0 ? `Смена ${left}с` : "Смена агента";
  }
  if (!bot.running) return "Выключен";
  if (bot.stopping || bot.phase === "ending") return "Встаёт";
  if (bot.phase === "playing") return "Играет";
  if (bot.phase === "starting") return "Старт";
  return "Ищет стол";
}

function homeBotList() {
  // Включённые и те, у кого идёт смена агента (пауза 10с).
  return bots.filter((bot) => bot.running || bot.switching);
}

function shortSeatName(name) {
  const raw = String(name || "").trim();
  if (!raw) return "—";
  if (/^мы$/i.test(raw)) return "мы";
  return raw.length > 12 ? `${raw.slice(0, 11)}…` : raw;
}

function partyStandings(stats) {
  const board = stats.scoreboard;
  const names = board && Array.isArray(board.names) && board.names.length
    ? board.names.map(String)
    : null;
  const values = String(stats.score || "")
    .split(":")
    .map((part) => part.trim())
    .filter((part) => part !== "");
  if (names && values.length) {
    return names.map((name, index) => ({
      name,
      value: values[index] != null ? values[index] : "—",
      us: /^мы$/i.test(name),
    }));
  }
  if (names && Array.isArray(board.rows) && board.rows.length) {
    const last = board.rows[board.rows.length - 1] || [];
    return names.map((name, index) => ({
      name,
      value: last[index] != null ? String(last[index]) : "—",
      us: /^мы$/i.test(name),
    }));
  }
  if (values.length) {
    return values.map((value, index) => ({
      name: index === 0 ? "мы" : `игрок ${index + 1}`,
      value,
      us: index === 0,
    }));
  }
  return [];
}

function botBlockHtml(bot) {
  const stats = viewStats(bot);
  const on = Boolean(bot.running);
  const wins = Number(stats.wins) || 0;
  const losses = Number(stats.losses) || 0;
  const standings = partyStandings(stats);
  const agentKey = bot.agent || "?";
  const agent = agentLabel(agentKey);
  const scores = standings.length
    ? `<div class="bot-block-standings">${standings.map((row) => `
        <div class="bot-standing ${row.us ? "is-us" : ""}">
          <span title="${escapeHtml(row.name)}">${escapeHtml(shortSeatName(row.name))}</span>
          <b>${escapeHtml(row.value)}</b>
        </div>`).join("")}</div>`
    : `<p class="bot-block-wait">Счёт появится после первой раздачи</p>`;
  return `
    <article class="bot-block ${on ? "is-on" : "is-off"} agent-${agentTone(agentKey)}" data-bot-block="${escapeHtml(bot.id)}">
      <header class="bot-block-head">
        <div class="bot-block-who">
          <b>${escapeHtml(bot.login)}</b>
          <div class="bot-block-meta">
            <span class="agent-chip tone-${agentTone(agentKey)}" title="${escapeHtml(agent)}">${escapeHtml(agentShort(agentKey))}</span>
            <span class="stat-chip win">побед ${wins}</span>
            ${losses ? `<span class="stat-chip loss">пор. ${losses}</span>` : ""}
            ${stats.hands ? `<span class="stat-chip deal">раздача ${stats.hands}</span>` : ""}
          </div>
        </div>
        <div class="bot-block-actions">
          <span class="bot-block-status ${on ? "on" : "off"}"><i></i>${botStatusLabel(bot)}</span>
          <a class="bot-block-open" href="#/bot/${escapeHtml(bot.id)}">${icon("out")} Открыть</a>
        </div>
      </header>
      <div class="bot-block-score">
        <span class="results-kicker">Счёт сейчас</span>
        ${scores}
      </div>
    </article>`;
}

function pullHomeFeed() {
  const root = document.querySelector("#bot-blocks");
  if (!root || page !== "home") return;
  const who = document.querySelector("#journal-who");
  const list = homeBotList();
  const playing = list.filter((bot) => bot.phase === "playing").length;
  if (who) {
    who.textContent = list.length
      ? `${list.length} вкл.` + (playing ? ` · ${playing} играют` : "")
      : "нет включённых ботов";
  }
  const clear = document.querySelector("#clear");
  if (clear) clear.disabled = list.length === 0;

  if (!list.length) {
    root.dataset.sig = "";
    root.innerHTML = `<p class="bot-blocks-empty">Нет включённых ботов — посадите профиль слева.</p>`;
    return;
  }

  const signature = list.map((bot) => {
    const stats = viewStats(bot);
    return [
      bot.id,
      bot.agent || "",
      bot.running ? 1 : 0,
      bot.phase,
      bot.stopping ? 1 : 0,
      bot.switching ? 1 : 0,
      bot.switchIn || 0,
      bot.switchAgent || "",
      stats.score || "",
      stats.hands || 0,
      stats.wins || 0,
      stats.losses || 0,
      (stats.scoreboard && stats.scoreboard.names || []).join(","),
    ].join(":");
  }).join("|");
  if (root.dataset.sig === signature) return;
  root.dataset.sig = signature;
  root.innerHTML = list.map((bot) => botBlockHtml(bot)).join("");
}

async function pullLog(id) {
  const log = document.querySelector("#log");
  if (!log || !id) return;
  const response = await fetch(`/api/bots/${id}?after=${cursors[id] || 0}`);
  if (!response.ok) return;
  const data = await response.json();
  rememberLines(id, (data.lines || []).map((row) => row.text), !(cursors[id] > 0));
  cursors[id] = data.next;
  const who = document.querySelector("#journal-who");
  if (who) who.textContent = `${data.login} · ${PHASE[data.phase]} · ${uptime(data)}`;
  if (data.lines.length) log.querySelector(".empty")?.remove();
  for (const row of data.lines) {
    if (!visibleLine(row.text)) continue;
    const item = document.createElement("li");
    const translated = ru(row.text);
    const tag = translated.match(/^(\[[^\]]+\])\s*([\s\S]*)$/);
    const isPlay = /\[ход\]/.test(translated) && tag && /:/.test(tag[2]);
    const tagClass = /ошиб|критич|потерян|нарушен/i.test(translated) ? "bad" : /\[ход\]/.test(translated) ? "game" : /\[связь\]|\[мост\]/.test(translated) ? "sdk" : "";
    if (isPlay) item.classList.add("is-play");
    const body = tag
      ? `<b class="tag ${tagClass}">${escapeHtml(tag[1])}</b> ${isPlay ? formatJournalBody(tag[2]) : escapeHtml(tag[2])}`
      : escapeHtml(translated);
    item.innerHTML = `<span class="when">${row.t}</span><i class="dot ${dotKind(row.text)}"></i><span class="log-body">${body}</span>`;
    log.append(item);
  }
  if (data.lines.length) log.scrollTop = log.scrollHeight;
  ensureFeedEmpty(log, "Журнал появится, когда бот сядет за стол.");
}


async function loadProfiles() {
  try {
    const response = await fetch("/api/profiles");
    const data = await response.json();
    profiles = data.profiles || [];
  } catch {
    profiles = [];
  }
  fillProfilePick();
}

function loginBusy(login) {
  const key = String(login || "").toLowerCase();
  if (!key) return false;
  return bots.some((bot) => bot.running && String(bot.login || "").toLowerCase() === key);
}

function fillProfilePick() {
  const select = document.querySelector("#profile-pick");
  if (!select) return;
  const current = select.value;
  select.innerHTML = `<option value="">Новый профиль…</option>` + profiles.map((row) => {
    const busy = loginBusy(row.login);
    const label = busy
      ? `${row.login} · в игре`
      : `${row.login} · ${agentLabel(row.agent || "strong")}`;
    return `<option value="${escapeHtml(row.login)}" ${busy ? "disabled" : ""} class="${busy ? "busy" : ""}" style="${busy ? "color:#c45a5a;font-weight:600" : ""}">${escapeHtml(label)}</option>`;
  }).join("");
  const stillOk = current && [...select.options].some((option) => option.value === current && !option.disabled);
  select.value = stillOk ? current : "";
  select.classList.toggle("is-busy", Boolean(current && loginBusy(current)));
  const del = document.querySelector("#profile-delete");
  if (del) del.disabled = !select.value;
  const form = document.querySelector("#form");
  const go = form && form.querySelector(".go");
  if (go && form) {
    const typedBusy = loginBusy(form.login.value);
    go.disabled = typedBusy;
    const error = document.querySelector("#error");
    if (typedBusy && error && !error.textContent) {
      error.textContent = "Этот аккаунт уже за столом. Сначала встаньте.";
    } else if (!typedBusy && error && error.textContent === "Этот аккаунт уже за столом. Сначала встаньте.") {
      error.textContent = "";
    }
  }
}

function applyProfile(login) {
  const form = document.querySelector("#form");
  if (!form) return;
  const select = document.querySelector("#profile-pick");
  if (login && loginBusy(login)) {
    const error = document.querySelector("#error");
    if (error) error.textContent = "Этот аккаунт уже за столом. Сначала встаньте.";
    if (select) {
      select.value = "";
      select.classList.add("is-busy");
    }
    return;
  }
  if (select) select.classList.remove("is-busy");
  const row = profiles.find((item) => item.login === login);
  const del = document.querySelector("#profile-delete");
  if (del) del.disabled = !row;
  if (!row) return;
  form.login.value = row.login;
  form.password.value = row.password;
  if (row.variant === "3" || row.variant === "4") {
    variant = row.variant;
    document.querySelectorAll("[data-variant]").forEach((btn) => {
      btn.classList.toggle("is-on", btn.dataset.variant === variant);
    });
    document.querySelectorAll("[data-points-row]").forEach((el) => {
      el.hidden = variant !== "3";
    });
  }
  if (variant === "3" && (row.points === "51" || row.points === "101" || row.points === "" || row.points === "all")) {
    points = row.points === "" ? "all" : row.points;
    document.querySelectorAll("[data-points]").forEach((btn) => {
      btn.classList.toggle("is-on", btn.dataset.points === points);
    });
  }
  const agent = form.querySelector("#agent");
  if (agent && row.agent) {
    if (![...agent.options].some((option) => option.value === row.agent)) {
      const option = document.createElement("option");
      option.value = row.agent;
      option.textContent = agentLabel(row.agent);
      agent.append(option);
    }
    agent.value = row.agent;
  }
  if (form.once) form.once.checked = Boolean(row.once);
  loadAgents();
}

async function deleteSelectedProfile() {
  const select = document.querySelector("#profile-pick");
  const form = document.querySelector("#form");
  const login = (select && select.value) || (form && form.login.value) || "";
  if (!login) return;
  if (!confirm(`Удалить профиль «${login}» из файла?`)) return;
  const response = await fetch("/api/profiles/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ login }),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = document.querySelector("#error");
    if (error) error.textContent = data.error || "Не удалось удалить.";
    return;
  }
  await loadProfiles();
  if (form) {
    form.login.value = "";
    form.password.value = "";
  }
  if (select) select.value = "";
  const del = document.querySelector("#profile-delete");
  if (del) del.disabled = true;
}

async function onStart(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const error = document.querySelector("#error");
  const warn = document.querySelector("#warn");
  error.textContent = "";
  warn.textContent = "";
  if (loginBusy(form.login.value)) {
    error.textContent = "Этот аккаунт уже за столом. Сначала встаньте.";
    fillProfilePick();
    return;
  }
  const button = form.querySelector(".go");
  button.disabled = true;
  const prevLabel = button.textContent;
  button.textContent = "Сажаю…";
  try {
    const response = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        login: form.login.value,
        password: form.password.value,
        variant,
        agent: form.agent.value,
        once: form.once.checked,
        points: variant === "3" ? points : "",
        reconnect: !form.once.checked,
        rejoinDelay: 5,
        maxRounds: 0,
        saveProfile: form.saveProfile ? form.saveProfile.checked : true,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      error.textContent = data.error || "Не удалось сесть.";
      if (warn) warn.textContent = "";
      return;
    }
    await loadProfiles();
    const pick = document.querySelector("#profile-pick");
    if (pick) pick.value = form.login.value;
    selectedId = data.id;
    cursors[data.id] = 0;
    if (data.warning) warn.textContent = data.warning;
    else if (warn) warn.textContent = "";
    document.querySelector("#stand").disabled = false;
    document.querySelector("#clear").disabled = false;
    await refresh();
  } catch (err) {
    error.textContent = err?.message || "Сеть: не удалось сесть.";
  } finally {
    button.textContent = prevLabel || "Сесть за стол";
    button.disabled = loginBusy(form.login.value);
  }
}

async function onQuick() {
  const pick = document.querySelector("#quick-account").value;
  const source = bots.find((bot) => bot.id === pick);
  if (!source) return;
  const response = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      login: source.login,
      password: source.password,
      variant,
      agent: source.agent === "random" ? "heuristic" : source.agent,
      once: false,
      reconnect: true,
      rejoinDelay: source.rejoinDelay || 5,
      maxRounds: source.maxRounds || 0,
      points: variant === "3" ? points : "",
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = document.querySelector("#quick-error");
    if (error) error.textContent = data.error || "Не удалось сесть.";
    return;
  }
  location.hash = `#/bot/${data.id}`;
}

async function refresh() {
  const response = await fetch("/api/bots");
  const data = await response.json();
  bots = data.bots || [];
  if (!selectedId && bots[0]) selectedId = bots[0].id;
  drawNav();
  fillRows();
  if (page === "home") fillProfilePick();
  if (page === "stats") fillCompare();
  if (page === "home") await pullHomeFeed();
  const current = bots.find((bot) => bot.id === (page === "bot" ? botPageId : selectedId));
  const readyTitle = document.querySelector("#ready-title");
  if (readyTitle) {
    readyTitle.textContent = current?.running ? PHASE[current.phase] : "Готов к игре";
    document.querySelector("#ready-note").textContent = current?.running
      ? `${current.login} за столом.`
      : "Бот подключен и ожидает посадки за выбранный стол.";
    const stand = document.querySelector("#stand");
    if (stand) stand.disabled = !current?.running;
    const clear = document.querySelector("#clear");
    if (clear && page === "home") clear.disabled = bots.length === 0;
  }
  if (page === "bot") {
    const exists = bots.some((bot) => bot.id === botPageId);
    if (exists && !view.querySelector(".stats-row")) paint();
    const bot = bots.find((item) => item.id === botPageId);
    await pullLog(botPageId);
    if (bot) {
      const switchWrap = view.querySelector("[data-switch-wrap]");
      const wasSwitching = Boolean(switchWrap && switchWrap.dataset.switching === "1");
      if (switchWrap) switchWrap.dataset.switching = bot.switching ? "1" : "0";
      if (wasSwitching !== Boolean(bot.switching) || (bot.switching && !view.querySelector("[data-cancel-switch]"))) {
        paint();
      } else if (bot.switching) {
        const banner = view.querySelector(".stop-banner span:last-child");
        if (banner) {
          banner.textContent = `Пауза ${Number(bot.switchIn) || 0}с · дальше ${agentShort(bot.switchAgent || "")}`;
        }
      }
      const tableSig = `${bot.tableAt || 0}:${bot.marks || 0}:${JSON.stringify(bot.table?.trick || [])}:${JSON.stringify(bot.table?.recentTricks || [])}:${(bot.table?.hand || []).map((c) => c.id).join(",")}:${JSON.stringify(bot.table?.declarations || [])}:${bot.table?.trumpName || ""}:${bot.table?.trumpOpen ? 1 : 0}`;
      const liveRoot = view.querySelector(".area-table");
      if (liveRoot && liveRoot.dataset.sig !== tableSig) {
        const note = document.querySelector("#mark-note")?.value || "";
        const better = document.querySelector("#mark-better")?.value || "";
        const betterCard = document.querySelector("#mark-better-card")?.value || "";
        const tmp = document.createElement("div");
        tmp.innerHTML = liveTableHtml(bot);
        const next = tmp.firstElementChild;
        if (next) {
          next.dataset.sig = tableSig;
          liveRoot.replaceWith(next);
          const noteEl = document.querySelector("#mark-note");
          if (noteEl) noteEl.value = note;
          const betterEl = document.querySelector("#mark-better");
          if (betterEl) betterEl.value = better;
          const cardEl = document.querySelector("#mark-better-card");
          if (cardEl) cardEl.value = betterCard;
          if (betterCard) {
            view.querySelectorAll(".live-hand .live-card").forEach((node) => {
              node.classList.toggle("is-pick", node.dataset.cardToken === betterCard);
            });
          }
        }
      } else if (liveRoot) {
        liveRoot.dataset.sig = tableSig;
      }
      const clockNode = view.querySelector("[data-clock]");
      if (clockNode) clockNode.textContent = uptime(bot);
      const phase = view.querySelector("[data-phase]");
      if (phase) {
        phase.textContent = PHASE[bot.phase];
        phase.className = phaseClass(bot.phase);
      }
      const stats = viewStats(bot);
      const finished = (stats.wins || 0) + (stats.losses || 0);
      const percent = stats.tracked && finished ? `${Math.round((stats.wins / finished) * 100)}%` : "—";
      const values = {
        hands: stats.tracked ? (stats.hands || 0) : "—",
        wins: dash(stats.wins, stats.tracked),
        losses: dash(stats.losses, stats.tracked),
        percent,
        score: stats.tracked ? (stats.score || "—") : "—",
        tables: stats.tables || 0,
        lost: stats.lostSeats || 0,
        hand: stats.tracked ? (stats.handPoints || "—") : "—",
        combos: stats.tracked ? (stats.combos || "—") : "—",
        capot: stats.tracked ? (stats.capot || "—") : "—",
      };
      Object.entries(values).forEach(([key, value]) => {
        const node = view.querySelector(`[data-stat="${key}"]`);
        if (node) node.textContent = value;
      });
      const note = view.querySelector('[data-stat="note"]');
      if (note) {
        const prev = note.querySelector(".score-table-wrap");
        const expanded = Boolean(prev && prev.dataset.expanded === "1");
        note.innerHTML = resultsBoard(stats, expanded);
      }
    }
  }
  if (page === "stats") {
    const map = { hands: sum("hands"), tables: sum("tables"), actions: sum("actions"), lost: sum("lostSeats"), running: bots.filter((bot) => bot.running).length };
    Object.entries(map).forEach(([key, value]) => {
      const node = view.querySelector(`[data-stat="${key}"]`);
      if (node) node.textContent = value;
    });
  }
  if (page === "bots") {
    const running = bots.filter((bot) => bot.running).length;
    const meter = document.querySelector("#meter-label");
    if (meter) {
      meter.textContent = `${running} из 100 ботов запущено`;
      document.querySelector("#meter-bar").style.width = `${running ? Math.max(running, 4) : 0}%`;
      document.querySelector("#sum-running").textContent = `${running} из 100`;
      document.querySelector("#sum-free").textContent = String(100 - running);
      document.querySelector("#sum-hands").textContent = String(sum("hands"));
      fillAccounts();
    }
  }
  if (page === "train") {
    const squad = trainSquadBots();
    const cards = document.querySelector("#train-cards");
    if (cards) {
      const focus = squad.find((b) => b.login === trainFocusLogin) || squad.find((b) => b.tableMode === "create") || squad[0];
      if (focus) trainFocusLogin = focus.login;
      cards.innerHTML = squad.length
        ? squad.map((b) => {
          const t = b.table || {};
          const scores = (t.scores || []).join(" : ") || "—";
          const role = b.tableMode === "create" ? "хост" : "гость";
          const on = focus && focus.id === b.id ? " is-on" : "";
          return `<button type="button" class="train-card${on}" data-train-focus="${escapeHtml(b.login)}">
            <span class="train-role">${role}</span>
            <b>${escapeHtml(b.login)}</b>
            <small>${escapeHtml(PHASE[b.phase] || b.phase)} · ${escapeHtml(agentShort(b.agent))}</small>
            <span class="train-score">счёт ${escapeHtml(scores)}</span>
            <span class="train-meta">раздача ${escapeHtml(String(t.handId ?? "—"))} · взятки ${escapeHtml(String(t.tricks ?? 0))}</span>
            <a class="train-open" href="#/bot/${encodeURIComponent(b.id)}" onclick="event.stopPropagation()">открыть бота →</a>
          </button>`;
        }).join("")
        : `<div class="train-empty">Трио ещё не запущено. Выберите 3 профиля и нажмите «Сесть втроём».</div>`;
      const stopBtn = document.querySelector("#train-stop");
      if (stopBtn) stopBtn.disabled = !squad.length || trainBusy;
    }
    const focus = squad.find((b) => b.login === trainFocusLogin) || squad[0];
    const slot = document.querySelector("#train-table-slot");
    if (slot && focus) {
      const tableSig = `${focus.tableAt || 0}:${JSON.stringify(focus.table?.trick || [])}:${JSON.stringify(focus.table?.scores || [])}:${focus.table?.handId || ""}`;
      if (slot.dataset.sig !== tableSig) {
        slot.innerHTML = liveTableHtml(focus);
        slot.dataset.sig = tableSig;
      }
    } else if (slot && !focus) {
      slot.innerHTML = `<div class="live-empty">Нет снимка — запустите трио.</div>`;
      slot.dataset.sig = "";
    }
  }
}

view.addEventListener("change", (event) => {
  const seat = event.target.closest?.("[data-train-seat]");
  if (seat) {
    const i = Number(seat.dataset.trainSeat);
    trainSeats[i] = seat.value || "";
    paintTrainSeatsOnly();
  }
  if (event.target.id === "train-agent") trainAgent = event.target.value || "";
  if (event.target.id === "train-password") trainPassword = event.target.value || "";
});

view.addEventListener("click", async (event) => {
  const trainFocus = event.target.closest("[data-train-focus]");
  if (trainFocus && !event.target.closest(".train-open")) {
    trainFocusLogin = trainFocus.dataset.trainFocus || "";
    if (page === "train") await refresh();
    return;
  }
  const trainPointsBtn = event.target.closest("[data-train-points]");
  if (trainPointsBtn) {
    trainPoints = trainPointsBtn.dataset.trainPoints || "101";
    view.querySelectorAll("[data-train-points]").forEach((btn) => {
      btn.classList.toggle("is-on", btn.dataset.trainPoints === trainPoints);
    });
    return;
  }
  const expandScore = event.target.closest("[data-expand-score], [data-collapse-score]");
  if (expandScore) {
    const wrap = expandScore.closest(".score-table-wrap");
    const table = wrap && wrap.querySelector(".score-table");
    if (wrap && table && wrap.dataset.scoreboard) {
      try {
        const board = JSON.parse(decodeURIComponent(wrap.dataset.scoreboard));
        const open = Boolean(expandScore.closest("[data-expand-score]"));
        wrap.dataset.expanded = open ? "1" : "0";
        table.innerHTML = renderScoreRows(board.names || [], board.rows || [], open);
      } catch { /* ignore bad board */ }
    }
    return;
  }
  const histTab = event.target.closest("[data-hist-tab]");
  if (histTab) {
    historyTab = histTab.dataset.histTab;
    paint();
    return;
  }
  if (event.target.id === "hist-refresh") {
    historyOffset = 0;
    fillHistory();
    return;
  }
  if (event.target.id === "hist-compare-refresh") {
    fillHistoryCompare();
    return;
  }
  if (event.target.id === "packs-refresh") {
    packsQuery = document.querySelector("#packs-q")?.value || "";
    packsOffset = 0;
    fillHistory();
    return;
  }
  if (event.target.id === "hist-prev") {
    historyOffset = Math.max(0, historyOffset - 40);
    fillHistory();
    return;
  }
  if (event.target.id === "hist-next") {
    historyOffset += 40;
    fillHistory();
    return;
  }
  if (event.target.id === "packs-prev") {
    packsOffset = Math.max(0, packsOffset - 30);
    fillHistory();
    return;
  }
  if (event.target.id === "packs-next") {
    packsOffset += 30;
    fillHistory();
    return;
  }
  if (event.target.id === "hist-pack") {
    event.target.disabled = true;
    let ids = [];
    try {
      ids = await fetchAllReadyGameIds();
    } catch (err) {
      event.target.disabled = false;
      alert(err.message || "Не удалось собрать список игр.");
      return;
    }
    if (!ids.length) {
      event.target.disabled = false;
      alert("Нет готовых завершённых игр для сборки.");
      return;
    }
    if (!confirm(`Собрать опыт из ${ids.length} готовых игр в один пакет?`)) {
      event.target.disabled = false;
      updatePackHint(historyGamesCache);
      return;
    }
    const key = `pack-${Date.now()}-${ids.slice().sort().join(",").slice(0, 120)}`;
    const response = await fetch("/api/history/pack", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gameIds: ids, idempotencyKey: key }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      event.target.disabled = false;
      updatePackHint(historyGamesCache);
      alert(data.error || "Не удалось запустить сборку.");
      return;
    }
    packJobId = data.job?.jobId || "";
    const node = document.querySelector("#hist-job");
    if (node) node.textContent = `сборка: ${ids.length} игр в один пакет…`;
    pollPackJob();
    return;
  }
  const viewGame = event.target.closest("[data-view-game]");
  if (viewGame) {
    const gid = viewGame.dataset.viewGame;
    const bot = viewGame.dataset.bot || "";
    const response = await fetch(`/api/history/games/${encodeURIComponent(gid)}?bot=${encodeURIComponent(bot)}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      alert(data.error || "Не удалось открыть игру.");
      return;
    }
    historyViewer = {
      meta: data.meta,
      events: data.events,
      handIndex: 0,
      gameId: gid,
      mode: "at_time",
      recon: data.reconstructed_hands || [],
      decisions: data.decisions || [],
    };
    const vis = document.querySelector("#history-viewer");
    if (vis) {
      vis.classList.add("is-open");
      vis.innerHTML = renderHistoryViewer(historyViewer);
      vis.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    return;
  }
  const viewMode = event.target.closest("[data-view-mode]");
  if (viewMode && historyViewer) {
    historyViewer.mode = viewMode.dataset.viewMode || "at_time";
    const vis = document.querySelector("#history-viewer");
    if (vis) vis.innerHTML = renderHistoryViewer(historyViewer);
    return;
  }
  const viewPack = event.target.closest("[data-view-pack]");
  if (viewPack) {
    const pid = viewPack.dataset.viewPack;
    const response = await fetch(`/api/history/packs?q=${encodeURIComponent(pid)}&limit=5`);
    const data = await response.json().catch(() => ({}));
    const pack = (data.packs || []).find((p) => p.packId === pid) || (data.packs || [])[0];
    if (!pack) {
      alert("Сборник не найден.");
      return;
    }
    const first = (pack.gameIds || [])[0];
    if (!first) {
      alert(`Сборник ${pack.filename}: игр ${pack.games}. Откройте скачанный JSON или выберите игру после скачивания.`);
      return;
    }
    const gRes = await fetch(`/api/history/packs/${encodeURIComponent(pid)}/games/${encodeURIComponent(first)}`);
    const gData = await gRes.json().catch(() => ({}));
    if (!gRes.ok) {
      alert(gData.error || "Не удалось открыть игру из сборника.");
      return;
    }
    historyViewer = {
      meta: gData.meta || gData,
      events: gData.events || [],
      handIndex: 0,
      gameId: first,
      packId: pid,
      mode: "at_time",
      recon: gData.reconstructed_hands || [],
      decisions: gData.decisions || [],
    };
    const vis = document.querySelector("#history-viewer");
    if (vis) {
      vis.classList.add("is-open");
      vis.innerHTML = renderHistoryViewer(historyViewer);
    }
    return;
  }
  const packDl = event.target.closest("[data-pack-dl]");
  if (packDl) {
    event.preventDefault();
    const pid = packDl.dataset.packDl;
    const response = await fetch(`/api/history/packs/${encodeURIComponent(pid)}/download`);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      alert(data.error || "Скачивание недоступно.");
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = (packDl.textContent || "pack").includes("опыт") ? `${pid}.json` : `pack_${pid}.json`;
    a.click();
    URL.revokeObjectURL(url);
    return;
  }
  const packCleanup = event.target.closest("[data-pack-cleanup]");
  if (packCleanup) {
    const response = await fetch(`/api/history/packs/${encodeURIComponent(packCleanup.dataset.packCleanup)}/cleanup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) alert(data.error || "Очистка не удалась.");
    fillHistory();
    return;
  }
  if (event.target.id === "hist-viewer-close") {
    historyViewer = null;
    const vis = document.querySelector("#history-viewer");
    if (vis) {
      vis.classList.remove("is-open");
      vis.innerHTML = `<p class="quiet">Откройте игру кнопкой «Посмотреть».</p>`;
    }
    return;
  }
  const handNav = event.target.closest("[data-hand-nav]");
  if (handNav && historyViewer) {
    historyViewer.handIndex = Math.max(0, (historyViewer.handIndex || 0) + Number(handNav.dataset.handNav || 0));
    const vis = document.querySelector("#history-viewer");
    if (vis) vis.innerHTML = renderHistoryViewer(historyViewer);
    return;
  }
  const seat = event.target.closest("[data-variant]");
  if (seat) {
    variant = seat.dataset.variant;
    view.querySelectorAll(".seat").forEach((item) => item.classList.toggle("is-on", item.dataset.variant === variant));
    view.querySelectorAll("[data-points-row]").forEach((row) => { row.hidden = variant !== "3"; });
    loadAgents();
  }
  const point = event.target.closest("[data-points]");
  if (point) {
    points = point.dataset.points;
    view.querySelectorAll("[data-points]").forEach((item) => item.classList.toggle("is-on", item.dataset.points === points));
  }
  const eye = event.target.closest("[data-eye]");
  if (eye) {
    const input = eye.parentElement.querySelector("input");
    input.type = input.type === "password" ? "text" : "password";
  }
  const reveal = event.target.closest("[data-reveal]");
  if (reveal) {
    const id = reveal.dataset.reveal;
    if (revealed.has(id)) revealed.delete(id);
    else revealed.add(id);
    if (page === "bot") paint();
    else fillRows();
  }
  const copy = event.target.closest("[data-copy]");
  if (copy) navigator.clipboard.writeText(copy.dataset.copy).catch(() => {});
  const stop = event.target.closest("[data-stop]");
  if (stop) {
    await fetch(`/api/bots/${stop.dataset.stop}/stop`, { method: "POST" });
    await refresh();
  }
  const cont = event.target.closest("[data-continue]");
  if (cont) {
    await fetch(`/api/bots/${cont.dataset.continue}/continue`, { method: "POST" });
    await refresh();
  }
  const histCard = event.target.closest(".live-history-cards .live-card");
  if (histCard && teachTrickN != null) {
    event.preventDefault();
    event.stopPropagation();
    view.querySelectorAll(".live-hand .live-card, .live-history-cards .live-card").forEach((node) => node.classList.remove("is-pick"));
    histCard.classList.add("is-pick");
    const input = document.querySelector("#mark-better-card");
    if (input && !input.disabled) input.value = histCard.dataset.cardToken || "";
    return;
  }
  const trickPick = event.target.closest("[data-trick-pick]");
  if (trickPick) {
    const n = Number(trickPick.dataset.trickPick);
    if (!Number.isFinite(n)) return;
    teachTrickN = Number(teachTrickN) === n ? null : n;
    const note = document.querySelector("#mark-note")?.value || "";
    const better = document.querySelector("#mark-better")?.value || "";
    const betterCard = document.querySelector("#mark-better-card")?.value || "";
    paint();
    const noteEl = document.querySelector("#mark-note");
    if (noteEl) {
      noteEl.value = note;
      if (teachTrickN != null) noteEl.focus();
    }
    const betterEl = document.querySelector("#mark-better");
    if (betterEl) betterEl.value = better;
    const cardEl = document.querySelector("#mark-better-card");
    if (cardEl) cardEl.value = betterCard;
    return;
  }
  const cardPick = event.target.closest(".live-hand .live-card");
  if (cardPick) {
    view.querySelectorAll(".live-hand .live-card, .live-history-cards .live-card").forEach((node) => node.classList.remove("is-pick"));
    cardPick.classList.add("is-pick");
    const input = document.querySelector("#mark-better-card");
    if (input && !input.disabled) input.value = cardPick.dataset.cardToken || "";
  }
  const markBtn = event.target.closest("[data-mark]");
  if (markBtn) {
    const id = markBtn.dataset.mark;
    const note = document.querySelector("#mark-note")?.value || "";
    const better = document.querySelector("#mark-better")?.value || "";
    const betterCard = document.querySelector("#mark-better-card")?.value || "";
    const bot = bots.find((row) => row.id === id);
    const aboutTrick = (bot?.table?.recentTricks || []).find((item) => Number(item.n) === Number(teachTrickN)) || null;
    if (!aboutTrick) {
      alert("Сначала выбери прошлую взятку.");
      return;
    }
    markBtn.disabled = true;
    const response = await fetch(`/api/bots/${id}/mark`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note, better, betterCard, aboutTrick }),
    });
    const data = await response.json().catch(() => ({}));
    markBtn.disabled = false;
    if (!response.ok) {
      alert(data.error || "Не удалось сохранить урок.");
      return;
    }
    teachTrickN = null;
    const noteEl = document.querySelector("#mark-note");
    if (noteEl) noteEl.value = "";
    const betterEl = document.querySelector("#mark-better");
    if (betterEl) betterEl.value = "";
    const cardEl = document.querySelector("#mark-better-card");
    if (cardEl) cardEl.value = "";
    view.querySelectorAll(".live-hand .live-card, .live-history-cards .live-card").forEach((node) => node.classList.remove("is-pick"));
    await refresh();
    if (page === "bot") paint();
  }
  const switchBtn = event.target.closest("[data-switch-agent]");
  if (switchBtn) {
    const id = switchBtn.dataset.switchAgent;
    const pick = document.querySelector("#switch-agent");
    const agent = pick && pick.value;
    if (!agent) return;
    switchBtn.disabled = true;
    const response = await fetch(`/api/bots/${id}/switch-agent`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      switchBtn.disabled = false;
      alert(data.error || "Не удалось сменить агента.");
      return;
    }
    await refresh();
    if (page === "bot") paint();
  }
  const cancelSwitch = event.target.closest("[data-cancel-switch]");
  if (cancelSwitch) {
    await fetch(`/api/bots/${cancelSwitch.dataset.cancelSwitch}/cancel-switch`, { method: "POST" });
    await refresh();
    if (page === "bot") paint();
  }
  if (event.target.id === "stand" && selectedId) {
    await fetch(`/api/bots/${selectedId}/stop`, { method: "POST" });
    await refresh();
  }
  if (event.target.closest?.("#clear")) {
    if (page === "home") {
      await Promise.all(bots.map(async (bot) => {
        await fetch(`/api/bots/${bot.id}/clear`, { method: "POST" });
        cursors[bot.id] = 0;
        remembered.delete(bot.id);
      }));
      pullHomeFeed();
      return;
    }
    const id = botPageId;
    if (!id) return;
    await fetch(`/api/bots/${id}/clear`, { method: "POST" });
    cursors[id] = 0;
    remembered.delete(id);
    const log = document.querySelector("#log");
    if (log) log.replaceChildren();
  }
});

async function show() {
  const next = route();
  const nextDay = next.page === "day" ? (next.id || "") : "";
  const nextBot = next.page === "bot" ? next.id : "";
  const changed = next.page !== page || nextBot !== botPageId || nextDay !== dayLogin;
  page = next.page;
  botPageId = nextBot;
  dayLogin = nextDay;
  drawNav();
  if (changed) paint();
  await refresh();
}

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-owner-mark]");
  if (!form) return;
  event.preventDefault();
  const status = form.querySelector(".owner-mark-status");
  const body = {
    gameId: form.dataset.gameId || "",
    decisionId: form.dataset.decisionId || "",
    handId: form.dataset.handId || "",
    ownerMark: form.ownerMark?.value || "",
    ownerPreferredCard: form.ownerPreferredCard?.value || "",
    ownerNote: form.ownerNote?.value || "",
  };
  try {
    const response = await fetch("/api/history/review-mark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (status) status.textContent = data.error || "ошибка (нужен admin)";
      return;
    }
    if (status) status.textContent = "сохранено";
  } catch (err) {
    if (status) status.textContent = String(err.message || err);
  }
});

window.addEventListener("hashchange", () => { show(); });
show();
setInterval(() => { refresh().catch(() => {}); }, 1000);
