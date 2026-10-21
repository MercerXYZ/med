"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const enc = encodeURIComponent;

// ---------- справочники

const SECTIONS = [
  ["1.1", "Определение заболевания или состояния"],
  ["1.2", "Этиология и патогенез"],
  ["1.3", "Эпидемиология"],
  ["1.4", "Особенности кодирования по МКБ"],
  ["1.5", "Классификация"],
  ["1.6", "Клиническая картина"],
  ["2.1", "Жалобы и анамнез"],
  ["2.2", "Физикальное обследование"],
  ["2.3", "Лабораторные диагностические исследования"],
  ["2.4", "Инструментальные диагностические исследования"],
  ["2.5", "Иные диагностические исследования"],
  ["3.1", "Консервативное лечение"],
  ["3.2", "Хирургическое лечение"],
  ["3.3", "Иное лечение"],
  ["4", "Медицинская реабилитация"],
  ["5", "Профилактика и диспансерное наблюдение"],
  ["6", "Организация оказания медицинской помощи"],
  ["7", "Дополнительная информация"],
];
const SECTION_TITLE = Object.fromEntries(SECTIONS);
const NARRATIVE = new Set(["1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "7"]);

const FACT_STATE = {
  verified: ["ok", "подтверждён"],
  needs_review: ["warn", "на проверке"],
  unverified: ["bad", "не подтверждён"],
  confirmed: ["ok", "подтверждён экспертом"],
  rejected: ["", "отклонён"],
};
const FACT_FILTERS = [
  ["verified", "Подтверждённые"],
  ["needs_review", "На проверке"],
  ["unverified", "Не подтверждённые"],
  ["rejected", "Отклонённые"],
];
const DOC_STATE = { done: "ok", done_empty: "ok", partial: "warn", error: "bad", unsupported: "bad", processing: "accent" };
const RUN_STATE = {
  running: ["accent", "Идёт"],
  completed: ["ok", "Готово"],
  completed_with_errors: ["warn", "Готово с ошибками"],
  failed: ["bad", "Ошибка"],
  stopped: ["", "Остановлено"],
};
const STAGE = {
  start: "Запуск",
  keywords: "Ключевые слова",
  extract: "Извлечение фактов",
  verify: "Проверка фактов",
  group: "Группировка",
  resolve: "Противоречия",
  kr: "Сборка КР",
  snapshot: "Сохранение",
  done: "Готово",
  failed: "Ошибка",
};
const KIND = {
  direction: "Противоположные выводы",
  numbers: "Несовместимые числа",
  population: "Разные группы пациентов",
  other: "Расхождение",
};
const DECISIONS = ["keep_both", "prefer_a", "prefer_b", "exclude"];
const DECISION = { keep_both: "Оставить оба", prefer_a: "Верно A", prefer_b: "Верно B", exclude: "Исключить оба" };
const PHRASES = ["Рекомендуется", "Рекомендуется рассмотреть возможность", "Не рекомендуется", "Не рекомендуется рассматривать"];
const CLAIM = { source_recommendation: "рекомендация источника", study_finding: "результат исследования", background_fact: "справочный факт" };
const POLARITY = { positive: "положительное", negative: "отрицательное", conditional: "условное", descriptive: "описательное" };
const AGE = { adults: "взрослые", children: "дети", mixed: "взрослые и дети", unspecified: "не указано" };
const AGE_CAT = { mixed: "Взрослые и дети", adults: "Взрослые", children: "Дети" };
const RELEVANCE = { direct: "прямая", indirect: "косвенная", none: "нет" };
const FACT_FIELDS = [
  ["section", "Раздел КР", (v) => secLabel(v)],
  ["claim_type", "Тип", (v) => CLAIM[v] || v],
  ["polarity", "Направление", (v) => POLARITY[v] || v],
  ["age_group", "Возраст", (v) => AGE[v] || v],
  ["population", "Пациенты"],
  ["intervention", "Вмешательство"],
  ["comparator", "Сравнение"],
  ["outcome", "Исход"],
  ["dose", "Доза"],
  ["timing", "Сроки"],
  ["thresholds", "Пороги"],
  ["conditions", "Условия"],
  ["topic_relevance", "Связь с темой", (v) => RELEVANCE[v] || v],
  ["secondary_citation", "Пересказ чужой работы", (v) => (v ? "да" : "")],
];
const FIELD_LABEL = {
  ...Object.fromEntries(FACT_FIELDS.map(([k, label]) => [k, label.toLowerCase()])),
  statement_ru: "формулировка", quote: "цитата", kr_section: "раздел КР", page: "страница",
};
// имя поля в форме (как в CARD_EDITABLE) → имя в ответе API
const FORM_KEYS = { statement_ru: "statement", kr_section: "section" };
const PAGE_SIZE = 50;

// ---------- состояние

const S = {
  projects: [], loaded: false, listError: null, selectKey: "",
  pid: null, info: null, infoError: null,
  route: { pid: null }, seq: 0, view: null, onPoll: null,
  pollTimer: null, wasRunning: false,
  docs: null, facts: null, fact: null, filter: emptyFilter(), factsReq: 0,
  conflicts: [], groups: new Map(), items: new Map(),
  dirty: false, krStale: false, focusGroup: null,
};

function emptyFilter() {
  return { status: "", section: "", doc: "", q: "" };
}

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* приватный режим */ } },
};

// ---------- запросы

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("Нет связи с сервером", 0);
  }
  if (!res.ok) throw new ApiError(await errorText(res), res.status);
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

async function errorText(res) {
  const raw = await res.text().catch(() => "");
  try {
    const d = JSON.parse(raw).detail;
    if (typeof d === "string" && d) return d;
    if (Array.isArray(d)) return d.map((x) => x.msg).join("; ");
  } catch { /* ответ не JSON */ }
  if (res.status === 404) return "Не найдено";
  if (res.status === 409) return "Обработка уже идёт";
  return `Ошибка сервера (${res.status})`;
}

const P = (rest = "") => `/api/projects/${enc(S.pid)}${rest}`;

// ---------- мелочи

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b === 1) return one;
  if (b >= 2 && b <= 4) return few;
  return many;
}
const count = (n, one, few, many) => `${n} ${plural(n, one, few, many)}`;
const short = (s, n) => { const t = String(s ?? ""); return t.length > n ? t.slice(0, n - 1) + "…" : t; };
const secLabel = (k) => (k ? `${k} ${SECTION_TITLE[k] || ""}`.trim() : "");
const stageLabel = (s) => STAGE[s] || s || "";
const numFmt = (v) => (v == null ? null : Number(v).toLocaleString("ru"));
const money = (v) => (v == null || isNaN(v) ? null : `$${Number(v).toFixed(2)}`);
const narrow = () => matchMedia("(max-width: 860px)").matches;

function toDate(t) {
  if (t == null || t === "") return null;
  const d = typeof t === "number" ? new Date(t < 1e12 ? t * 1000 : t) : new Date(t);
  return isNaN(d) ? null : d;
}
function fmtDate(t) {
  const d = toDate(t);
  return d ? d.toLocaleString("ru", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
}
function fmtTime(t) {
  const d = toDate(t);
  return d ? d.toLocaleTimeString("ru", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
}
function fmtDuration(from, to) {
  const a = toDate(from), b = toDate(to) || new Date();
  if (!a) return "—";
  const s = Math.max(0, Math.round((b - a) / 1000));
  if (s < 60) return `${s} с`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} мин` : `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

function pill(map, key, fallback = "") {
  const [cls, text] = map[key] || ["", fallback || key || "—"];
  return `<span class="pill ${cls}">${esc(text)}</span>`;
}
const factPill = (st) => pill(FACT_STATE, st);

function optionsHtml(pairs, cur, emptyLabel) {
  const val = cur == null ? "" : String(cur);
  const list = pairs.map(([v, t]) => [String(v), t]);
  if (val && !list.some(([v]) => v === val)) list.push([val, val]);
  if (emptyLabel != null || !val) list.unshift(["", emptyLabel ?? "—"]);
  return list.map(([v, t]) => `<option value="${esc(v)}"${v === val ? " selected" : ""}>${esc(t)}</option>`).join("");
}

const setMain = (html) => { $("#main").innerHTML = html; };
const loadingHtml = () => `<p class="loading">Загрузка…</p>`;
const errorHtml = (e) => `<div class="msg bad">${esc(e?.message || "Что-то пошло не так")}</div>`;
const emptyHtml = (title, text = "") =>
  `<div class="empty"><p class="empty-title">${esc(title)}</p>${text ? `<p>${esc(text)}</p>` : ""}</div>`;
const headHtml = (title, sub = "", extra = "") =>
  `<div class="view-head"><h1>${esc(title)}${extra}</h1>${sub ? `<p class="sub">${esc(sub)}</p>` : ""}</div>`;
const field = (label, control, hint = "") =>
  `<label class="field"><span>${esc(label)}</span>${control}${hint ? `<small>${esc(hint)}</small>` : ""}</label>`;

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.className = "toast"; }, 3500);
}

function flash(el) {
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function setBusy(el, on) {
  el.classList.toggle("busy", on);
  $$("button", el).forEach((b) => { b.disabled = on; });
}

function markDirty() {
  S.dirty = true;
  refreshInfo();
}

// ---------- маршруты

const VIEWS = Object.assign(Object.create(null), { pdf: viewDocs, facts: viewFacts, groups: viewGroups, kr: viewKr, history: viewHistory });

function parseHash() {
  let parts = [];
  try {
    parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  } catch { /* битая ссылка */ }
  if (parts[0] !== "p" || !parts[1]) return { pid: null };
  return { pid: parts[1], view: parts[2] || "", id: parts[3] || null };
}

const link = (view, id, pid = S.pid) => "#/p/" + [pid, view, id].filter(Boolean).map(enc).join("/");

function go(hash, replace = false) {
  if (replace) {
    history.replaceState(null, "", hash);
    route();
  } else if (location.hash === hash) route();
  else location.hash = hash;
}

async function route() {
  const r = parseHash();
  const seq = ++S.seq;
  S.onPoll = null;
  if (!S.loaded) await loadProjects();
  if (seq !== S.seq) return;
  if (!r.pid) return showHome();
  if (!VIEWS[r.view]) return go(link("pdf", null, r.pid), true);
  if (r.pid !== S.pid) {
    await openProject(r.pid);
    if (seq !== S.seq) return;
  }
  if (!S.info) return showProjectError();
  const prev = S.route;
  S.route = r;
  drawNav();
  try {
    await VIEWS[r.view](r, seq, prev);
  } catch (e) {
    if (seq === S.seq) setMain(errorHtml(e));
  }
}

function showHome() {
  const last = store.get("krgen.pid");
  const p = S.projects.find((x) => x.id === last) || S.projects[0];
  if (p) return go(link("pdf", null, p.id), true);
  clearTimeout(S.pollTimer);
  Object.assign(S, { pid: null, info: null, route: { pid: null }, view: null });
  drawHeader();
  if (S.listError) {
    return setMain(`<div class="hero">${errorHtml(S.listError)}<button class="btn" data-act="reload">Повторить</button></div>`);
  }
  setMain(`<div class="hero">
    <h1>Создайте проект</h1>
    <p class="muted">Проект — это одна клиническая рекомендация: тема, статьи и результат обработки.</p>
    <button class="btn primary" data-act="newProject">Новый проект</button>
  </div>`);
}

function showProjectError() {
  S.route = { pid: S.pid };
  drawHeader();
  if (S.infoError?.status === 404) {
    return setMain(`<div class="hero"><h1>Проект не найден</h1>
      <p class="muted">Возможно, ссылка устарела.</p><a class="btn" href="#/">К проектам</a></div>`);
  }
  setMain(`<div class="hero">${errorHtml(S.infoError)}<button class="btn" data-act="reload">Повторить</button></div>`);
}

// ---------- проект, шапка, прогон

async function loadProjects() {
  try {
    S.projects = await api("/api/projects");
    S.listError = null;
  } catch (e) {
    S.projects = [];
    S.listError = e;
  }
  S.loaded = true;
  S.selectKey = "";
  fillProjectSelect();
}

function fillProjectSelect() {
  const sel = $("#projectSel");
  const key = JSON.stringify([S.pid, S.projects.map((p) => [p.id, p.title])]);
  if (key === S.selectKey) return;
  S.selectKey = key;
  const seen = {};
  S.projects.forEach((p) => { seen[p.title] = (seen[p.title] || 0) + 1; });
  const label = (p) => (seen[p.title] > 1 ? `${p.title} · ${fmtDate(p.created_at)}` : p.title);
  let html = S.projects.map((p) => `<option value="${esc(p.id)}">${esc(short(label(p), 90))}</option>`).join("");
  if (S.pid && S.info && !S.projects.some((p) => p.id === S.pid)) {
    html = `<option value="${esc(S.pid)}">${esc(S.info.project?.title || S.pid)}</option>` + html;
  }
  sel.innerHTML = html;
  sel.value = S.pid || "";
  sel.hidden = !S.projects.length && !S.pid;
}

async function openProject(pid) {
  clearTimeout(S.pollTimer);
  Object.assign(S, {
    pid, info: null, infoError: null, view: null, docs: null, facts: null, fact: null,
    filter: emptyFilter(), dirty: false, krStale: false, wasRunning: false,
  });
  await refreshInfo();
  if (S.pid === pid && S.info) store.set("krgen.pid", pid);
}

async function refreshInfo() {
  const pid = S.pid;
  if (!pid) return;
  try {
    const d = await api(P());
    if (pid !== S.pid) return;
    S.info = d;
    S.infoError = null;
  } catch (e) {
    if (pid !== S.pid) return;
    S.infoError = e;
    if (e.status === 404) S.info = null;
  }
  drawHeader();
  schedulePoll();
}

function drawHeader() {
  fillProjectSelect();
  const has = !!S.info;
  ["#tabs", "#runBtn", "#historyLink"].forEach((s) => { $(s).hidden = !has; });
  if (!has) {
    $("#runInd").hidden = true;
    return;
  }
  drawNav();
  drawRun();
}

function drawNav() {
  $$("#tabs a[data-tab]").forEach((a) => {
    const t = a.dataset.tab;
    const on = S.route.view === t;
    a.href = link(t);
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  const h = $("#historyLink");
  h.href = link("history");
  h.classList.toggle("active", S.route.view === "history");
}

function drawRun() {
  const d = S.info, run = d.run, running = !!d.running;
  const noDocs = d.counts?.documents === 0;
  const btn = $("#runBtn");
  btn.disabled = running || noDocs;
  btn.textContent = running ? "Идёт обработка" : "Обработать";
  btn.title = noDocs && !running ? "Сначала загрузите PDF" : "";
  $$("[data-run]").forEach((b) => { b.disabled = running; });
  const el = $("#runInd");
  el.hidden = !run;
  if (!run) return;
  el.href = link("history", run.id);
  if (running) {
    const p = run.progress || {};
    const total = Number(p.total) || 0, done = Number(p.done) || 0;
    const pct = total ? Math.min(100, Math.round((100 * done) / total)) : 0;
    el.className = "run-ind running";
    el.title = "";
    el.innerHTML = `<span class="run-text">${esc(stageLabel(p.stage || run.stage))}${total ? ` · ${done} из ${total}` : ""}</span>
      <span class="bar${total ? "" : " indeterminate"}"><i style="width:${pct}%"></i></span>`;
    return;
  }
  const [cls, text] = RUN_STATE[run.status] || ["", run.status || ""];
  el.className = `run-ind ${cls}`;
  el.title = run.error ? short(run.error, 600) : run.finished_at ? `Завершено ${fmtDate(run.finished_at)}` : "";
  el.innerHTML = `<span class="run-text">${esc(text)}</span>`;
}

function schedulePoll() {
  clearTimeout(S.pollTimer);
  if (S.info?.running) {
    S.wasRunning = true;
    S.pollTimer = setTimeout(pollTick, 2000);
  } else if (S.wasRunning) {
    S.wasRunning = false;
    runFinished();
  }
}

async function pollTick() {
  await refreshInfo();
  if (S.info?.running && S.onPoll) S.onPoll();
}

function runFinished() {
  const failed = S.info?.run?.status === "failed";
  toast(failed ? "Обработка завершилась с ошибкой" : "Обработка завершена", failed ? "bad" : "");
  if (!failed) {
    S.dirty = false;
    S.krStale = false;
  }
  if ($(".editing")) {
    $("#main").insertAdjacentHTML("afterbegin",
      `<div class="notice"><span>Обработка завершена.</span><button class="btn small" data-act="refresh">Обновить окно</button></div>`);
    return;
  }
  S.view = null;
  route();
}

async function startRun(mode, extra = {}) {
  if (!S.pid) return;
  try {
    await api(P("/run"), { method: "POST", body: { mode, use_judge: true, only_failed: false, ...extra } });
    toast(mode === "kr" ? "Сборка КР запущена" : "Обработка запущена");
  } catch (e) {
    if (e.status !== 409) return toast(e.message, "bad");
    toast("Обработка уже идёт");
  }
  S.wasRunning = true;
  $("#runBtn").disabled = true;
  $$("[data-run]").forEach((b) => { b.disabled = true; });
  await refreshInfo();
  if (S.info?.running) S.onPoll?.();
}

// ---------- 1. Документы

async function viewDocs(r, seq) {
  S.view = "pdf";
  const p = S.info.project || {};
  const meta = [
    (p.icd_codes || []).length ? `МКБ-10: ${p.icd_codes.join(", ")}` : "",
    AGE_CAT[p.age_category] || "",
    p.developer_org || "",
  ].filter(Boolean).join(" · ");
  setMain(`<section class="view">
    ${headHtml("Документы", "Загрузите статьи в PDF и нажмите «Обработать».")}
    ${meta ? `<p class="muted small">${esc(meta)}</p>` : ""}
    <label class="drop" id="drop">
      <input type="file" id="fileInput" accept=".pdf,application/pdf" multiple>
      <span class="drop-title">Перетащите PDF сюда</span>
      <span class="drop-sub">или <span class="link-like">выберите файлы</span></span>
    </label>
    <div id="docsBox">${loadingHtml()}</div>
  </section>`);
  bindDrop();
  S.onPoll = () => refreshDocs();
  await refreshDocs(seq);
}

async function refreshDocs(seq = S.seq) {
  let docs;
  try {
    docs = await api(P("/documents"));
  } catch (e) {
    if (seq === S.seq && $("#docsBox")) $("#docsBox").innerHTML = errorHtml(e);
    return;
  }
  if (seq !== S.seq || !$("#docsBox")) return;
  S.docs = docs;
  $("#docsBox").innerHTML = docs.length ? docsHtml(docs) : emptyHtml("Документов пока нет");
}

function docsHtml(docs) {
  const failed = docs.some((d) => d.status === "partial" || d.status === "error");
  const running = !!S.info?.running;
  return `${failed ? `<div class="bar-line"><span class="muted">Часть документов обработана не полностью.</span>
      <button class="btn small" data-act="runFailed" data-run${running ? " disabled" : ""}>Повторить сбойные фрагменты</button></div>` : ""}
    <div class="table-wrap"><table class="table">
      <thead><tr><th>Файл</th><th class="num">Страниц</th><th>Статус</th><th class="num">Фактов</th><th></th></tr></thead>
      <tbody>${docs.map(docRow).join("")}</tbody>
    </table></div>`;
}

function docRow(d) {
  const b = d.biblio || {}, pg = d.pages || {};
  const sub = [b.title ? d.filename : "", b.year, b.design_label].filter(Boolean).join(" · ");
  const pagesTip = [
    pg.done != null && `обработано ${pg.done}`,
    pg.references && `литература ${pg.references}`,
    pg.skipped && `пропущено ${pg.skipped}`,
    pg.no_text && `без текста ${pg.no_text}`,
    pg.error && `с ошибкой ${pg.error}`,
  ].filter(Boolean).join(", ");
  const showDetail = d.detail && ["partial", "error", "unsupported", "duplicate"].includes(d.status);
  const facts = Number(d.facts) || 0;
  return `<tr>
    <td><div class="doc-title">${esc(b.title || d.filename)}</div>${sub ? `<div class="muted small">${esc(sub)}</div>` : ""}</td>
    <td class="num" title="${esc(pagesTip)}">${esc(d.n_pages ?? "—")}</td>
    <td><span class="pill ${DOC_STATE[d.status] || ""}" title="${esc(d.detail || "")}">${esc(d.status_label)}</span>
      ${showDetail ? `<div class="muted small clamp">${esc(d.detail)}</div>` : ""}</td>
    <td class="num">${facts ? `<a href="${esc(link("facts"))}" data-act="docFacts" data-id="${esc(d.id)}">${facts}</a>` : "0"}</td>
    <td class="right"><a href="/api/docs/${enc(d.id)}/pdf" target="_blank" rel="noopener">Открыть PDF</a></td>
  </tr>`;
}

function bindDrop() {
  const drop = $("#drop"), input = $("#fileInput");
  input.addEventListener("change", () => {
    upload(input.files);
    input.value = "";
  });
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    upload(e.dataTransfer.files);
  });
}

async function upload(fileList) {
  const all = [...(fileList || [])];
  const files = all.filter((f) => /\.pdf$/i.test(f.name) || f.type === "application/pdf" || !f.type);
  if (!all.length) return;
  if (!files.length) return toast("Нужны файлы PDF", "bad");
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  const drop = $("#drop"), title = $(".drop-title", drop);
  drop.classList.add("busy");
  title.textContent = `Загрузка: ${count(files.length, "файл", "файла", "файлов")}…`;
  try {
    const res = await api(P("/upload"), { method: "POST", body: fd });
    toast(uploadSummary(res, all.length - files.length));
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    drop.classList.remove("busy");
    title.textContent = "Перетащите PDF сюда";
  }
  await refreshDocs();
  refreshInfo();
}

function uploadSummary(res, skipped) {
  const list = Array.isArray(res) ? res : [];
  const dup = list.filter((x) => x.status === "duplicate").length;
  const bad = list.filter((x) => x.status === "error" || x.status === "unsupported").length;
  const ok = list.length - dup - bad;
  const parts = [`Загружено: ${ok}`];
  if (dup) parts.push(`дубликатов: ${dup}`);
  if (bad) parts.push(`с ошибкой: ${bad}`);
  if (skipped) parts.push(`не PDF: ${skipped}`);
  return parts.join(", ");
}

// ---------- 2. Факты

async function viewFacts(r, seq, prev) {
  if (!(S.view === "facts" && prev.pid === r.pid && $("#factList"))) {
    S.view = "facts";
    setMain(factsLayout());
    bindFactFilters();
    loadFactDocs();
    loadFacts(true);
  }
  markSelectedFact(r.id);
  await showFact(r.id);
}

function factsLayout() {
  const f = S.filter;
  return `<section class="view facts-view" id="factsView">
    ${headHtml("Факты", "", ` <span class="count" id="factsCount"></span>`)}
    <div class="filters">
      <select id="fStatus" aria-label="Статус">${optionsHtml(FACT_FILTERS, f.status, "Все статусы")}</select>
      <select id="fSection" aria-label="Раздел КР">${optionsHtml(SECTIONS.map(([k, t]) => [k, `${k} ${t}`]), f.section, "Все разделы")}</select>
      <select id="fDoc" aria-label="Документ">${docOptions(f.doc)}</select>
      <input id="fQ" type="search" placeholder="Поиск по тексту" value="${esc(f.q)}" aria-label="Поиск">
    </div>
    <div class="split" id="factsSplit">
      <div class="list-pane"><ul class="fact-list" id="factList"></ul><div id="factMore"></div></div>
      <aside class="detail-pane" id="factDetail" aria-label="Факт"></aside>
    </div>
  </section>`;
}

function docOptions(cur) {
  const pairs = (S.docs || []).map((d) => [d.id, short(d.biblio?.title || d.filename, 70)]);
  if (cur && !pairs.some(([id]) => id === cur)) pairs.push([cur, "Выбранный документ"]);
  return optionsHtml(pairs, cur, "Все документы");
}

function bindFactFilters() {
  const apply = () => {
    S.filter = { status: $("#fStatus").value, section: $("#fSection").value, doc: $("#fDoc").value, q: $("#fQ").value.trim() };
    loadFacts(true);
  };
  ["#fStatus", "#fSection", "#fDoc"].forEach((s) => $(s).addEventListener("change", apply));
  let timer;
  $("#fQ").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(apply, 300);
  });
}

async function loadFactDocs() {
  try {
    S.docs = await api(P("/documents"));
  } catch {
    return;
  }
  const sel = $("#fDoc");
  if (sel) sel.innerHTML = docOptions(S.filter.doc);
}

async function loadFacts(reset) {
  const req = ++S.factsReq, pid = S.pid;
  const offset = reset || !S.facts ? 0 : S.facts.items.length;
  const qs = new URLSearchParams({ limit: PAGE_SIZE, offset });
  Object.entries(S.filter).forEach(([k, v]) => { if (v) qs.set(k, v); });
  if (reset) {
    $("#factList").innerHTML = `<li class="loading">Загрузка…</li>`;
    $("#factMore").innerHTML = "";
  } else {
    const b = $("#factMore button");
    if (b) b.disabled = true;
  }
  let d;
  try {
    d = await api(P(`/facts?${qs}`));
  } catch (e) {
    if (req !== S.factsReq || !$("#factList")) return;
    if (reset) $("#factList").innerHTML = "";
    $("#factMore").innerHTML = errorHtml(e);
    return;
  }
  if (req !== S.factsReq || pid !== S.pid || !$("#factList")) return;
  if (reset || !S.facts) S.facts = { items: [], total: 0 };
  S.facts.items.push(...(d.items || []));
  S.facts.total = d.total ?? S.facts.items.length;
  drawFactList();
}

function drawFactList() {
  const { items, total } = S.facts;
  $("#factsCount").textContent = total ? count(total, "факт", "факта", "фактов") : "";
  const filtered = Object.values(S.filter).some(Boolean);
  $("#factList").innerHTML = items.length
    ? items.map(factRow).join("")
    : `<li class="empty-li">${filtered ? "Ничего не найдено." : "Фактов пока нет. Загрузите PDF и нажмите «Обработать»."}</li>`;
  const rest = total - items.length;
  $("#factMore").innerHTML = rest > 0 ? `<button class="btn wide" data-act="moreFacts">Показать ещё (${rest})</button>` : "";
  markSelectedFact(S.route.id);
}

function factRow(f) {
  const meta = [f.section && `раздел ${f.section}`, f.filename, f.page && `с. ${f.page}`].filter(Boolean).join(" · ");
  return `<li><a class="fact-row" href="${esc(link("facts", f.id))}" data-fid="${esc(f.id)}">
    ${factPill(f.user_status || f.status)}
    <span class="fact-text">${esc(f.statement)}</span>
    ${meta ? `<span class="fact-meta">${esc(meta)}</span>` : ""}
  </a></li>`;
}

function markSelectedFact(id) {
  $$("#factList .fact-row").forEach((a) => a.classList.toggle("sel", a.dataset.fid === id));
  $("#factsSplit")?.classList.toggle("has-detail", !!id);
  $("#factsView")?.classList.toggle("has-detail", !!id);
}

function updateFactRow(full) {
  const items = S.facts?.items || [];
  const i = items.findIndex((x) => x.id === full.id);
  if (i < 0) return;
  items[i] = { ...items[i], statement: full.statement, section: full.section, page: full.page, status: full.status, user_status: full.user_status };
  const a = $(`#factList .fact-row[data-fid="${CSS.escape(full.id)}"]`);
  if (a) a.parentElement.outerHTML = factRow(items[i]);
  markSelectedFact(S.route.id);
}

async function showFact(id) {
  const box = $("#factDetail");
  if (!box) return;
  if (!id) {
    S.fact = null;
    box.innerHTML = `<p class="detail-empty">Выберите факт в списке.</p>`;
    return;
  }
  box.innerHTML = detailTop() + loadingHtml();
  if (narrow()) window.scrollTo(0, 0);
  let f;
  try {
    f = await api(`/api/facts/${enc(id)}`);
  } catch (e) {
    if (S.route.id === id && $("#factDetail")) {
      box.innerHTML = detailTop() + errorHtml(e.status === 404 ? new Error("Факт не найден") : e);
    }
    return;
  }
  if (S.route.id !== id || !$("#factDetail")) return;
  renderFact(f);
}

function renderFact(f) {
  S.fact = f;
  const box = $("#factDetail");
  box.innerHTML = factHtml(f);
  box.scrollTop = 0;
  $("#factForm").addEventListener("submit", (e) => {
    e.preventDefault();
    saveFact(e.target);
  });
  const img = $(".page-img", box);
  if (img) {
    img.addEventListener("error", () => {
      img.closest(".page-link").outerHTML = `<p class="muted">Не удалось показать страницу.</p>`;
    });
  }
}

const detailTop = () => `<div class="detail-top">
  <a class="back" href="${esc(link("facts"))}">‹ Все факты</a>
  <a class="close" href="${esc(link("facts"))}" title="Закрыть" aria-label="Закрыть">✕</a>
</div>`;

function factHtml(f) {
  const src = f.source || {};
  const docId = src.doc_id || f.doc_id;
  const page = Number(f.page) || null;
  const pdf = docId ? `/api/docs/${enc(docId)}/pdf${page ? `#page=${page}` : ""}` : "";
  const srcMeta = [src.year, src.design_label, src.doi && `DOI ${src.doi}`, src.title && src.filename]
    .filter(Boolean).join(" · ");
  const id = esc(f.id);
  const auto = FACT_STATE[f.status]?.[1] || f.status;
  return `${detailTop()}
    <div class="fact-pills">
      ${f.user_status ? `${factPill(f.user_status)}<span class="muted small">автопроверка: ${esc(auto)}</span>` : factPill(f.status)}
      ${f.edited ? `<span class="tag">изменён</span>` : ""}
    </div>
    ${f.status !== "verified" && !f.user_status ? `<p class="muted small">В группы и КР идут только подтверждённые факты.</p>` : ""}
    ${f.pending ? factPendingHtml(f) : ""}
    <p class="fact-statement">${esc(f.statement)}</p>
    ${f.quote ? `<blockquote class="quote">${esc(f.quote)}</blockquote>` : ""}
    <div class="actions">
      <button class="btn ok-btn" data-act="factStatus" data-id="${id}" data-status="confirmed" aria-pressed="${f.user_status === "confirmed"}">Подтвердить</button>
      <button class="btn bad-btn" data-act="factStatus" data-id="${id}" data-status="rejected" aria-pressed="${f.user_status === "rejected"}">Отклонить</button>
      ${f.user_status ? `<button class="btn ghost" data-act="factStatus" data-id="${id}" data-status="">Снять решение</button>` : ""}
      <button class="btn ghost" data-act="toggleFactEdit" aria-expanded="false" aria-controls="factForm">Изменить</button>
    </div>
    ${factFormHtml(f)}
    ${docId && page ? `<h3>Страница ${page}</h3>
      <a class="page-link" href="${esc(pdf)}" target="_blank" rel="noopener">
        <img class="page-img" src="/api/docs/${enc(docId)}/page/${page}.png?card=${enc(f.id)}" alt="Страница ${page} с выделенной цитатой" loading="lazy">
      </a>` : ""}
    <h3>Источник</h3>
    <p class="src">${esc(src.title || src.filename || "—")}${srcMeta ? `<span class="muted small">${esc(srcMeta)}</span>` : ""}</p>
    ${pdf ? `<p><a href="${esc(pdf)}" target="_blank" rel="noopener">Открыть PDF${page ? ` на странице ${page}` : ""}</a></p>` : ""}
    <h3>Проверки</h3>
    ${checksHtml(f)}
    ${factDetailsHtml(f)}
    ${f.group_id ? `<p class="group-link"><a href="${esc(link("groups"))}" data-act="openGroup" data-id="${esc(f.group_id)}">Группа этого факта ›</a></p>` : ""}`;
}

function factPendingHtml(f) {
  const p = f.pending;
  const other = Object.keys(p).filter((k) => k !== "statement_ru").map((k) => FIELD_LABEL[k] || k);
  return `<div class="notice warn"><div>
      <div>После повторной обработки есть новая версия факта.</div>
      ${p.statement_ru ? `<div class="small">Новая формулировка: ${esc(p.statement_ru)}</div>` : ""}
      ${other.length ? `<div class="small muted">Изменились также: ${esc(other.join(", "))}</div>` : ""}
    </div>
    <div class="actions">
      <button class="btn small" data-act="factPending" data-id="${esc(f.id)}" data-accept="1">Принять новую</button>
      <button class="btn small ghost" data-act="factPending" data-id="${esc(f.id)}" data-accept="">Оставить мою</button>
    </div></div>`;
}

async function factPending(btn) {
  const accept = !!btn.dataset.accept;
  try {
    const full = await api(`/api/facts/${enc(btn.dataset.id)}/pending`, { method: "POST", body: { accept } });
    toast(accept ? "Принята новая версия" : "Оставлена ваша правка");
    applyFact(full);
    markDirty();
  } catch (e) {
    toast(e.message, "bad");
  }
}

function checksHtml(f) {
  const mark = { ok: "✓", warn: "~", bad: "✕" };
  const row = (cls, text) => `<li class="${cls}"><span class="mark" aria-hidden="true">${mark[cls]}</span><span>${esc(text)}</span></li>`;
  const items = f.checks.map((c) => row(c.ok == null ? "warn" : c.ok ? "ok" : "bad", c.text));
  (f.judge?.issues || []).forEach((i) => items.push(`<li class="sub">${esc(i)}</li>`));
  return `<ul class="checks">${items.join("")}</ul>`;
}

function factDetailsHtml(f) {
  const rows = FACT_FIELDS.map(([k, label, fmt]) => {
    const v = fmt ? fmt(f[k]) : f[k];
    return v == null || v === "" ? "" : `<dt>${esc(label)}</dt><dd>${esc(v)}</dd>`;
  }).join("");
  return rows ? `<details class="fact-more"><summary>Подробности</summary><dl class="kv">${rows}</dl></details>` : "";
}

function factFormHtml(f) {
  const sel = (name, map, val) => `<select name="${name}">${optionsHtml(Object.entries(map), val)}</select>`;
  const inp = (name, label) => field(label, `<input name="${name}" value="${esc(f[name] ?? "")}" autocomplete="off">`);
  return `<form id="factForm" class="edit-form" hidden>
    ${field("Формулировка", `<textarea name="statement_ru" rows="3">${esc(f.statement)}</textarea>`)}
    ${field("Цитата", `<textarea name="quote" rows="3">${esc(f.quote)}</textarea>`)}
    <div class="grid2">
      ${field("Раздел КР", `<select name="kr_section">${optionsHtml(SECTIONS.map(([k, t]) => [k, `${k} ${t}`]), f.section)}</select>`)}
      ${field("Страница", `<input name="page" type="number" min="1" value="${esc(f.page ?? "")}">`)}
      ${field("Тип", sel("claim_type", CLAIM, f.claim_type))}
      ${field("Направление", sel("polarity", POLARITY, f.polarity))}
      ${field("Возраст", sel("age_group", AGE, f.age_group))}
      ${field("Связь с темой", sel("topic_relevance", RELEVANCE, f.topic_relevance))}
      ${inp("population", "Пациенты")}
      ${inp("intervention", "Вмешательство")}
      ${inp("comparator", "Сравнение")}
      ${inp("outcome", "Исход")}
      ${inp("dose", "Доза")}
      ${inp("timing", "Сроки")}
      ${inp("thresholds", "Пороги")}
      ${inp("conditions", "Условия")}
    </div>
    <label class="check"><input type="checkbox" name="secondary_citation"${f.secondary_citation ? " checked" : ""}> Пересказ чужой работы</label>
    ${field("Комментарий", `<input name="comment" placeholder="необязательно" autocomplete="off">`)}
    <div class="actions">
      <button class="btn primary" type="submit">Сохранить</button>
      <button class="btn ghost" type="button" data-act="toggleFactEdit">Отмена</button>
    </div>
  </form>`;
}

function toggleFactEdit() {
  const form = $("#factForm");
  if (!form) return;
  const open = form.hidden;
  if (!open) form.reset();
  form.hidden = !open;
  form.classList.toggle("editing", open);
  $('[data-act="toggleFactEdit"][aria-expanded]')?.setAttribute("aria-expanded", String(open));
  if (open) $("textarea", form).focus();
}

async function saveFact(form) {
  const f = S.fact;
  if (!f) return;
  const data = new FormData(form);
  const changed = {};
  for (const [k, raw] of data.entries()) {
    if (k === "comment" || k === "secondary_citation") continue;
    const v = String(raw).trim();
    if (v !== String(f[FORM_KEYS[k] || k] ?? "").trim()) changed[k] = k === "page" ? (v ? Number(v) : null) : v;
  }
  const sc = form.elements.secondary_citation.checked;
  if (sc !== !!f.secondary_citation) changed.secondary_citation = sc;
  if (!Object.keys(changed).length) return toast("Изменений нет");
  if ("statement_ru" in changed && !changed.statement_ru) return toast("Формулировка не может быть пустой", "bad");
  setBusy(form, true);
  try {
    const full = await api(`/api/facts/${enc(f.id)}`, {
      method: "PATCH",
      body: { ...changed, comment: String(data.get("comment") || "").trim() },
    });
    toast("Сохранено");
    applyFact(full);
    markDirty();
  } catch (e) {
    toast(e.message, "bad");
    setBusy(form, false);
  }
}

async function setFactStatus(id, status) {
  try {
    const full = await api(`/api/facts/${enc(id)}/status`, { method: "POST", body: { status } });
    toast(status === "confirmed" ? "Факт подтверждён" : status === "rejected" ? "Факт отклонён" : "Решение снято");
    applyFact(full);
    markDirty();
  } catch (e) {
    toast(e.message, "bad");
  }
}

function applyFact(full) {
  updateFactRow(full);
  if (S.route.id === full.id && $("#factDetail")) renderFact(full);
}

// ---------- 3. Группы и противоречия

async function viewGroups(r, seq) {
  S.view = "groups";
  setMain(`<section class="view">
    ${headHtml("Группы и противоречия", "Похожие факты собраны в группы. Противоречия нужно решить до сборки КР.")}
    <div id="krHint"></div>
    <section class="block" id="conflictsBox"><h2>Противоречия</h2>${loadingHtml()}</section>
    <div id="groupsBox">${loadingHtml()}</div>
  </section>`);
  drawKrHint();
  const [c, g] = await Promise.allSettled([api(P("/conflicts")), api(P("/groups"))]);
  if (seq !== S.seq) return;
  if (c.status === "fulfilled") {
    S.conflicts = c.value;
    $("#conflictsBox").innerHTML = conflictsHtml(S.conflicts);
  } else {
    $("#conflictsBox").innerHTML = `<h2>Противоречия</h2>${errorHtml(c.reason)}`;
  }
  $("#groupsBox").innerHTML = g.status === "fulfilled" ? groupsHtml(g.value) : errorHtml(g.reason);
  focusGroup();
  api(P("/kr")).then((kr) => {
    if (seq !== S.seq) return;
    S.krStale = !!kr?.stale;
    drawKrHint();
  }).catch(() => {});
}

function drawKrHint() {
  const box = $("#krHint");
  if (!box) return;
  const show = S.dirty || S.krStale;
  box.innerHTML = show ? `<div class="notice"><span>Изменения вступят в силу после сборки КР.</span>
    <button class="btn small primary" data-act="buildKr" data-run${S.info?.running ? " disabled" : ""}>Собрать КР</button></div>` : "";
}

function conflictsHtml(list) {
  if (!list.length) {
    return `<div class="block-head"><h2>Противоречия</h2></div><p class="muted">Противоречий не найдено.</p>`;
  }
  const open = list.filter((c) => !c.decision), done = list.filter((c) => c.decision);
  return `<div class="block-head"><h2>Противоречия</h2><span id="openCount">${openCountHtml(open.length)}</span></div>
    ${open.length ? `<div class="conflicts">${open.map(conflictHtml).join("")}</div>` : ""}
    ${done.length ? `<details class="resolved"${open.length ? "" : " open"}>
      <summary>Решённые: ${done.length}</summary>
      <div class="conflicts">${done.map(conflictHtml).join("")}</div></details>` : ""}`;
}

const openCountHtml = (n) => (n ? `<span class="pill warn">ждут решения: ${n}</span>` : `<span class="pill ok">все решены</span>`);

function conflictHtml(c) {
  const d = c.decision;
  const [a, b] = { prefer_a: ["win", "lose"], prefer_b: ["lose", "win"], exclude: ["lose", "lose"] }[d] || ["", ""];
  const who = c.decided_by === "expert" ? "Решение эксперта" : "Предложено";
  const status = d
    ? `<span class="decision-label">${who}: ${esc(DECISION[d] || d)}</span>${c.reason ? `<span class="muted"> — ${esc(c.reason)}</span>` : ""}`
    : `<span class="decision-label warn">${esc(c.reason || "Нужно решение")}</span>`;
  return `<article class="conflict${d ? "" : " open"}">
    <div class="conflict-head">
      <span class="tag warn">${esc(KIND[c.kind] || KIND.other)}</span>
      ${c.section ? `<span class="muted small">${esc(secLabel(c.section))}</span>` : ""}
    </div>
    ${c.description ? `<p class="conflict-desc">${esc(c.description)}</p>` : ""}
    <div class="sides">${sideHtml("A", c.side_a, a)}${sideHtml("B", c.side_b, b)}</div>
    <div class="decision">${status}</div>
    <div class="seg" role="group" aria-label="Решение">
      ${DECISIONS.map((k) => `<button class="seg-btn${d === k ? " on" : ""}" data-act="decide" data-id="${esc(c.id)}" data-decision="${k}" aria-pressed="${d === k}">${DECISION[k]}</button>`).join("")}
    </div>
  </article>`;
}

function sideHtml(label, facts, state) {
  const note = state === "win" ? "входит в КР" : state === "lose" ? "не входит в КР" : "";
  const items = (facts || []).map((f) => {
    const meta = [factSource(f), f.udd != null && `УДД ${f.udd}`].filter(Boolean).join(" · ");
    return `<li><a href="${esc(link("facts", f.id))}">${esc(f.statement || f.id)}</a>${meta ? `<span class="muted small">${esc(meta)}</span>` : ""}</li>`;
  }).join("");
  return `<div class="side ${state}">
    <div class="side-label">${label}${note ? `<span class="side-state">${note}</span>` : ""}</div>
    <ul>${items || `<li class="muted">Нет фактов</li>`}</ul>
  </div>`;
}

function factSource(f) {
  const s = f.source;
  return [s.title ? short(s.title, 90) : s.filename, s.year, f.page && `с. ${f.page}`].filter(Boolean).join(", ");
}

async function decide(btn) {
  const id = btn.dataset.id, decision = btn.dataset.decision;
  const card = btn.closest(".conflict");
  setBusy(card, true);
  try {
    const res = await api(`/api/conflicts/${enc(id)}/decision`, { method: "POST", body: { decision } });
    S.conflicts[S.conflicts.findIndex((x) => x.id === id)] = res;
    card.outerHTML = conflictHtml(res);
    const n = S.conflicts.filter((c) => !c.decision).length;
    if ($("#openCount")) $("#openCount").innerHTML = openCountHtml(n);
    markDirty();
    drawKrHint();
  } catch (e) {
    toast(e.message, "bad");
    setBusy(card, false);
  }
}

function groupsHtml(data) {
  const secs = (data?.sections || []).filter((s) => (s.groups || []).length);
  S.groups = new Map();
  secs.forEach((s) => s.groups.forEach((g) => S.groups.set(g.id, g)));
  if (!secs.length) return emptyHtml("Групп пока нет", "Они появятся после обработки документов.");
  return secs.map((s) => `<section class="gsec">
    <div class="gsec-head"><h2>${esc(`${s.section} ${s.title}`)}</h2>
      <span class="muted small">${count(s.groups.length, "группа", "группы", "групп")}</span></div>
    <div class="groups">${s.groups.map(groupHtml).join("")}</div>
  </section>`).join("");
}

function groupHtml(g) {
  return `<article class="group${g.excluded ? " excluded" : ""}" id="g-${esc(g.id)}">
    <div class="group-body">
      <button type="button" class="g-statement" data-act="editGroup" data-id="${esc(g.id)}" title="Изменить формулировку">${esc(g.statement)}</button>
      <div class="g-meta">
        <span>${count(g.n_facts, "факт", "факта", "фактов")}</span>
        <span>${count(g.n_sources, "источник", "источника", "источников")}</span>
        ${g.n_in_kr !== g.n_facts && !g.excluded ? `<span>в КР: ${esc(g.n_in_kr)}</span>` : ""}
        ${g.edited ? `<span class="tag">изменено</span>` : ""}
        ${g.in_conflict ? `<span class="tag warn">есть противоречие</span>` : ""}
      </div>
      ${(g.facts || []).length ? `<details class="g-facts"><summary>Факты группы</summary>
        <ul>${g.facts.map(groupFactHtml).join("")}</ul></details>` : ""}
    </div>
    <label class="toggle"><input type="checkbox" data-change="excludeGroup" data-id="${esc(g.id)}"${g.excluded ? " checked" : ""}><span>Не включать в КР</span></label>
  </article>`;
}

function groupFactHtml(f) {
  const meta = [f.filename, f.page && `с. ${f.page}`].filter(Boolean).join(", ");
  const out = f.in_kr === false ? ` <span class="tag">не в КР</span>` : "";
  return `<li><a href="${esc(link("facts", f.id))}">${esc(f.statement || f.id)}</a>${meta ? ` <span class="muted small">${esc(meta)}</span>` : ""}${out}</li>`;
}

function focusGroup() {
  const id = S.focusGroup;
  S.focusGroup = null;
  const el = id && document.getElementById(`g-${id}`);
  if (!el) return;
  $("details", el)?.setAttribute("open", "");
  el.scrollIntoView({ block: "center" });
  flash(el);
}

function editGroup(el) {
  if (String(getSelection())) return;
  const g = S.groups.get(el.dataset.id);
  if (!g) return;
  const id = esc(g.id);
  const box = document.createElement("div");
  box.className = "g-edit editing";
  box.innerHTML = `<textarea rows="3" aria-label="Формулировка группы">${esc(g.statement)}</textarea>
    ${g.edited && g.auto_statement ? `<p class="muted small">Исходная: ${esc(g.auto_statement)}</p>` : ""}
    <div class="actions">
      <button class="btn primary small" data-act="saveGroup" data-id="${id}">Сохранить</button>
      <button class="btn ghost small" data-act="cancelGroup" data-id="${id}">Отмена</button>
      ${g.edited ? `<button class="btn ghost small" data-act="resetGroup" data-id="${id}">Вернуть исходную</button>` : ""}
    </div>`;
  el.replaceWith(box);
  const ta = $("textarea", box);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

async function saveGroup(btn, reset = false) {
  const id = btn.dataset.id, box = btn.closest(".g-edit");
  const text = reset ? "" : $("textarea", box).value.trim();
  if (!reset && !text) return toast("Формулировка не может быть пустой", "bad");
  if (!reset && text === S.groups.get(id)?.statement) return redrawGroup(id);
  setBusy(box, true);
  try {
    const g = await api(`/api/groups/${enc(id)}`, { method: "PATCH", body: { statement: text } });
    S.groups.set(id, g);
    redrawGroup(id);
    toast("Сохранено");
    markDirty();
    drawKrHint();
  } catch (e) {
    toast(e.message, "bad");
    setBusy(box, false);
  }
}

function redrawGroup(id) {
  const el = document.getElementById(`g-${id}`), g = S.groups.get(id);
  if (el && g) el.outerHTML = groupHtml(g);
}

async function excludeGroup(input) {
  const id = input.dataset.id, val = input.checked;
  const card = input.closest(".group");
  card.classList.toggle("excluded", val);
  try {
    const g = await api(`/api/groups/${enc(id)}`, { method: "PATCH", body: { excluded: val } });
    S.groups.set(id, g);
    markDirty();
    drawKrHint();
  } catch (e) {
    input.checked = !val;
    card.classList.toggle("excluded", !val);
    toast(e.message, "bad");
  }
}

// ---------- 4. Клиническая рекомендация

async function viewKr(r, seq) {
  S.view = "kr";
  const running = !!S.info?.running;
  const exp = (fmt) => esc(P(`/export/${fmt}`));
  setMain(`<section class="view">
    <div class="kr-toolbar">
      <button class="btn primary" data-act="buildKr" data-run${running ? " disabled" : ""}>Собрать КР заново</button>
      <div class="kr-exports">
        <a class="btn" href="${exp("docx")}" download>Скачать Word</a>
        <a class="btn ghost" href="${exp("md")}" download>Markdown</a>
        <a class="btn ghost" href="${exp("json")}" download>JSON</a>
        <a class="btn ghost" href="${exp("kr_schema")}" download>JSON по схеме kr_recommendation</a>
        <span class="schema-state" id="schemaState"></span>
      </div>
      <button class="btn ghost kr-add" data-act="addThesis">Добавить тезис</button>
    </div>
    <div id="krNotices"></div>
    <div id="krDoc">${loadingHtml()}</div>
  </section>`);
  await loadKr(seq);
  checkSchema(seq);
}

async function loadKr(seq = S.seq, keepScroll = false) {
  const y = window.scrollY;
  let kr;
  try {
    kr = await api(P("/kr"));
  } catch (e) {
    if (seq === S.seq && $("#krDoc")) $("#krDoc").innerHTML = errorHtml(e);
    return;
  }
  if (seq !== S.seq || !$("#krDoc")) return;
  S.krStale = !!kr.stale;
  S.items = new Map();
  (kr.sections || []).forEach((s) => (s.items || []).forEach((it) => {
    if (it.id && !it.empty) S.items.set(it.id, it);
  }));
  $("#krNotices").innerHTML = krNoticesHtml(kr);
  $("#krDoc").innerHTML = krDocHtml(kr);
  if (keepScroll) window.scrollTo(0, y);
}

const reloadKr = () => loadKr(S.seq, true);

function krNoticesHtml(kr) {
  const out = [];
  if (kr.stale) out.push(`<div class="notice"><span>Есть изменения после последней сборки.</span></div>`);
  const n = Number(kr.pending_conflicts) || 0;
  if (n) {
    const what = plural(n, "противоречие ждёт", "противоречия ждут", "противоречий ждут");
    out.push(`<div class="notice warn"><span>${n} ${what} решения — ${plural(n, "его", "их", "их")} факты не вошли в КР.</span>
      <a class="btn small" href="${esc(link("groups"))}">Перейти к противоречиям</a></div>`);
  }
  return out.join("");
}

function krDocHtml(kr) {
  const m = kr.meta || {};
  const row = (k, v) => (v ? `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>` : "");
  const head = `<header class="kr-title">
    <div class="kr-kicker">Клинические рекомендации</div>
    <h1>${esc(kr.title || S.info?.project?.title || "")}</h1>
    <dl class="kr-meta">
      ${row("Кодирование по МКБ-10", (m.icd_codes || []).join(", "))}
      ${row("Возрастная группа", AGE_CAT[m.age_category] || m.age_category)}
      ${row("Год", m.year)}
      ${row("Разработчик", m.developer_org)}
    </dl>
  </header>`;
  if (!S.items.size) {
    return `<article class="kr-doc">${head}<div class="kr-empty">${emptyHtml("В КР пока нет текста",
      "Обработайте документы или нажмите «Собрать КР заново».")}</div></article>`;
  }
  return `<article class="kr-doc">${head}${krSectionsHtml(kr.sections || [])}${criteriaHtml(kr.criteria)}${refsHtml(kr.references)}</article>`;
}

function krSectionsHtml(secs) {
  return secs.map((s) => {
    const h = s.level > 1 ? "h3" : "h2";
    return `<section class="kr-sec"><${h}>${esc(s.heading)}</${h}>${s.items.map(krItemHtml).join("")}</section>`;
  }).join("");
}

function krItemHtml(it) {
  if (it.empty || !it.id) return `<p class="muted">${esc(it.text)}</p>`;
  const thesis = it.type === "thesis";
  const text = thesis && (it.phrase || it.body)
    ? `${it.phrase ? `<strong>${esc(it.phrase)}</strong> ` : ""}${textWithRefs(it.body ?? "", it.refs)}`
    : inlineRefs(it.text);
  const marks = [
    it.status === "approved" ? `<span class="tag ok">утверждён</span>` : "",
    it.status === "rejected" ? `<span class="tag bad">отклонён</span>` : "",
    it.edited ? `<span class="tag">изменён</span>` : "",
    it.pending ? `<span class="tag warn">есть новая версия</span>` : "",
  ].join("");
  const flags = it.flags || [];
  return `<div class="kr-item ${thesis ? "thesis" : "text"} st-${esc(it.status || "draft")}" data-act="editItem" data-id="${esc(it.id)}">
    <button type="button" class="kr-edit-btn" data-act="editItem" data-id="${esc(it.id)}">Изменить</button>
    <p class="kr-text">${text}</p>
    ${thesis ? `<p class="kr-grade">${esc(it.grade)}</p>` : ""}
    ${it.comment ? `<p class="kr-comment"><span class="muted">Комментарий:</span> ${esc(it.comment)}</p>` : ""}
    ${marks ? `<div class="kr-marks">${marks}</div>` : ""}
    ${flags.length ? `<ul class="kr-flags">${flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

function textWithRefs(text, refs) {
  const t = String(text ?? "").trim();
  if (!refs?.length) return esc(t);
  const m = t.match(/^([\s\S]*?)([.!?…]*)$/);
  const links = refs.map((n) => `<a href="${esc(link("kr"))}" data-act="ref" data-n="${esc(n)}">${esc(n)}</a>`).join(", ");
  return `${esc(m[1])} <span class="refs">[${links}]</span>${esc(m[2] || ".")}`;
}

// в абзацах ссылки [1, 2] уже стоят в тексте после предложений
function inlineRefs(text) {
  return esc(String(text ?? "").trim()).replace(/\[(\d+(?:, ?\d+)*)\]/g, (_, list) => {
    const links = list.split(/, ?/).map((n) => `<a href="${esc(link("kr"))}" data-act="ref" data-n="${esc(n)}">${esc(n)}</a>`).join(", ");
    return `<span class="refs">[${links}]</span>`;
  });
}

function criteriaHtml(list) {
  if (!list?.length) return "";
  return `<section class="kr-sec"><h2>Критерии оценки качества медицинской помощи</h2>
    <div class="table-wrap"><table class="table criteria">
      <thead><tr><th class="num">№</th><th>Критерий качества</th><th>Оценка выполнения</th></tr></thead>
      <tbody>${list.map((c, i) => `<tr><td class="num">${i + 1}</td><td>${esc(c.text)}</td><td>Да / Нет</td></tr>`).join("")}</tbody>
    </table></div>
  </section>`;
}

function refsHtml(list) {
  if (!list?.length) return "";
  return `<section class="kr-sec"><h2>Список литературы</h2>
    <ol class="ref-list">${list.map(refHtml).join("")}</ol></section>`;
}

function refHtml(r) {
  return `<li id="ref-${esc(r.n)}" value="${esc(r.n)}">${esc(r.text)}</li>`;
}

function scrollToRef(n) {
  const el = document.getElementById(`ref-${n}`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  flash(el);
}

function editItem(el) {
  if (String(getSelection())) return;
  const box = el.closest(".kr-item");
  const it = box && S.items.get(box.dataset.id);
  if (!it) return;
  const thesis = it.type === "thesis";
  const src = thesis ? it.body : it.text;  // в абзаце ссылки [n] остаются на своих местах
  const ed = document.createElement("div");
  ed.className = "kr-edit editing";
  ed.dataset.id = it.id;
  const grades = `<div class="grid2">
    ${field("УДД", `<select name="udd">${optionsHtml([1, 2, 3, 4, 5].map((n) => [n, n]), it.udd, "—")}</select>`)}
    ${field("УУР", `<select name="uur">${optionsHtml(["A", "B", "C"].map((x) => [x, x]), it.uur, "—")}</select>`)}
  </div>`;
  ed.innerHTML = `${thesis ? field("Вводная фраза", `<select name="phrase">${optionsHtml(PHRASES.map((p) => [p, p]), it.phrase)}</select>`) : ""}
    ${field(thesis ? "Текст тезиса" : "Текст", `<textarea name="body" rows="${thesis ? 3 : 6}">${esc(src ?? "")}</textarea>`)}
    ${thesis ? grades : ""}
    ${thesis ? field("Комментарий", `<textarea name="comment" rows="2">${esc(it.comment ?? "")}</textarea>`) : ""}
    ${it.pending ? `<div class="notice warn"><span>После повторной обработки есть новая версия.</span>
      <div class="actions">
        <button class="btn small" data-act="itemPending" data-accept="1">Принять новую</button>
        <button class="btn small ghost" data-act="itemPending" data-accept="">Оставить мою</button>
      </div></div>` : ""}
    <div class="actions">
      <button class="btn primary small" data-act="saveItem">Сохранить</button>
      <button class="btn ok-btn small" data-act="itemStatus" data-status="approved">Утвердить</button>
      ${it.status !== "rejected" ? `<button class="btn bad-btn small" data-act="itemStatus" data-status="rejected">Отклонить</button>` : ""}
      ${it.status && it.status !== "draft" ? `<button class="btn ghost small" data-act="itemStatus" data-status="draft">Вернуть в черновик</button>` : ""}
      <button class="btn ghost small" data-act="cancelItem">Отмена</button>
    </div>`;
  box.replaceWith(ed);
  $("textarea", ed).focus();
}

function itemChanges(ed, it) {
  const out = {};
  const src = it.type === "thesis" ? it.body : it.text;
  const body = $("[name=body]", ed).value.trim();
  if (body !== String(src ?? "").trim()) out.body = body;
  const phrase = $("[name=phrase]", ed);
  if (phrase && phrase.value !== (it.phrase || "")) out.strength_phrase = phrase.value;
  const udd = $("[name=udd]", ed);
  if (udd && udd.value !== String(it.udd ?? "")) out.udd = udd.value ? Number(udd.value) : null;
  const uur = $("[name=uur]", ed);
  if (uur && uur.value !== String(it.uur ?? "")) out.uur = uur.value || null;
  const comment = $("[name=comment]", ed);
  if (comment && comment.value.trim() !== String(it.comment ?? "").trim()) out.comment = comment.value.trim();
  return out;
}

async function saveItemChanges(ed) {
  const it = S.items.get(ed.dataset.id);
  const fields = itemChanges(ed, it);
  if ("body" in fields && !fields.body) throw new Error("Текст не может быть пустым");
  if (!Object.keys(fields).length) return false;
  await api(`/api/theses/${enc(it.id)}`, { method: "PATCH", body: fields });
  return true;
}

async function saveItem(btn) {
  const ed = btn.closest(".kr-edit");
  setBusy(ed, true);
  try {
    if (!(await saveItemChanges(ed))) {
      toast("Изменений нет");
      setBusy(ed, false);
      return;
    }
    toast("Сохранено");
    await reloadKr();
  } catch (e) {
    toast(e.message, "bad");
    setBusy(ed, false);
  }
}

async function itemStatus(btn) {
  const ed = btn.closest(".kr-edit"), status = btn.dataset.status;
  setBusy(ed, true);
  try {
    if (status === "approved") await saveItemChanges(ed);
    await api(`/api/theses/${enc(ed.dataset.id)}/status`, { method: "POST", body: { status } });
    toast({ approved: "Утверждено", rejected: "Отклонено", draft: "Возвращено в черновик" }[status]);
    await reloadKr();
  } catch (e) {
    toast(e.message, "bad");
    setBusy(ed, false);
  }
}

async function itemPending(btn) {
  const ed = btn.closest(".kr-edit"), accept = !!btn.dataset.accept;
  setBusy(ed, true);
  try {
    await api(`/api/theses/${enc(ed.dataset.id)}/pending`, { method: "POST", body: { accept } });
    toast(accept ? "Принята новая версия" : "Оставлена ваша правка");
    await reloadKr();
  } catch (e) {
    toast(e.message, "bad");
    setBusy(ed, false);
  }
}

function cancelItem(btn) {
  const ed = btn.closest(".kr-edit"), id = ed.dataset.id, it = S.items.get(id);
  ed.outerHTML = it ? krItemHtml(it) : "";
  $(`.kr-edit-btn[data-id="${CSS.escape(id)}"]`)?.focus();
}

async function checkSchema(seq) {
  let res;
  try {
    res = await api(P("/export/kr_schema_check"));
  } catch {
    return;
  }
  const el = $("#schemaState");
  if (seq !== S.seq || !el) return;
  el.className = `schema-state ${res.valid ? "ok" : "warn"}`;
  el.textContent = res.valid ? "схема: без ошибок" : `схема: ${count(res.errors.length, "ошибка", "ошибки", "ошибок")}`;
  el.title = res.errors.slice(0, 20).join("\n");
}

// ---------- история

async function viewHistory(r, seq) {
  S.view = "history";
  if (r.id) return viewRun(r, seq);
  setMain(`<section class="view">
    ${headHtml("История", "Каждая обработка сохраняет снимок результата.")}
    <div id="runsBox">${loadingHtml()}</div>
  </section>`);
  S.onPoll = () => refreshRuns();
  await refreshRuns(seq);
}

async function refreshRuns(seq = S.seq) {
  let runs;
  try {
    runs = await api(P("/runs"));
  } catch (e) {
    if (seq === S.seq && $("#runsBox")) $("#runsBox").innerHTML = errorHtml(e);
    return;
  }
  if (seq !== S.seq || !$("#runsBox")) return;
  $("#runsBox").innerHTML = runs.length ? runsTableHtml(runs) : emptyHtml("Обработок ещё не было");
}

// в старых снимках: n_cards и cards, theses_in_draft или theses, conflicts — число
const SUM = {
  facts: (s) => s.n_facts ?? s.n_cards,
  theses: (s) => s.n_theses ?? s.theses_in_draft ?? s.theses,
};

function runsTableHtml(runs) {
  const cell = (v) => (v == null ? `<span class="muted">—</span>` : esc(v));
  const rows = runs.map((run) => {
    const s = run.summary || {}, u = s.run_usage || {}, href = esc(link("history", run.id));
    return `<tr class="clickable" data-act="goto" data-href="${href}">
      <td><a href="${href}">${esc(fmtDate(run.started_at))}</a></td>
      <td>${pill(RUN_STATE, run.status)}</td>
      <td class="num">${cell(s.n_documents)}</td>
      <td class="num">${cell(SUM.facts(s))}</td>
      <td class="num">${cell(s.n_groups)}</td>
      <td class="num">${cell(SUM.theses(s))}</td>
      <td class="num">${cell(numFmt(u.calls))}</td>
      <td class="num">${cell(money(u.cost_usd))}</td>
    </tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="table">
    <thead><tr><th>Дата</th><th>Статус</th><th class="num">Документы</th><th class="num">Факты</th>
      <th class="num">Группы</th><th class="num">Тезисы</th><th class="num">Запросы</th><th class="num">Стоимость</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

async function viewRun(r, seq) {
  setMain(`<section class="view">
    <a class="back" href="${esc(link("history"))}">‹ Вся история</a>
    <div id="runBox">${loadingHtml()}</div>
  </section>`);
  await loadRun(r.id, seq);
}

async function loadRun(id, seq) {
  let d;
  try {
    d = await api(`/api/runs/${enc(id)}`);
  } catch (e) {
    if (seq === S.seq && $("#runBox")) $("#runBox").innerHTML = errorHtml(e.status === 404 ? new Error("Обработка не найдена") : e);
    return;
  }
  if (seq !== S.seq || !$("#runBox")) return;
  const box = $("#runBox"), old = $(".log", box);
  // журнал: кто был внизу — остаётся внизу, иначе прежняя прокрутка
  const atEnd = old ? old.scrollTop + old.clientHeight >= old.scrollHeight - 8 : d.run.status === "running";
  const top = old?.scrollTop || 0;
  box.innerHTML = runDetailHtml(d.run, d.summary, d.log, d.files);
  const log = $(".log", box);
  if (log) log.scrollTop = atEnd ? log.scrollHeight : top;
  S.onPoll = d.run.status === "running" ? () => loadRun(id, S.seq) : null;
}

function runDetailHtml(run, s, log, files) {
  const running = run.status === "running";
  const p = run.progress || {};
  const [first, ...rest] = String(run.error || "").split("\n");
  const has = (name) => files.includes(name);
  const file = (name, label) => (has(name) ? `<a class="btn small" href="${esc(`/api/runs/${enc(run.id)}/file/${name}`)}" download>${label}</a>` : "");
  const links = [file("draft.docx", "Word"), file("draft.md", "Markdown"), file("export.json", "JSON")].join("");
  return `<div class="run-head"><h1>Обработка ${esc(fmtDate(run.started_at))}</h1>${pill(RUN_STATE, run.status)}</div>
    <p class="muted">${running
      ? `Сейчас: ${esc(stageLabel(p.stage || run.stage))}${p.total ? ` · ${esc(p.done || 0)} из ${esc(p.total)}` : ""}`
      : `Длительность: ${esc(fmtDuration(run.started_at, run.finished_at))}`}</p>
    ${first ? `<div class="msg bad"><div>${esc(first)}</div>${rest.length ? `<details><summary>Подробнее</summary><pre class="pre">${esc(rest.join("\n"))}</pre></details>` : ""}</div>` : ""}
    ${s ? `<div class="kpis">${runKpisHtml(s)}</div>
      ${links ? `<div class="downloads"><span class="muted">Снимок результата:</span>${links}</div>` : ""}`
      : `<p class="muted">${running ? "Снимок появится после завершения." : "Снимок результата не сохранён."}</p>`}
    <h2 class="sec-title">Журнал</h2>
    ${log.length ? `<ol class="log">${log.map(logRowHtml).join("")}</ol>` : `<p class="muted">Записей нет.</p>`}`;
}

function runKpisHtml(s) {
  const u = s.run_usage || {};
  const parts = (list) => list.filter(([, v]) => v != null && v !== 0).map(([k, v]) => `${k} ${v}`).join(" · ");
  const fs = s.facts || s.cards;
  const cs = s.n_conflicts != null ? s.conflicts : null;
  const tokens = u.input_tokens != null || u.output_tokens != null
    ? `${numFmt(u.input_tokens || 0)} / ${numFmt(u.output_tokens || 0)}` : null;
  const kpi = (k, v, sub) => `<div class="kpi"><span class="k">${esc(k)}</span><span class="v">${esc(v ?? "—")}</span>${sub ? `<span class="s">${esc(sub)}</span>` : ""}</div>`;
  return [
    kpi("Документы", s.n_documents),
    kpi("Факты", SUM.facts(s), fs && parts([
      ["подтверждено", fs.verified], ["экспертом", fs.confirmed], ["на проверке", fs.needs_review],
      ["не подтверждено", fs.unverified], ["отклонено", fs.rejected]])),
    kpi("Группы", s.n_groups, s.groups && parts([["исключено", s.groups.excluded]])),
    kpi("Противоречия", cs ? cs.total : s.conflicts, cs && parts([
      ["решено авто", cs.auto], ["экспертом", cs.expert], ["ждут", cs.open]])),
    kpi("Тезисы в КР", SUM.theses(s)),
    kpi("Запросы к модели", numFmt(u.calls), parts([["токены", tokens], ["стоимость", money(u.cost_usd)]])),
  ].join("");
}

const logRowHtml = (l) => `<li><time>${esc(fmtTime(l.ts))}</time><span class="stage">${esc(stageLabel(l.stage))}</span><span class="text">${esc(l.msg)}</span></li>`;

// ---------- окна

function openDialog(id) {
  const dlg = $(id);
  const err = $(".form-error", dlg);
  err.hidden = true;
  dlg.showModal();
  $("input, textarea", dlg)?.focus();
  return dlg;
}

function formError(form, msg) {
  const err = $(".form-error", form);
  err.textContent = msg;
  err.hidden = false;
}

function openThesisDialog() {
  const dlg = $("#thesisDlg"), sec = $("[name=section]", dlg);
  if (!sec.options.length) {
    sec.innerHTML = optionsHtml(SECTIONS.map(([k, t]) => [k, `${k} ${t}`]), "2.1");
  }
  openDialog("#thesisDlg");
  $("[name=body]", dlg).focus();
}

$("#projForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target, btn = $("[type=submit]", form), f = new FormData(form);
  const title = String(f.get("title") || "").trim();
  if (!title) return formError(form, "Укажите тему");
  btn.disabled = true;
  $(".form-error", form).hidden = true;
  try {
    const p = await api("/api/projects", {
      method: "POST",
      body: {
        title,
        age_category: f.get("age_category"),
        icd_codes: String(f.get("icd") || "").split(/[,;]/).map((x) => x.trim()).filter(Boolean),
        developer_org: String(f.get("org") || "").trim(),
      },
    });
    $("#projDlg").close();
    form.reset();
    S.loaded = false;
    go(link("pdf", null, p.id));
  } catch (ex) {
    formError(form, ex.message);
  } finally {
    btn.disabled = false;
  }
});

$("#thesisForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target, btn = $("[type=submit]", form), f = new FormData(form);
  const body = String(f.get("body") || "").trim();
  if (!body) return formError(form, "Введите текст тезиса");
  btn.disabled = true;
  $(".form-error", form).hidden = true;
  try {
    await api(P("/theses"), {
      method: "POST",
      body: { section: f.get("section"), phrase: f.get("phrase"), body, comment: String(f.get("comment") || "").trim() },
    });
    $("#thesisDlg").close();
    form.reset();
    toast("Тезис добавлен");
    if (S.view === "kr") reloadKr();
    refreshInfo();
  } catch (ex) {
    formError(form, ex.message);
  } finally {
    btn.disabled = false;
  }
});

$("#thesisForm [name=section]").addEventListener("change", (e) => {
  const phrase = $("#thesisForm [name=phrase]");
  if (NARRATIVE.has(e.target.value)) phrase.value = "";
  else if (!phrase.value) phrase.value = PHRASES[0];
});

// ---------- события

const ACT = {
  newProject: () => openDialog("#projDlg"),
  closeDlg: (el) => el.closest("dialog").close(),
  run: () => startRun("all"),
  runFailed: () => startRun("all", { only_failed: true }),
  buildKr: () => startRun("kr"),
  refresh: () => { S.view = null; route(); },
  reload: () => { S.loaded = false; S.pid = null; S.view = null; route(); },
  goto: (el) => go(el.dataset.href),
  docFacts: (el) => { S.filter = { ...emptyFilter(), doc: el.dataset.id }; S.view = null; go(link("facts")); },
  moreFacts: () => loadFacts(false),
  factStatus: (el) => setFactStatus(el.dataset.id, el.dataset.status || null),
  factPending: (el) => factPending(el),
  toggleFactEdit: () => toggleFactEdit(),
  openGroup: (el) => { S.focusGroup = el.dataset.id; go(link("groups")); },
  decide: (el) => decide(el),
  editGroup: (el) => editGroup(el),
  saveGroup: (el) => saveGroup(el),
  resetGroup: (el) => saveGroup(el, true),
  cancelGroup: (el) => {
    redrawGroup(el.dataset.id);
    $(`#g-${CSS.escape(el.dataset.id)} .g-statement`)?.focus();
  },
  editItem: (el) => editItem(el),
  saveItem: (el) => saveItem(el),
  itemStatus: (el) => itemStatus(el),
  itemPending: (el) => itemPending(el),
  cancelItem: (el) => cancelItem(el),
  ref: (el) => scrollToRef(el.dataset.n),
  addThesis: () => openThesisDialog(),
};

const CHANGE = { excludeGroup };

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || el.disabled || !ACT[el.dataset.act]) return;
  if (el.dataset.act === "goto" && e.target.closest("a")) return;
  e.preventDefault();
  ACT[el.dataset.act](el, e);
});

document.addEventListener("change", (e) => {
  const el = e.target.closest?.("[data-change]");
  if (el && CHANGE[el.dataset.change]) CHANGE[el.dataset.change](el, e);
});

$("#projectSel").addEventListener("change", (e) => {
  const view = VIEWS[S.route.view] ? S.route.view : "pdf";
  go(link(view, null, e.target.value));
});

// файл, брошенный мимо зоны загрузки, браузер иначе откроет вместо приложения
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

const header = $(".top");
const setHeaderHeight = () => document.documentElement.style.setProperty("--header-h", `${header.offsetHeight}px`);
if (window.ResizeObserver) new ResizeObserver(setHeaderHeight).observe(header);
setHeaderHeight();

window.addEventListener("hashchange", route);
route();
