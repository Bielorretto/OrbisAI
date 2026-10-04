// Petits comportements côté navigateur : panneau latéral, chronomètres, sélection ordonnée.

// ---------------------------------------------------------------- panneau latéral (fiche + agenda)
(function () {
  const drawer = document.getElementById("drawer");
  const backdrop = document.getElementById("drawer-backdrop");
  if (!drawer) return;

  function close() {
    drawer.classList.remove("open");
    backdrop.classList.remove("open");
  }

  document.addEventListener("click", async (event) => {
    const link = event.target.closest("[data-panel]");
    if (link) {
      event.preventDefault();
      drawer.innerHTML = '<p class="muted">Chargement…</p>';
      drawer.classList.add("open");
      backdrop.classList.add("open");
      const res = await fetch(link.dataset.panel);
      drawer.innerHTML = res.ok ? await res.text() : '<p class="muted">Impossible de charger la fiche.</p>';
      return;
    }
    if (event.target.closest("[data-close]") || event.target === backdrop) close();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
})();

// ---------------------------------------------------------------- chronomètres
// L'heure de l'application peut être décalée (démo) : on part de l'heure serveur rendue dans la page.
(function () {
  const nodes = document.querySelectorAll("[data-since]");
  if (!nodes.length) return;
  const loadedAt = Date.now();
  function tick() {
    nodes.forEach((node) => {
      const since = new Date(node.dataset.since).getTime();
      const serverNow = new Date(node.dataset.now).getTime() + (Date.now() - loadedAt);
      const s = Math.max(0, Math.floor((serverNow - since) / 1000));
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      node.textContent = `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
    });
  }
  tick();
  setInterval(tick, 1000);
})();

// ---------------------------------------------------------------- sélection ordonnée (écran d'attribution)
function initSelection(min) {
  const list = document.getElementById("selection");
  const inputs = document.getElementById("selection-inputs");
  const empty = document.getElementById("selection-empty");
  const validate = document.getElementById("validate");
  const count = document.getElementById("selection-count");
  const selected = []; // [{id, name, score}]

  function render() {
    list.innerHTML = "";
    inputs.innerHTML = "";
    selected.forEach((item, index) => {
      const li = document.createElement("li");
      li.draggable = true;
      li.dataset.index = index;
      const name = document.createElement("span");
      name.className = "grow";
      name.textContent = item.name;
      li.append(name,
        button("↑", "Monter", () => move(index, -1), index === 0),
        button("↓", "Descendre", () => move(index, 1), index === selected.length - 1),
        button("✕", "Retirer", () => remove(item.id)));
      list.append(li);
      inputs.insertAdjacentHTML("beforeend",
        `<input type="hidden" name="user_ids" value="${item.id}"><input type="hidden" name="score_${item.id}" value="${item.score}">`);
    });
    empty.hidden = selected.length > 0;
    validate.disabled = selected.length < min;
    count.textContent = selected.length < min
      ? `${selected.length} / ${min} minimum`
      : `${selected.length} collaborateur(s) dans l'ordre d'envoi`;
    document.querySelectorAll(".candidate").forEach((card) => {
      const isSelected = selected.some((s) => String(s.id) === card.dataset.id);
      card.classList.toggle("selected", isSelected);
      const add = card.querySelector(".add");
      if (add) add.textContent = isSelected ? "Retirer" : "Ajouter";
    });
  }

  function button(label, title, onClick, disabled) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn small";
    b.textContent = label;
    b.title = title;
    b.disabled = !!disabled;
    b.addEventListener("click", onClick);
    return b;
  }

  function move(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= selected.length) return;
    [selected[index], selected[target]] = [selected[target], selected[index]];
    render();
  }

  function remove(id) {
    const i = selected.findIndex((s) => String(s.id) === String(id));
    if (i >= 0) selected.splice(i, 1);
    render();
  }

  document.querySelectorAll(".candidate .add").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".candidate");
      if (selected.some((s) => String(s.id) === card.dataset.id)) return remove(card.dataset.id);
      selected.push({ id: card.dataset.id, name: card.dataset.name, score: btn.dataset.score });
      render();
    });
  });

  // Glisser-déposer pour réordonner
  let dragIndex = null;
  list.addEventListener("dragstart", (e) => { dragIndex = Number(e.target.dataset.index); });
  list.addEventListener("dragover", (e) => e.preventDefault());
  list.addEventListener("drop", (e) => {
    e.preventDefault();
    const li = e.target.closest("li");
    if (!li || dragIndex === null) return;
    const [item] = selected.splice(dragIndex, 1);
    selected.splice(Number(li.dataset.index), 0, item);
    dragIndex = null;
    render();
  });

  render();
}

// ---------------------------------------------------------------- assistant d'attribution (chatbot)
(function () {
  const panel = document.getElementById("assistant-panel");
  if (!panel) return;
  const toggle = document.getElementById("assistant-toggle");
  const list = document.getElementById("assistant-messages");
  const form = document.getElementById("assistant-form");
  const input = document.getElementById("assistant-input");
  const chips = document.getElementById("assistant-suggestions");
  const history = []; // [{role, content}] envoyé au serveur pour garder le fil de la conversation

  function open() { panel.hidden = false; toggle.hidden = true; input.focus(); }
  function close() { panel.hidden = true; toggle.hidden = false; }
  toggle.addEventListener("click", open);
  document.getElementById("assistant-close").addEventListener("click", close);

  function add(role, text, source) {
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    if (role === "assistant") div.innerHTML = renderLight(text);
    else div.textContent = text;
    if (source) {
      const s = document.createElement("span");
      s.className = "source";
      s.textContent = { mistral: "Mistral", "vérifié": "Réponse vérifiée par l'application" }[source]
        || "Mode simulé (sans clé Mistral)";
      div.append(s);
    }
    list.append(div);
    list.scrollTop = list.scrollHeight;
    return div;
  }

  // Markdown minimal : on échappe tout le HTML, puis on n'autorise que **gras** et les puces.
  function renderLight(text) {
    const escaped = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return escaped
      .replace(/^#{1,6}\s*(.+)$/gm, "<strong>$1</strong>")
      .replace(/^\s*(-{3,}|\*{3,})\s*$/gm, "")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/^\s*[-*•]\s+/gm, "• ")
      .replace(/\n{3,}/g, "\n\n");
  }

  async function ask(question) {
    if (!question.trim()) return;
    add("user", question);
    chips.hidden = true;
    const pending = add("assistant", "Mistral réfléchit…");
    pending.classList.add("pending");
    form.querySelector("button").disabled = true;
    try {
      const res = await fetch(panel.dataset.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: question, history }),
      });
      const data = await res.json();
      pending.remove();
      if (!res.ok) throw new Error(data.error || "Erreur");
      add("assistant", data.answer, data.source);
      history.push({ role: "user", content: question }, { role: "assistant", content: data.answer });
    } catch (e) {
      pending.remove();
      add("error", "Mistral n'a pas pu répondre : " + e.message);
    } finally {
      form.querySelector("button").disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value;
    input.value = "";
    ask(q);
  });

  JSON.parse(panel.dataset.suggestions || "[]").forEach((text) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.addEventListener("click", () => ask(text));
    chips.append(b);
  });
  add("assistant", "Bonjour ! Je peux vous expliquer le classement : pourquoi une personne est en tête, "
    + "pourquoi une autre est exclue, ou ce qui départage deux candidats.");
})();

// ---------------------------------------------------------------- formulaires dossier / tâche : analyse Mistral
// url : route d'analyse ; titleId : champ qui sert de titre ; defaultDays : échéance proposée par défaut
function initAnalysisForm(url, titleId, defaultDays) {
  const $ = (id) => document.getElementById(id);
  const deadline = $("deadline");
  if (deadline && !deadline.value) {
    const d = new Date(Date.now() + defaultDays * 864e5);
    const pad = (n) => String(n).padStart(2, "0");
    deadline.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T18:00`;
  }

  // Les sous-spécialités proposées dépendent du domaine choisi
  function filterSubs() {
    const domain = $("specialty_id").value;
    const select = $("sub_specialty_id");
    for (const option of select.options) {
      option.hidden = !!option.dataset.domain && option.dataset.domain !== domain;
    }
    if (select.selectedOptions[0]?.hidden) select.value = "";
  }
  $("specialty_id").addEventListener("change", filterSubs);
  filterSubs();

  $("analyze").addEventListener("click", async () => {
    const status = $("analyze-status");
    const box = $("analysis");
    const body = new FormData();
    body.append("title", $(titleId).value);
    body.append("description", $("description").value);
    status.textContent = "Analyse en cours…";
    try {
      const res = await fetch(url, { method: "POST", body });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Erreur");
      if (data.specialty_id) $("specialty_id").value = data.specialty_id;
      filterSubs();
      if (data.sub_specialty_id) $("sub_specialty_id").value = data.sub_specialty_id;
      if (data.task_type) $("task_type").value = data.task_type;
      $("complexity").value = data.complexity;
      $("estimated_hours").value = data.estimated_hours;
      $("ai_summary").value = data.summary;
      box.hidden = false;
      box.innerHTML = "";
      const strong = document.createElement("strong");
      strong.textContent = `Analyse IA (${data.source}) : `;
      box.append(strong, document.createTextNode(data.summary + " "));
      if (data.keywords.length) {
        const kw = document.createElement("div");
        kw.className = "small muted";
        kw.textContent = "Mots-clés : " + data.keywords.join(", ");
        box.append(kw);
      }
      status.textContent = "Champs pré-remplis : vérifiez-les avant de valider.";
    } catch (e) {
      status.textContent = "Analyse impossible : " + e.message;
    }
  });
}
