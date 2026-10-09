// KRGen — интерфейс проверки и редактирования. Без сборки, vanilla JS.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { pid: null, tab: "docs", meta: null, cards: [], selCard: null, pollTimer: null, cardFilter: {} };

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.body && !(o.body instanceof FormData)) { o.body = JSON.stringify(o.body); o.headers = { "Content-Type": "application/json" }; }
  const r = await fetch(path, o);
  if (!r.ok) { const t = await r.text(); toast("Ошибка: " + t.slice(0, 300)); throw new Error(t); }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 3500); }

const STATUS = {
  verified: ["ok", "подтверждена"], needs_review: ["warn", "требует проверки"], unverified: ["bad", "не подтверждена"],
  extracted: ["", "не проверена"], confirmed: ["ok", "подтверждена экспертом"], rejected: ["bad", "отклонена экспертом"],
  merged: ["info", "объединена"], unmerged: ["", "разъединена"],
};
const DOCST = {
  done: ["ok", "обработан"], done_empty: ["info", "обработан, утверждений нет"], partial: ["warn", "обработан частично"],
  error: ["bad", "ошибка"], unsupported: ["bad", "не поддерживается"], duplicate: ["info", "дубликат"],
  queued: ["", "в очереди"], processing: ["warn", "обрабатывается…"],
};
// ключи разделов «4», «5»… JS ставит перед «1.1» — сортируем явно
const secKeys = () => Object.keys(S.meta.sections).sort((a, b) => parseFloat(a) - parseFloat(b) || a.localeCompare(b));
// Markdown черновика собирается из текста PDF и ответов модели — без санитайзера HTML из
// статьи (prompt injection) выполнился бы в интерфейсе, где утверждаются тезисы
const mdSafe = (md) => (window.DOMPurify ? DOMPurify.sanitize(marked.parse(md || "")) : esc(md || "").replace(/\n/g, "<br>"));
const chip = (map, k) => { const [c, t] = map[k] || ["", k || "—"]; return `<span class="chip ${c}">${esc(t)}</span>`; };
const CLAIM = { source_recommendation: "рекомендация источника", study_finding: "результат исследования", background_fact: "справочный факт" };
const POL = { positive: "положительное", negative: "отрицательное", conditional: "условное", descriptive: "описательное" };
const AGE = { adults: "взрослые", children: "дети", mixed: "взрослые и дети", unspecified: "не указано" };
const DESIGN_RU = { meta_analysis_rct: "метаанализ РКИ", systematic_review_rct: "систематический обзор РКИ", rct: "РКИ", cohort: "когортное", case_control: "случай-контроль", case_series: "серия случаев", case_report: "описание случая", non_comparative: "несравнительное", narrative_review: "обзор", clinical_guideline: "руководство", textbook: "учебник", expert_opinion: "мнение экспертов", systematic_review_other: "систематический обзор", diagnostic_with_reference: "диагностическое с референсом", diagnostic_without_reference: "диагностическое без референса", meta_analysis_diagnostic: "метаанализ диагностических", unknown: "не определён" };

// ------------------------------------------------------------ проекты ---
async function loadProjects(selectId) {
  const ps = await api("/api/projects");
  const sel = $("#projectSel");
  sel.innerHTML = `<option value="">— выберите проект —</option>` + ps.map((p) => `<option value="${p.id}">${esc(p.meta?.label ? p.meta.label + " · " + p.title : p.title)} (${p.id})</option>`).join("");
  const want = selectId || localStorage.getItem("krgen.pid");
  if (want && ps.some((p) => p.id === want)) { sel.value = want; openProject(want); }
}
$("#projectSel").onchange = (e) => openProject(e.target.value);
$("#newProjBtn").onclick = () => $("#newProj").showModal();
$("#newProjForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();  // окно закрывается только после успешного создания: при ошибке поля не теряются
  const f = new FormData(e.target);
  const p = await api("/api/projects", { method: "POST", body: { title: f.get("title"), age_category: f.get("age_category"), icd_codes: f.get("icd").split(","), developer_org: f.get("org") } });
  $("#newProj").close();
  loadProjects(p.id);
});

async function openProject(pid) {
  S.pid = pid || null;
  localStorage.setItem("krgen.pid", pid || "");
  $("#projBar").classList.toggle("hidden", !pid);
  $("#tabs").classList.toggle("hidden", !pid);
  if (!pid) { $("#main").innerHTML = `<div class="panel">Создайте или выберите проект.</div>`; return; }
  await refreshHeader();
  render();
}

async function refreshHeader() {
  const d = await api(`/api/projects/${S.pid}`);
  const p = d.project;
  $("#pTitle").textContent = p.meta?.label ? `${p.meta.label} — ${p.title}` : p.title;
  $("#pMeta").textContent = `МКБ-10: ${(p.icd_codes || []).join(", ")} · ${AGE[p.age_category] || p.age_category} · ${p.developer_org || ""}`;
  const c = d.counts, cc = c.cards || {};
  $("#counts").innerHTML = [
    `<span class="chip">документов: ${c.documents}</span>`,
    `<span class="chip ok">подтверждено: ${cc.verified || 0}</span>`,
    `<span class="chip warn">на проверку: ${cc.needs_review || 0}</span>`,
    `<span class="chip bad">не подтверждено: ${cc.unverified || 0}</span>`,
    `<span class="chip info">тезисов: ${c.theses}</span>`,
    `<span class="chip ${c.conflicts ? "warn" : ""}">противоречий: ${c.conflicts}</span>`,
    c.pending ? `<span class="chip warn">новых автоверсий после правок: ${c.pending}</span>` : "",
  ].join(" ");
  const r = d.run;
  $("#runBtn").disabled = d.running;
  if (r) {
    const pr = r.progress || {};
    const last = (r.log || []).slice(-1)[0];
    const st = { running: "⏳ идёт обработка", completed: "✅ завершено", completed_with_errors: "⚠ завершено с ошибками", failed: "❌ сбой", stopped: "⏹ остановлен" }[r.status] || r.status;
    $("#runStatus").innerHTML = `<b>${st}</b> · этап: ${esc(pr.stage || r.stage)}${pr.total ? ` (${pr.done}/${pr.total})` : ""} · ${last ? esc(last.msg) : ""}` +
      (r.error ? `<div class="flag">${esc(r.error.split("\n").slice(0, 3).join(" | "))}</div>` : "");
  } else $("#runStatus").textContent = "Обработка ещё не запускалась.";
  clearTimeout(S.pollTimer);
  if (d.running) S.pollTimer = setTimeout(async () => { await refreshHeader(); if (["docs", "report"].includes(S.tab)) render(); }, 2500);
  else if (S._wasRunning) { S._wasRunning = false; render(); }
  if (d.running) S._wasRunning = true;
}

$("#runBtn").onclick = async () => {
  await api(`/api/projects/${S.pid}/run`, { method: "POST", body: { use_judge: $("#optJudge").checked, only_failed: $("#optFailed").checked } });
  toast("Обработка запущена"); setTimeout(refreshHeader, 500);
};

document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  S.tab = b.dataset.tab; render();
}));

function render() { ({ runs: renderRuns, docs: renderDocs, cards: renderCards, conflicts: renderConflicts, theses: renderTheses, draft: renderDraft, report: renderReport, edits: renderEdits })[S.tab](); }

// ------------------------------------------------------------ прогоны ---
const RUNST = { running: ["warn", "идёт"], completed: ["ok", "завершён"], completed_with_errors: ["warn", "завершён с ошибками"], failed: ["bad", "сбой"], stopped: ["", "остановлен"] };
const fmtDur = (s) => (s == null ? "—" : s < 90 ? `${Math.round(s)} с` : `${Math.round(s / 60)} мин`);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("ru", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

function runRow(r) {
  const s = r.summary, u = r.usage || {}, p = r.progress || {};
  const dur = (r.finished_at || Date.now() / 1000) - r.started_at;
  return `<tr class="${r.id === S.selRun ? "sel" : ""}"><td>${fmtTime(r.started_at)}</td><td>${r.meta?.label ? `<b>${esc(r.meta.label)}</b><br><span class="muted" style="font-size:12px">${esc(r.title)}</span>` : esc(r.title || r.project_id)}${r.project_id === S.pid ? ` <span class="chip info">текущий</span>` : ""}</td>
    <td>${chip(RUNST, r.status)}${r.status === "running" ? `<div class="muted" style="font-size:12px">${esc(p.stage || r.stage)}${p.total ? ` ${p.done}/${p.total}` : ""}</div>` : ""}</td>
    <td>${fmtDur(dur)}</td><td>${s ? `${s.n_documents} / ${s.n_cards} / ${s.theses_in_draft ?? s.theses}` : `<span class="muted">снимка нет</span>`}</td>
    <td>${u.calls || 0} (${u.hits || 0})</td><td>${(u.cost || 0).toFixed(2)}</td>
    <td><button class="small ghost" style="color:var(--accent)" onclick="showRun('${esc(r.id)}')">Открыть</button></td></tr>`;
}

// Пока идёт прогон, обновляются ТОЛЬКО строки таблицы (и карточка идущего прогона):
// форма нового прогона и раскрытый черновик завершённого прогона не пересоздаются.
async function refreshRunsTable() {
  if (S.tab !== "runs" || !$("#runsBody")) return;
  const runs = await api("/api/runs");
  $("#runsBody").innerHTML = runs.map(runRow).join("") || `<tr><td colspan=8 class="muted">Прогонов ещё не было</td></tr>`;
  const sel = runs.find((r) => r.id === S.selRun);
  if (sel && (sel.status === "running" || S._shownRunKey !== `${sel.id}:${sel.status}`)) showRun(sel.id, true);
  clearTimeout(S.runsTimer);
  if (runs.some((r) => r.status === "running")) S.runsTimer = setTimeout(refreshRunsTable, 4000);
}

async function renderRuns() {
  const docs = await api(`/api/projects/${S.pid}/documents`);
  const usable = docs.filter((d) => !["duplicate", "unsupported"].includes(d.status));
  S._shownRunKey = null;
  $("#main").innerHTML = `
  <div class="panel"><h3 style="margin-top:0">Новый прогон</h3>
    <div class="muted">Запускается в <b>отдельной копии проекта</b> на выбранных статьях — результаты текущего проекта и прошлых прогонов не меняются. Для живой демонстрации хватит 1–2 коротких статей (около 1–2 минут на статью).</div>
    <div class="grid2" style="margin-top:8px"><label>Название прогона<input id="forkTitle" placeholder="Демонстрация"></label>
      <label style="justify-content:flex-end"><span><input type="checkbox" id="forkJudge" checked> LLM-рецензент</span></label></div>
    <div style="max-height:180px;overflow:auto;margin-top:8px;border:1px solid var(--line);border-radius:6px;padding:6px">
      ${usable.map((d) => `<label style="display:block;font-size:13px"><input type="checkbox" class="forkDoc" value="${esc(d.id)}"> ${esc((d.biblio?.title || d.filename).slice(0, 90))} <span class="muted">(${d.n_pages} стр.)</span></label>`).join("") || `<span class="muted">В проекте нет документов</span>`}
    </div>
    <div class="row"><button id="forkBtn">▶ Запустить новый прогон</button><span class="muted" id="forkCount"></span></div></div>
  <div class="panel"><h3 style="margin-top:0">История прогонов</h3>
    <table><thead><tr><th>Начат</th><th>Проект</th><th>Статус</th><th>Длит.</th><th>Док. / карточек / тезисов</th><th>Запросов (кэш)</th><th>$ экв.</th><th></th></tr></thead>
    <tbody id="runsBody"><tr><td colspan=8 class="muted">Загрузка…</td></tr></tbody></table></div>
  <div id="runDetail"></div>`;
  const upd = () => ($("#forkCount").textContent = `выбрано: ${document.querySelectorAll(".forkDoc:checked").length}`);
  document.querySelectorAll(".forkDoc").forEach((c) => (c.onchange = upd)); upd();
  $("#forkBtn").onclick = async () => {
    const ids = [...document.querySelectorAll(".forkDoc:checked")].map((c) => c.value);
    if (!ids.length) return toast("Выберите хотя бы одну статью");
    $("#forkBtn").disabled = true;
    try {
      const r = await api(`/api/projects/${S.pid}/fork`, { method: "POST", body: { doc_ids: ids, title: $("#forkTitle").value.trim() || null, use_judge: $("#forkJudge").checked } });
      toast("Новый прогон запущен"); S.selRun = null; await loadProjects(r.project_id); S.tab = "runs";
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === "runs")); render();
    } finally { if ($("#forkBtn")) $("#forkBtn").disabled = false; }
  };
  await refreshRunsTable();
}

async function showRun(id, quiet) {
  S.selRun = id;
  if (!quiet) document.querySelectorAll("#runsBody tr").forEach((tr) => tr.classList.toggle("sel", tr.innerHTML.includes(`showRun('${id}')`)));
  const wasOpen = !!$("#runDetail details")?.open;
  const r = await api(`/api/runs/${id}`);
  if (S.selRun !== id || !$("#runDetail")) return;  // пока грузилось, выбрали другой прогон или ушли со вкладки
  S._shownRunKey = `${id}:${r.status}`;
  const s = r.summary, cs = (s && s.cards) || {}, ru = (s && s.run_usage) || {};
  const log = (r.log || []).slice().reverse();
  $("#runDetail").innerHTML = `<div class="panel">
    <div class="row" style="margin-top:0;justify-content:space-between"><h3 style="margin:0">${esc(r.title || r.project_id)} <span class="muted" style="font-size:13px">${r.id}</span></h3>
      <div class="row" style="margin-top:0">${chip(RUNST, r.status)}
        <button class="small ghost" style="color:var(--accent)" onclick="openRunProject('${r.project_id}')">Перейти к проекту</button></div></div>
    <div class="muted">Начат ${fmtTime(r.started_at)}${r.finished_at ? `, длительность ${fmtDur(r.finished_at - r.started_at)}` : ""} · модель ${esc((r.config || {}).model || "")} · рассуждения ${(r.config || {}).thinking_budget ? "вкл" : "выкл"}</div>
    ${r.error ? `<div class="flag">${esc(r.error).replace(/\n/g, "<br>")}</div>` : ""}
    ${s ? `<div class="kpis" style="margin-top:10px">
      <div class="kpi"><span class="muted">документов</span><b>${s.n_documents}</b><span class="muted">стр. обработано ${s.pages?.done ?? "—"} из ${s.pages?.total ?? "—"}</span></div>
      <div class="kpi"><span class="muted">карточек</span><b>${s.n_cards}</b><span class="muted">✓ ${cs.verified || 0} · ⚠ ${cs.needs_review || 0} · ✗ ${cs.unverified || 0}</span></div>
      <div class="kpi"><span class="muted">тезисов в черновике</span><b>${s.theses_in_draft ?? s.theses}</b><span class="muted">описательных ${s.theses_narrative}, объединено ${s.theses_merged_away}</span></div>
      <div class="kpi"><span class="muted">повторов / противоречий</span><b>${s.duplicates} / ${s.conflicts}</b></div>
      <div class="kpi"><span class="muted">запросов в прогоне</span><b>${ru.calls || 0}</b><span class="muted">кэш ${ru.cache_hits || 0} · ошибок ${ru.errors || 0} · $${(ru.cost_usd || 0).toFixed(2)}</span></div>
      <div class="kpi"><span class="muted">всего по проекту (все прогоны)</span><b>${(s.project_usage || {}).calls || 0}</b><span class="muted">вых. токенов ${((s.project_usage || {}).output_tokens || 0).toLocaleString("ru")} · $${((s.project_usage || {}).cost_usd || 0).toFixed(2)}</span></div></div>
      <div class="row"><b>Снимок результата:</b>
        <a href="/api/runs/${r.id}/file/draft.docx"><button class="small">Word</button></a>
        <a href="/api/runs/${r.id}/file/draft.md"><button class="small ghost" style="color:var(--accent)">Markdown</button></a>
        <a href="/api/runs/${r.id}/file/export.json"><button class="small ghost" style="color:var(--accent)">JSON</button></a></div>`
      : `<div class="muted" style="margin-top:8px">${r.status === "running" ? "Прогон идёт — снимок появится по завершении." : "Снимок результата для этого прогона не сохранялся."}</div>`}
    <div class="split" style="margin-top:12px;grid-template-columns:1fr 1fr">
      <div><h4 style="margin:4px 0">Запросы по этапам</h4><table><thead><tr><th>Этап</th><th>Запросов</th><th>Кэш</th><th>Вых. токены</th><th>$</th><th>Ошибок</th></tr></thead><tbody>
        ${(r.usage_by_stage || []).map((u) => `<tr><td>${u.purpose}</td><td>${u.calls}</td><td>${u.hits || 0}</td><td>${(u.out_tokens || 0).toLocaleString("ru")}</td><td>${(u.cost || 0).toFixed(2)}</td><td>${u.errors || 0}</td></tr>`).join("") || `<tr><td colspan=6 class="muted">—</td></tr>`}</tbody></table></div>
      <div><h4 style="margin:4px 0">Журнал</h4><div style="max-height:260px;overflow:auto;font-size:12px">
        ${log.map((l) => `<div><span class="muted">${new Date(l.ts * 1000).toLocaleTimeString("ru")}</span> <b>${esc(l.stage)}</b> ${esc(l.msg)}</div>`).join("") || `<span class="muted">пусто</span>`}</div></div></div>
    ${r.draft_md ? `<details style="margin-top:10px"><summary><b>Черновик из снимка</b></summary><div class="draft" style="margin-top:8px">${mdSafe(r.draft_md)}</div></details>` : ""}
  </div>`;
  if (wasOpen && $("#runDetail details")) $("#runDetail details").open = true;
  if (!quiet) $("#runDetail").scrollIntoView({ behavior: "smooth" });
}
async function openRunProject(pid) { await loadProjects(pid); document.querySelector('#tabs button[data-tab="theses"]').click(); }

// ---------------------------------------------------------- документы ---
async function renderDocs() {
  const docs = await api(`/api/projects/${S.pid}/documents`);
  const pgTitle = (p) => `стр. ${p.page_no}: ${p.status === "done" ? "обработана" : (p.reason || p.kind)}`;
  $("#main").innerHTML = `
  <div class="panel"><div class="drop" id="drop">Перетащите PDF сюда или <label style="color:var(--accent);cursor:pointer">выберите файлы<input type="file" id="fileIn" multiple accept=".pdf" hidden></label>.
  Поддерживаются PDF с текстовым слоем (рус./англ.). Сканы без текстового слоя будут явно помечены как необработанные.</div></div>
  <div class="panel"><div class="legend">Покрытие страниц:
    <span><i class="pg done"></i>обработана</span><span><i class="pg references"></i>список литературы</span>
    <span><i class="pg skipped"></i>вне бюджета/нерелевантна</span><span><i class="pg no_text"></i>нет текстового слоя</span>
    <span><i class="pg error"></i>ошибка API</span><span><i class="pg"></i>не обработана</span></div>
  <table><thead><tr><th>Документ</th><th>Статус</th><th>Карточек</th><th>Страницы</th></tr></thead><tbody>
  ${docs.map((d) => {
    const b = d.biblio || {};
    return `<tr><td><b>${esc(b.title || d.filename)}</b><div class="muted">${esc(d.filename)} · ${d.n_pages} стр.${b.year ? " · " + b.year : ""}${b.journal ? " · " + esc(b.journal) : ""}${b.study_design ? " · дизайн: " + esc(DESIGN_RU[b.study_design] || b.study_design) : ""}${b.doi ? " · DOI " + esc(b.doi) : ""}</div>
      <a href="/api/docs/${d.id}/pdf" target="_blank">открыть PDF</a></td>
      <td>${chip(DOCST, d.status)}<div class="muted" style="font-size:12px;max-width:340px">${esc(d.status_detail || "")}</div></td>
      <td>${d.n_cards}</td>
      <td><div class="pages">${d.pages.map((p) => `<i class="pg ${p.status === "done" ? "done" : p.status === "error" ? "error" : p.kind === "body" ? "" : p.kind}" title="${esc(pgTitle(p))}"></i>`).join("")}</div>
      ${d.chunks.filter((c) => c.status === "error").map((c) => `<div class="flag">стр. ${c.page_from}–${c.page_to}: ${esc(c.error)}</div>`).join("")}</td></tr>`;
  }).join("") || `<tr><td colspan=4 class="muted">Документов нет</td></tr>`}</tbody></table></div>`;
  const drop = $("#drop");
  const upload = async (files) => {
    const fd = new FormData(); [...files].forEach((f) => fd.append("files", f));
    toast("Загрузка…"); const res = await api(`/api/projects/${S.pid}/upload`, { method: "POST", body: fd });
    toast(res.map((r) => `${r.filename}: ${(DOCST[r.status] || [0, r.status])[1]}`).join("; ")); renderDocs(); refreshHeader();
  };
  $("#fileIn").onchange = (e) => upload(e.target.files);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); };
}

// ----------------------------------------------------------- карточки ---
async function renderCards() {
  if (!S.meta) S.meta = await api("/api/meta");
  const docs = await api(`/api/projects/${S.pid}/documents`);
  const f = S.cardFilter;
  const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v));
  S.cards = await api(`/api/projects/${S.pid}/cards?${qs}`);
  const secOpts = secKeys().map((k) => [k, S.meta.sections[k]]).map(([k, v]) => `<option value="${k}" ${f.section === k ? "selected" : ""}>${k} ${esc(v)}</option>`).join("");
  $("#main").innerHTML = `
  <div class="filters">
    <select id="fStatus"><option value="">все статусы</option>${["verified", "needs_review", "unverified", "confirmed", "rejected"].map((s) => `<option value="${s}" ${f.status === s ? "selected" : ""}>${STATUS[s][1]}</option>`).join("")}</select>
    <select id="fSection"><option value="">все разделы</option>${secOpts}</select>
    <select id="fDoc"><option value="">все документы</option>${docs.map((d) => `<option value="${d.id}" ${f.doc === d.id ? "selected" : ""}>${esc((d.biblio?.title || d.filename).slice(0, 60))}</option>`).join("")}</select>
    <input id="fQ" placeholder="поиск" value="${esc(f.q || "")}">
    <span class="muted">${S.cards.length} карточек</span>
  </div>
  <div class="split"><div class="panel list" id="cardList"></div><div class="panel detail" id="cardDetail"><span class="muted">Выберите карточку слева.</span></div></div>`;
  const upd = () => { S.cardFilter = { status: $("#fStatus").value, section: $("#fSection").value, doc: $("#fDoc").value, q: $("#fQ").value }; renderCards(); };
  ["fStatus", "fSection", "fDoc"].forEach((id) => ($("#" + id).onchange = upd));
  $("#fQ").onkeydown = (e) => e.key === "Enter" && upd();
  drawCardList();
  if (S.selCard && S.cards.some((c) => c.id === S.selCard)) showCard(S.selCard);
}

function cardRow(c) {
  return `<div class="item ${c.id === S.selCard ? "sel" : ""}" data-id="${esc(c.id)}">
    ${chip(STATUS, c.user_status || c.status)} ${c.merged_into ? `<span class="chip info">повтор → ${esc(c.merged_into)}</span>` : ""} ${c.duplicates.length ? `<span class="chip info">+${c.duplicates.length} повтор.</span>` : ""}
    ${c.pending_auto ? `<span class="chip warn">новая автоверсия</span>` : ""} ${c.override ? `<span class="chip">правка эксперта</span>` : ""}
    <div class="st">${esc(c.data.statement_ru)}</div>
    <div class="muted" style="font-size:12px">${esc(c.data.kr_section)} · ${esc(CLAIM[c.data.claim_type] || "")} · ${esc((c.source.title || c.filename || "").slice(0, 50))} · стр. ${esc(c.data.page)}</div></div>`;
}

function drawCardList() {
  $("#cardList").innerHTML = S.cards.map(cardRow).join("") || `<div class="muted">Нет карточек</div>`;
  document.querySelectorAll("#cardList .item").forEach((el) => (el.onclick = () => { S.selCard = el.dataset.id; document.querySelectorAll("#cardList .item").forEach((x) => x.classList.toggle("sel", x === el)); showCard(el.dataset.id); }));
}

// обновить одну строку списка после решения эксперта — без перезагрузки всего списка
function updateCardRow(full) {
  const i = S.cards.findIndex((x) => x.id === full.id); if (i < 0) return;
  S.cards[i] = { ...S.cards[i], status: full.status, user_status: full.user_status, merged_into: full.merged_into,
    override: full.override ? {} : null, pending_auto: full.pending_auto ? true : null,
    data: { ...S.cards[i].data, statement_ru: full.data.statement_ru, kr_section: full.data.kr_section, claim_type: full.data.claim_type, page: full.data.page } };
  const el = document.querySelector(`#cardList .item[data-id="${CSS.escape(full.id)}"]`);
  if (!el) return;
  const tmp = document.createElement("div"); tmp.innerHTML = cardRow(S.cards[i]);
  const row = tmp.firstElementChild; el.replaceWith(row);
  row.onclick = () => { S.selCard = full.id; document.querySelectorAll("#cardList .item").forEach((x) => x.classList.toggle("sel", x === row)); showCard(full.id); };
}

function checkIcon(c) { return c.ok ? "✅" : c.resolved_by_judge ? "☑️" : c.severity === "error" ? "❌" : c.severity === "info" ? "ℹ️" : "⚠️"; }

async function showCard(id) {
  const c = await api(`/api/cards/${id}`);
  if (S.selCard !== id) return;  // пока грузилось, выбрали другую карточку
  const d = c.data, v = c.verification || {}, j = v.judge;
  const sel = (name, opts, val) => `<select name="${name}">${(Array.isArray(opts) ? opts : Object.entries(opts)).map(([k, t]) => `<option value="${k}" ${val === k ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
  const inp = (name, label) => `<label>${label}<input name="${name}" value="${esc(d[name] || "")}"></label>`;
  const page = (v.quote && v.quote.found_page) || d.page;
  $("#cardDetail").innerHTML = `
    <div class="row" style="margin-top:0">${chip(STATUS, c.status)} ${c.user_status ? chip(STATUS, c.user_status) : ""} ${c.merge_lock ? chip(STATUS, c.merge_lock) : ""} <span class="muted">${c.id}</span></div>
    ${c.pending_auto ? `<div class="diff"><b>После повторной обработки получена новая автоверсия</b>, ваша правка сохранена.<br>Новая формулировка: «${esc(c.pending_auto.statement_ru)}»
      <div class="row"><button class="small" onclick="pendingAct('cards','${c.id}',true)">Принять новую (сбросить правку)</button><button class="small ghost" onclick="pendingAct('cards','${c.id}',false)">Оставить мою</button></div></div>` : ""}
    <div class="stmt" style="font-size:16px;font-weight:600">${esc(d.statement_ru)}</div>
    <div class="muted" style="font-size:12px">${esc(CLAIM[d.claim_type] || "")} · ${esc(POL[d.polarity] || "")} · ${esc(AGE[d.age_group] || "")} · раздел ${esc(d.kr_section)}${d.secondary_citation ? " · вторичное цитирование" : ""}${d.dose ? " · доза: " + esc(d.dose) : ""}${d.timing ? " · сроки: " + esc(d.timing) : ""}</div>
    <h4>Источник</h4>
    <div>${esc(c.source.title || c.filename)} ${c.source.year ? "(" + c.source.year + ")" : ""} · ${esc(DESIGN_RU[c.source.design] || c.source.design || "дизайн не определён")}${c.source.doi ? " · DOI " + esc(c.source.doi) : ""}</div>
    <div class="muted">Страница PDF: ${d.page}${v.quote && v.quote.page_corrected ? ` (модель указала ${v.quote.claimed_page})` : ""} · <a href="/api/docs/${c.doc_id}/pdf#page=${page}" target="_blank">открыть PDF на странице</a></div>
    <div class="quote">${esc(d.quote)}</div>
    <h4>Проверки</h4>
    <ul class="checks">${(v.checks || []).map((k) => `<li>${checkIcon(k)} ${esc(k.detail)}${k.resolved_by_judge ? ` <span class="muted">— эвристика снята рецензентом</span>` : ""}</li>`).join("")}
      ${j ? (j.error ? `<li>⚠️ Рецензент: ${esc(j.error)}</li>` : `<li>${j.entailment === "supported" ? "✅" : j.entailment === "partial" ? "⚠️" : "❌"} Рецензент (LLM): ${{ supported: "цитата подтверждает формулировку", partial: "подтверждает частично", not_supported: "не подтверждает" }[j.entailment]}
        · числа ${j.numbers_ok ? "✓" : "✗"} · отрицание ${j.negation_ok ? "✓" : "✗"} · популяция ${j.population_ok ? "✓" : "✗"} · тип ${j.claim_type_ok ? "✓" : "✗"}</li>
        ${(j.issues || []).map((i) => `<li class="muted">— ${esc(i)}</li>`).join("")}`) : `<li class="muted">Смысловая проверка не выполнялась</li>`}
      ${(v.conflicts || []).length ? `<li>⚠️ Участвует в противоречии: ${v.conflicts.join(", ")}</li>` : ""}</ul>
    ${c.duplicates.length ? `<h4>Объединённые повторы (источники сохранены)</h4>${c.duplicates.map((x) => `<div><a href="#" onclick="S.selCard='${x.id}';showCard('${x.id}');return false">${x.id}</a> <span class="muted">${x.doc_id}</span></div>`).join("")}` : ""}
    <div class="row"><button type="button" class="ok" onclick="cardStatus('${c.id}','confirmed')">Подтвердить</button>
<button type="button" class="bad" onclick="cardStatus('${c.id}','rejected')">Отклонить</button>
${c.user_status ? `<button type="button" class="ghost" onclick="cardStatus('${c.id}',null)">Сбросить решение</button>` : ""}
${c.merged_into ? `<button type="button" class="ghost" onclick="cardStatus('${c.id}','unmerged')">Разъединить с ${c.merged_into}</button>` : ""}
<button type="button" class="ghost" onclick="mergePrompt('${c.id}')">Объединить с…</button></div>
    <details id="editBox"><summary><b>Редактировать формулировку и параметры</b></summary>
    <form id="cardForm">
      <label class="muted" style="font-size:12px">Формулировка (рус.)<textarea name="statement_ru" rows="3">${esc(d.statement_ru)}</textarea></label>
      <div class="grid2">
        <label>Тип${sel("claim_type", CLAIM, d.claim_type)}</label>
        <label>Характер${sel("polarity", POL, d.polarity)}</label>
        <label>Возраст${sel("age_group", AGE, d.age_group)}</label>
        <label>Раздел КР${sel("kr_section", secKeys().map((k) => [k, k + " " + S.meta.sections[k]]), d.kr_section)}</label>
        ${inp("population", "Группа пациентов")}${inp("intervention", "Действие / метод")}
        ${inp("outcome", "Цель / результат")}${inp("comparator", "Сравнение")}
        ${inp("conditions", "Условия / ограничения")}${inp("dose", "Доза")}
        ${inp("timing", "Сроки")}${inp("thresholds", "Пороги")}
        <label>Релевантность теме${sel("topic_relevance", { direct: "прямая", indirect: "косвенная", none: "нет" }, d.topic_relevance)}</label>
        <label>Вторичное цитирование${sel("secondary_citation", { false: "нет", true: "да (пересказ чужой работы)" }, String(!!d.secondary_citation))}</label>
      </div>
      <label class="muted" style="font-size:12px;margin-top:6px">Комментарий к правке<input name="_comment" placeholder="необязательно"></label>
      <div class="row"><button type="submit">Сохранить правку</button></div>
    </form></details>
    <h4>Страница с подсветкой цитаты</h4>
    <img class="pageimg" loading="lazy" src="/api/docs/${c.doc_id}/page/${page}.png?card=${c.id}" alt="страница ${page}">`;
  $("#cardForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target); const fields = {};
    for (const [k, val] of f.entries()) if (k !== "_comment") fields[k] = k === "secondary_citation" ? val === "true" : val;
    const changed = Object.fromEntries(Object.entries(fields).filter(([k, val]) => String(val) !== String(d[k] ?? (k === "secondary_citation" ? false : ""))));
    if (!Object.keys(changed).length) return toast("Изменений нет");
    const full = await api(`/api/cards/${c.id}`, { method: "PATCH", body: { fields: changed, comment: f.get("_comment") } });
    toast("Правка сохранена, детерминированные проверки пересчитаны"); updateCardRow(full); showCard(c.id);
  };
}
async function cardStatus(id, st) { const full = await api(`/api/cards/${id}/status`, { method: "POST", body: { user_status: st } }); toast("Решение сохранено"); updateCardRow(full); showCard(id); refreshHeader(); }
async function mergePrompt(id) {
  const other = prompt("ID карточки-повтора, которую объединить с текущей (ev_…):"); if (!other) return;
  await api("/api/cards/merge", { method: "POST", body: { canonical: id, others: other.split(/[ ,]+/).filter(Boolean) } }); toast("Объединено"); renderCards();
}
async function pendingAct(entity, id, accept) { await api(`/api/${entity}/${id}/pending`, { method: "POST", body: { accept } }); toast(accept ? "Принята новая автоверсия" : "Оставлена ваша правка"); render(); refreshHeader(); }

// ------------------------------------------------------- противоречия ---
async function renderConflicts() {
  const cs = await api(`/api/projects/${S.pid}/conflicts`);
  $("#main").innerHTML = cs.map((c) => `<div class="panel">
    <div class="row" style="margin-top:0">${c.status === "open" ? `<span class="chip warn">открыто</span>` : `<span class="chip ok">разрешено</span>`} <b>${esc(c.description)}</b> <span class="muted">${c.id} · ${esc(c.kind)}</span></div>
    <div class="conf">${c.cards.map((k) => `<div class="thesis"><div>${chip(STATUS, k.status)}</div><div class="stmt" style="font-size:14px">${esc(k.data.statement_ru)}</div>
      <div class="muted">${esc(k.source.title || k.filename)} (${k.source.year || "?"}), ${esc(DESIGN_RU[k.source.design] || "")} · стр. ${k.data.page} · ${AGE[k.data.age_group] || ""}</div>
      <div class="quote" style="font-size:13px">${esc(k.data.quote)}</div><a href="#" onclick="gotoCard('${k.id}');return false">открыть карточку</a></div>`).join("")}</div>
    ${c.status === "open" ? `<div class="row"><input id="res_${c.id}" placeholder="Решение эксперта (обе стороны сохраняются)" style="flex:1"><button onclick="resolveConflict('${c.id}')">Зафиксировать решение</button></div>`
      : `<div class="muted">Решение: ${esc(c.resolution?.text || "")}</div>`}</div>`).join("") || `<div class="panel muted">Противоречий не обнаружено.</div>`;
}
async function resolveConflict(id) { await api(`/api/conflicts/${id}/resolve`, { method: "POST", body: { resolution: $("#res_" + id).value } }); toast("Решение сохранено"); renderConflicts(); refreshHeader(); }
function gotoCard(id) { S.selCard = id; S.cardFilter = {}; document.querySelector('#tabs button[data-tab="cards"]').click(); }

// -------------------------------------------------------------- тезисы ---
async function renderTheses() {
  if (!S.meta) S.meta = await api("/api/meta");
  const ts = await api(`/api/projects/${S.pid}/theses`);
  S.theses = ts;
  const merged = ts.filter((t) => t.merged_into).length;
  const bySec = {}; ts.filter((t) => !t.merged_into).forEach((t) => (bySec[t.section] = bySec[t.section] || []).push(t));
  const secs = secKeys();
  $("#main").innerHTML = `
  <div class="panel muted">Тезисов в черновике: ${ts.length - merged}${merged ? ` · ещё ${merged} вошли в объединённые тезисы (повторы)` : ""}</div>
  <div class="panel"><b>Добавить экспертный тезис</b> <span class="muted">— останется отличимым от извлечённых из статей; цитата не создаётся.</span>
    <div class="grid2" style="margin-top:6px">
      <label>Раздел<select id="eSec">${secs.map((k) => `<option value="${k}">${k} ${esc(S.meta.sections[k])}</option>`).join("")}</select></label>
      <label>Вводная фраза<select id="ePhrase">${["Рекомендуется", "Рекомендуется рассмотреть возможность", "Не рекомендуется", "Не рекомендуется рассматривать"].map((x) => `<option>${x}</option>`).join("")}</select></label>
      <label>Автор<input id="eAuthor" placeholder="ФИО эксперта"></label>
      <label>УУР (решение эксперта)<select id="eUur"><option value="">—</option><option>A</option><option>B</option><option>C</option></select></label>
    </div>
    <label class="muted" style="font-size:12px">Текст тезиса (без вводной фразы)<textarea id="eBody" rows="2"></textarea></label>
    <label class="muted" style="font-size:12px">Комментарий<input id="eComment" style="width:100%"></label>
    <div class="row"><button onclick="addExpert()">Добавить</button></div></div>
  ${secs.map((s) => `<div class="panel"><h3 style="margin:0">${s} ${esc(S.meta.sections[s])}</h3>
    ${(bySec[s] || []).map(thesisHtml).join("") || `<div class="muted">Недостаточно материалов: подтверждённых утверждений нет.</div>`}</div>`).join("")}`;
}

function thesisHtml(t) {
  const d = t.data, narrative = d.kind === "narrative";
  const cls = [t.provenance === "manual" ? "manual" : "", t.status].join(" ");
  const grade = narrative ? "" : `<div class="row">
    <label>Вводная фраза <select id="ph_${t.id}">${["Рекомендуется", "Рекомендуется рассмотреть возможность", "Не рекомендуется", "Не рекомендуется рассматривать"].map((x) => `<option ${d.strength_phrase === x ? "selected" : ""}>${x}</option>`).join("")}</select></label>
    <label>УУР <select id="uur_${t.id}"><option value="">—</option>${["A", "B", "C"].map((x) => `<option ${d.uur === x ? "selected" : ""}>${x}</option>`).join("")}</select></label>
    <label>УДД <select id="udd_${t.id}"><option value="">—</option>${[1, 2, 3, 4, 5].map((x) => `<option ${d.udd === x ? "selected" : ""}>${x}</option>`).join("")}</select></label>
    ${d.uur_preliminary ? `<span class="chip warn">УУР предварительный</span>` : d.grade_set_by === "expert" ? `<span class="chip ok">уровни заданы экспертом</span>` : ""}</div>
    <div class="muted" style="font-size:12px">${esc(d.grade_rationale || "")}</div>`;
  return `<div class="thesis ${cls}" id="t_${t.id}">
    <div class="row" style="margin-top:0">${{ draft: `<span class="chip">черновик</span>`, approved: `<span class="chip ok">утверждён</span>`, rejected: `<span class="chip bad">отклонён</span>` }[t.status] || ""}
      ${t.provenance === "manual" ? `<span class="chip" style="background:#efe6fb;color:#5b2ea6">экспертное дополнение</span>` : t.provenance === "auto+expert_review" ? `<span class="chip info">авто + правка эксперта</span>` : `<span class="chip">автоматически</span>`}
      ${narrative ? `<span class="chip">описательный текст</span>` : ""} ${(t.merged_from || []).length ? `<span class="chip info">объединено из ${t.merged_from.length} тезисов</span>` : ""} ${t.orphan ? `<span class="chip warn">не воспроизведён при повторной обработке, сохранён из-за правки</span>` : ""}
      <span class="muted">${t.id}</span></div>
    ${t.pending_auto ? `<div class="diff"><b>Новая автоверсия после повторной обработки</b> (ваша правка сохранена): «${esc(t.pending_auto.statement)}»
      <div class="row"><button class="small" onclick="pendingAct('theses','${t.id}',true)">Принять новую</button><button class="small ghost" onclick="pendingAct('theses','${t.id}',false)">Оставить мою</button></div></div>` : ""}
    <div class="stmt">${esc(d.statement)}</div>
    <textarea id="body_${t.id}" rows="2" style="margin-top:6px">${esc(d.body)}</textarea>
    ${grade}
    ${narrative ? "" : `<label class="muted" style="font-size:12px">Комментарий<textarea id="cm_${t.id}" rows="2">${esc(d.comment || "")}</textarea></label>
    <label class="muted" style="font-size:12px">Критерий качества (прошедшее время)<input id="qc_${t.id}" style="width:100%" value="${esc(d.quality_criterion?.criterion_text || "")}"></label>`}
    ${(t.review_flags || []).map((f) => `<div class="flag">⚠ ${esc(f)}</div>`).join("")}
    ${(d.expert_additions || []).length ? `<div class="flag">Экспертные надстройки (не из источников): ${d.expert_additions.map((a) => esc(a.claim)).join("; ")}</div>` : ""}
    <div class="row"><button class="small" onclick="saveThesis('${t.id}', ${narrative})">Сохранить правку</button>
      <button class="small ok" onclick="thesisStatus('${t.id}','approved')">Утвердить</button>
      <button class="small bad" onclick="thesisStatus('${t.id}','rejected')">Отклонить</button>
      <button class="small ghost" onclick="thesisStatus('${t.id}','draft')">В черновик</button>
      <button class="small ghost" onclick="editEvidence('${t.id}', '${(t.evidence || []).map((e) => e.id).join(",")}')">Связи с карточками…</button></div>
    <details ${t.evidence.length <= 2 ? "open" : ""}><summary class="muted">Доказательства: ${t.evidence.length}</summary>
    ${t.evidence.map((e) => `<div class="ev">${chip(STATUS, e.user_status || e.status)} <a href="#" onclick="gotoCard('${e.id}');return false">${e.id}</a> — ${esc(e.statement)}
      <div class="muted">${esc(e.source.title || e.filename)} (${e.source.year || "?"}), ${esc(DESIGN_RU[e.source.design] || "")} · стр. ${e.page}</div><div class="quote" style="font-size:12px">${esc(e.quote)}</div></div>`).join("") || `<div class="muted">Нет связанных карточек (экспертный тезис).</div>`}</details>
  </div>`;
}
async function saveThesis(id, narrative) {
  const fields = { body: $("#body_" + id).value };
  if (!narrative) {
    fields.strength_phrase = $("#ph_" + id).value; fields.comment = $("#cm_" + id).value;
    const uur = $("#uur_" + id).value, udd = $("#udd_" + id).value;
    const cur = (S.theses || []).find((x) => x.id === id)?.data || {};  // уже загруженный список
    if ((uur || null) !== (cur.uur || null)) fields.uur = uur || null;
    if ((udd ? +udd : null) !== (cur.udd ?? null)) fields.udd = udd ? +udd : null;
    const qc = $("#qc_" + id).value.trim();
    if (qc !== (cur.quality_criterion?.criterion_text || "")) fields.quality_criterion = { ...(cur.quality_criterion || {}), is_criterion: !!qc, criterion_text: qc || null, measurable: !!qc };
  }
  await api(`/api/theses/${id}`, { method: "PATCH", body: { fields } }); toast("Тезис сохранён"); renderTheses();
}
async function thesisStatus(id, st) { await api(`/api/theses/${id}/status`, { method: "POST", body: { status: st } }); toast("Статус сохранён"); renderTheses(); }
async function editEvidence(id, cur) {
  const v = prompt("ID карточек-доказательств через запятую:", cur); if (v === null) return;
  await api(`/api/theses/${id}/evidence`, { method: "POST", body: { evidence_ids: v.split(/[ ,]+/).filter(Boolean) } }); toast("Связи обновлены"); renderTheses();
}
async function addExpert() {
  const body = $("#eBody").value.trim(); if (!body) return toast("Введите текст тезиса");
  await api(`/api/projects/${S.pid}/theses`, { method: "POST", body: { section: $("#eSec").value, strength_phrase: $("#ePhrase").value, body, author: $("#eAuthor").value, comment: $("#eComment").value, uur: $("#eUur").value || null } });
  toast("Экспертный тезис добавлен"); renderTheses(); refreshHeader();
}

// ------------------------------------------------------------ черновик ---
async function renderDraft() {
  const [md, check] = await Promise.all([api(`/api/projects/${S.pid}/draft.md`), api(`/api/projects/${S.pid}/export/kr_schema_check`)]);
  $("#main").innerHTML = `<div class="panel row" style="margin-top:0">
    <b>Выгрузка:</b>
    <a href="/api/projects/${S.pid}/export/docx"><button>Word (.docx)</button></a>
    <a href="/api/projects/${S.pid}/export/md"><button class="ghost" style="color:var(--accent)">Текст (.md)</button></a>
    <a href="/api/projects/${S.pid}/export/json"><button class="ghost" style="color:var(--accent)">JSON (полный, krgen-export/1.0)</button></a>
    <a href="/api/projects/${S.pid}/export/kr_schema"><button class="ghost" style="color:var(--accent)">JSON по kr_recommendation.schema</button></a>
    ${check.valid ? `<span class="chip ok">соответствует kr_recommendation.schema.json</span>` : `<span class="chip bad" title="${esc(check.errors.join("\n"))}">ошибок схемы: ${check.errors.length}</span>`}
  </div><div class="draft">${mdSafe(md)}</div>`;
}

// --------------------------------------------------------------- отчёт ---
async function renderReport() {
  const [r, ev] = await Promise.all([api(`/api/projects/${S.pid}/report`), api(`/api/projects/${S.pid}/eval`)]);
  const t = r.llm_total, fmt = (n) => (n || 0).toLocaleString("ru");
  $("#main").innerHTML = `
  <div class="kpis">
    <div class="kpi"><span class="muted">запросов к модели</span><b>${fmt(t.calls)}</b><span class="muted">из кэша: ${fmt(t.cache_hits)} · ошибок: ${fmt(t.errors)}</span></div>
    <div class="kpi"><span class="muted">токены вход / выход</span><b>${fmt(t.input_tokens)} / ${fmt(t.output_tokens)}</b></div>
    <div class="kpi"><span class="muted">стоимость (эквивалент API)</span><b>$${(t.cost_usd || 0).toFixed(3)}</b><span class="muted">списано из лимитов подписки</span></div>
    <div class="kpi"><span class="muted">суммарное время вызовов</span><b>${Math.round((t.duration_ms || 0) / 1000)} с</b></div>
    <div class="kpi"><span class="muted">объединено повторов</span><b>${fmt(r.duplicates_merged)}</b></div>
  </div>
  <p class="muted">${esc(r.cost_note)}</p>
  ${ev && ev.available !== false ? evalHtml(ev) : `<div class="panel muted">Оценка по эталону ещё не запускалась: <code>python -m krgen.evaluate &lt;проект&gt; gold/…</code></div>`}
  <div class="panel"><h3 style="margin-top:0">Покрытие документов</h3><table><thead><tr><th>Документ</th><th>Статус</th><th>Покрытие</th><th>Пропуски</th></tr></thead><tbody>
  ${r.documents.map((d) => `<tr><td>${esc(d.filename)}</td><td>${chip(DOCST, d.status)}</td><td>${d.pages_processed}/${d.n_pages} (${Math.round(d.coverage * 100)}%)</td>
    <td style="font-size:12px">${d.skipped_pages.map((s) => `стр. ${s.from}${s.to !== s.from ? "–" + s.to : ""}: ${esc(s.reason)}`).join("<br>")}</td></tr>`).join("")}</tbody></table></div>
  <div class="panel"><h3 style="margin-top:0">Вызовы модели по этапам</h3><table><thead><tr><th>Этап</th><th>Модель</th><th>Запросов</th><th>Кэш</th><th>Ошибок</th><th>Вход</th><th>Выход</th><th>$</th><th>Время, с</th></tr></thead><tbody>
  ${r.llm_by_purpose.map((c) => `<tr><td>${c.purpose}</td><td>${c.model}</td><td>${c.calls}</td><td>${c.cache_hits}</td><td>${c.errors}</td><td>${fmt(c.input_tokens)}</td><td>${fmt(c.output_tokens)}</td><td>${(c.cost_usd || 0).toFixed(3)}</td><td>${Math.round(c.duration_ms / 1000)}</td></tr>`).join("")}</tbody></table></div>
  <div class="panel"><h3 style="margin-top:0">Запуски</h3><table><thead><tr><th>Запуск</th><th>Статус</th><th>Модель</th><th>Длительность</th><th>Запросы (кэш)</th><th>$</th><th>Ошибка</th></tr></thead><tbody>
  ${r.runs.map((x) => `<tr><td>${x.id}</td><td>${x.status}</td><td>${x.model}</td><td>${x.wall_s} с</td><td>${x.llm.calls} (${x.llm.hits || 0})</td><td>${(x.llm.cost || 0).toFixed(3)}</td><td style="font-size:12px">${esc((x.error || "").slice(0, 200))}</td></tr>`).join("")}</tbody></table></div>
  ${r.recent_errors.length ? `<div class="panel"><h3 style="margin-top:0">Последние ошибки API</h3>${r.recent_errors.map((e) => `<div class="flag">${new Date(e.ts * 1000).toLocaleString("ru")} · ${e.purpose}: ${esc(e.error)}</div>`).join("")}</div>` : ""}`;
}
function evalHtml(e) {
  const m = e.overall || {};
  return `<div class="panel"><h3 style="margin-top:0">Качество на эталоне (${esc(e.gold_file || "")}, ${e.n_gold} эталонных утверждений)</h3>
  <div class="kpis">${["precision", "recall", "f1"].map((k) => `<div class="kpi"><span class="muted">${k}</span><b>${m[k] == null ? "—" : (m[k] * 100).toFixed(1) + "%"}</b></div>`).join("")}
    <div class="kpi"><span class="muted">проверяемость цитат</span><b>${e.quote_verifiability == null ? "—" : (e.quote_verifiability * 100).toFixed(1) + "%"}</b></div>
    <div class="kpi"><span class="muted">доля повторов</span><b>${e.duplicate_rate == null ? "—" : (e.duplicate_rate * 100).toFixed(1) + "%"}</b></div></div>
  <table style="margin-top:8px"><thead><tr><th>Категория</th><th>n эталона</th><th>Precision</th><th>Recall</th><th>F1</th></tr></thead><tbody>
  ${Object.entries(e.by_category || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n_gold}</td><td>${v.precision == null ? "—" : (v.precision * 100).toFixed(1) + "%"}</td><td>${v.recall == null ? "—" : (v.recall * 100).toFixed(1) + "%"}</td><td>${v.f1 == null ? "—" : (v.f1 * 100).toFixed(1) + "%"}</td></tr>`).join("")}</tbody></table></div>`;
}

// --------------------------------------------------------------- журнал ---
async function renderEdits() {
  const es = await api(`/api/projects/${S.pid}/edits`);
  $("#main").innerHTML = `<div class="panel"><table><thead><tr><th>Время</th><th>Объект</th><th>Действие</th><th>Было</th><th>Стало</th><th>Комментарий</th></tr></thead><tbody>
  ${es.map((e) => `<tr><td>${new Date(e.ts * 1000).toLocaleString("ru")}</td><td>${e.entity} ${e.entity_id}</td><td>${esc(e.action)}</td>
    <td style="font-size:12px;max-width:300px">${esc(JSON.stringify(e.before) || "")}</td><td style="font-size:12px;max-width:300px">${esc(JSON.stringify(e.after) || "")}</td><td>${esc(e.comment || "")}</td></tr>`).join("") || `<tr><td colspan=6 class="muted">Правок нет</td></tr>`}</tbody></table></div>`;
}

loadProjects();
