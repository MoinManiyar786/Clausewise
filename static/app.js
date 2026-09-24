/* ClauseWise front end.
 * Security: every piece of server or document text is inserted with textContent
 * (via the `el` helper). No innerHTML is used with dynamic data.
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const state = { busy: false, history: [], lastResult: null, lastKind: null };

  const RISK_LABEL = { high: "High risk", medium: "Worth checking", low: "Low risk" };
  const FAVORS = { you: "Favours you", "other party": "Favours the other side", balanced: "Balanced", unclear: "" };

  /** Create an element safely. Children may be strings, nodes, or null. */
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === "") continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const section = (title, ...body) => {
    const id = "h-" + title.toLowerCase().replace(/[^a-z]+/g, "-");
    return el("section", { class: "result-block", "aria-labelledby": id }, el("h3", { id, text: title }), ...body);
  };
  const list = (items, ordered = false) =>
    items && items.length ? el(ordered ? "ol" : "ul", {}, items.map((t) => el("li", { text: t }))) : null;
  const riskTag = (risk) => el("span", { class: `tag tag-${risk}`, text: RISK_LABEL[risk] || risk });

  /* ------------------------------------------------------------ context */
  function getContext() {
    return {
      role: $("#role").value,
      jurisdiction: $("#jurisdiction").value.trim(),
      reading_level: document.querySelector("input[name=reading_level]:checked").value,
      language: $("#language").value,
      redact_pii: $("#redact").checked,
    };
  }

  /* ------------------------------------------------------------ status + errors */
  function setStatus(message) { $("#status").textContent = message; }
  function showError(message) {
    const box = $("#error");
    box.textContent = message;
    box.hidden = !message;
  }

  async function api(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status}).`);
    return data;
  }

  async function run(label, task) {
    if (state.busy) return;
    state.busy = true;
    showError("");
    setStatus(label);
    document.querySelectorAll("button.primary").forEach((b) => { b.disabled = true; b.setAttribute("aria-busy", "true"); });
    try {
      await task();
      setStatus("Done. Results are below.");
    } catch (err) {
      setStatus("");
      showError(err.message || "Something went wrong.");
    } finally {
      state.busy = false;
      document.querySelectorAll("button.primary").forEach((b) => { b.disabled = false; b.removeAttribute("aria-busy"); });
    }
  }

  function docText() {
    const text = $("#doc-text").value.trim();
    if (text.length < 40) throw new Error("Paste or upload your document first (at least 40 characters).");
    return text;
  }

  /* ------------------------------------------------------------ results shell */
  function showResults(kind, data, nodes) {
    state.lastResult = data;
    state.lastKind = kind;
    const results = $("#results");
    results.replaceChildren(...nodes.filter(Boolean), resultToolbar(), metaNote(data.meta));
    results.hidden = false;
    results.focus({ preventScroll: true });
    results.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  function metaNote(meta) {
    if (!meta) return null;
    const bits = [meta.mode === "ai" ? "Explained by AI (Gemini)." : "AI unavailable: automatic pattern scan only."];
    const hidden = Object.values(meta.redactions || {}).reduce((a, b) => a + b, 0);
    if (hidden) bits.push(`${hidden} personal detail(s) were hidden before analysis.`);
    if (meta.cached) bits.push("Loaded from recent results.");
    return el("p", { class: "meta-note" }, bits.join(" "), el("br"), meta.disclaimer);
  }

  function resultToolbar() {
    return el("div", { class: "toolbar", role: "group", "aria-label": "Result actions" },
      el("button", { type: "button", class: "ghost", onclick: downloadResult, text: "Download as text" }),
      el("button", { type: "button", class: "ghost", onclick: () => window.print(), text: "Print" }),
      "speechSynthesis" in window
        ? el("button", { type: "button", class: "ghost", id: "speak", "aria-pressed": "false", onclick: toggleSpeech, text: "Read summary aloud" })
        : null);
  }

  function helpBanner(help, urgency) {
    if (!help || help.level === "optional") return null;
    const urgent = help.level === "now";
    return el("div", { class: `banner ${urgent ? "banner-urgent" : "banner-advice"}`, role: urgent ? "alert" : "note" },
      el("strong", { text: urgent ? "Get legal help soon." : "Consider talking to a professional." }),
      list(help.reasons),
      urgency && urgency.shortest_deadline_days !== null && urgency.shortest_deadline_days !== undefined
        ? el("p", { text: `Shortest deadline found: ${urgency.shortest_deadline_days} day(s). Check the exact date in the document.` })
        : null,
      urgent ? el("p", { text: "Many places have free legal aid services. Use the “Prepare for a lawyer” tab to get ready quickly." }) : null);
  }

  function quote(text, verified) {
    if (!text) return null;
    return el("figure", { class: "quote" },
      el("blockquote", { text }),
      el("figcaption", { class: verified ? "verified" : "unverified",
        text: verified ? "Found in your document" : "Exact wording not found in your document. Check it yourself." }));
  }

  /* ------------------------------------------------------------ 1. understand */
  function markedDocument(doc, clauses) {
    const spans = clauses.filter((c) => c.span).map((c) => ({ start: c.span[0], end: c.span[1], risk: c.risk, title: c.title }))
      .sort((a, b) => a.start - b.start);
    const text = doc.text;
    const out = [];
    let cursor = 0;
    for (const s of spans) {
      if (s.start < cursor) continue; // skip overlaps
      out.push(text.slice(cursor, s.start));
      out.push(el("mark", { class: `hl hl-${s.risk}`, title: `${RISK_LABEL[s.risk]}: ${s.title}` },
        text.slice(s.start, s.end), el("span", { class: "visually-hidden", text: ` (${RISK_LABEL[s.risk]}: ${s.title})` })));
      cursor = s.end;
    }
    out.push(text.slice(cursor));
    return el("details", { class: "marked-doc", open: spans.length ? true : null },
      el("summary", { text: `Your document, marked up (${spans.length} passage${spans.length === 1 ? "" : "s"})` }),
      el("p", { class: "legend", "aria-hidden": "true" },
        el("mark", { class: "hl hl-high", text: "High risk" }), " ",
        el("mark", { class: "hl hl-medium", text: "Worth checking" }), " ",
        el("mark", { class: "hl hl-low", text: "Low risk" })),
      el("div", { class: "doc-view", tabindex: "0", "aria-label": "Document with highlighted clauses" }, out));
  }

  function renderAnalysis(d) {
    const rc = d.risk_counts;
    const facts = el("dl", { class: "facts" },
      el("div", {}, el("dt", { text: "Type" }), el("dd", { text: d.document.type_label })),
      el("div", {}, el("dt", { text: "Readability" }), el("dd", { text: d.document.readability.label })),
      el("div", {}, el("dt", { text: "High risk" }), el("dd", { text: String(rc.high) })),
      el("div", {}, el("dt", { text: "Worth checking" }), el("dd", { text: String(rc.medium) })));

    const clauseCards = d.clauses.map((c) => el("li", { class: `clause clause-${c.risk}` },
      el("div", { class: "clause-head" }, riskTag(c.risk), el("h4", { text: c.title }),
        FAVORS[c.who_it_favors] ? el("span", { class: "favors", text: FAVORS[c.who_it_favors] }) : null),
      quote(c.quote, c.verified),
      c.plain_meaning ? el("p", {}, el("strong", { text: "In plain words: " }), c.plain_meaning) : null,
      c.why_it_matters && c.why_it_matters !== c.plain_meaning ? el("p", {}, el("strong", { text: "Why it matters: " }), c.why_it_matters) : null));

    const table = (rows, cols) => rows.length ? el("div", { class: "table-wrap", tabindex: "0" },
      el("table", {}, el("thead", {}, el("tr", {}, cols.map(([, label]) => el("th", { scope: "col", text: label })))),
        el("tbody", {}, rows.map((r) => el("tr", {}, cols.map(([key]) => el("td", { text: r[key] || "—" }))))))) : null;

    const checklist = d.checklist.length ? el("ul", { class: "checklist" }, d.checklist.map((item, i) =>
      el("li", {}, el("input", { type: "checkbox", id: `chk-${i}` }), el("label", { for: `chk-${i}`, text: item })))) : null;

    showResults("analysis", d, [
      el("h2", { text: "What this document means for you" }),
      helpBanner(d.professional_help, d.urgency),
      d.meta.injection_warning ? el("div", { class: "banner banner-advice", role: "note",
        text: "This document contains text that looks like instructions to an AI. ClauseWise ignored it, but be cautious about where the document came from." }) : null,
      facts,
      section("Summary", el("p", { class: "summary", id: "summary-text", text: d.summary }), list(d.key_points)),
      markedDocument(d.document, d.clauses),
      d.clauses.length ? section("Clauses to look at", el("ol", { class: "clauses" }, clauseCards)) : null,
      d.obligations.length ? section("Who has to do what", table(d.obligations, [["party", "Who"], ["obligation", "Must do"]])) : null,
      d.deadlines.length ? section("Dates and deadlines", table(d.deadlines, [["what", "What"], ["when", "When"]])) : null,
      d.inconsistencies.length ? section("Unclear or inconsistent parts",
        el("ul", {}, d.inconsistencies.map((x) => el("li", {}, x.issue, quote(x.quote, x.verified))))) : null,
      d.missing_protections.length ? section("Protections that seem to be missing", list(d.missing_protections)) : null,
      d.key_terms.length ? section("Legal words explained",
        el("dl", { class: "terms" }, d.key_terms.map((t) => [el("dt", { text: t.term }), el("dd", { text: t.explanation })]))) : null,
      d.next_steps.length ? section("Your options and next steps", list(d.next_steps)) : null,
      checklist ? section("Checklist", checklist) : null,
    ]);
  }

  /* ------------------------------------------------------------ 2. ask */
  function renderAnswer(question, d) {
    const conf = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" }[d.confidence];
    const item = el("li", { class: "qa" },
      el("p", { class: "q" }, el("span", { class: "visually-hidden", text: "You asked: " }), question),
      el("div", { class: "a" },
        el("p", { class: "a-meta" }, el("span", { class: `tag tag-conf-${d.confidence}`, text: conf }),
          d.found_in_document ? " Based on your document." : " Not clearly covered by your document."),
        el("p", { text: d.answer }),
        d.grounding_warning ? el("p", { class: "unverified", text: d.grounding_warning }) : null,
        d.citations.map((c) => quote(c.quote, c.verified)),
        d.follow_up_questions.length ? el("div", { class: "followups" }, el("p", { text: "You could also ask:" }),
          d.follow_up_questions.map((q) => el("button", { type: "button", class: "chip", text: q,
            onclick: () => { $("#question").value = q; $("#question").focus(); } }))) : null));
    $("#qa-log").prepend(item);
    state.lastResult = d; state.lastKind = "answer";
  }

  /* ------------------------------------------------------------ 3. compare */
  function renderComparison(d) {
    const better = { a: "Version A is better for you", b: "Version B is better for you", same: "No real difference", unclear: "Depends on your situation" };
    const rc = d.risk_changes;
    showResults("comparison", d, [
      el("h2", { text: "How the two versions differ" }),
      el("dl", { class: "facts" },
        el("div", {}, el("dt", { text: "Text similarity" }), el("dd", { text: `${d.similarity}%` })),
        el("div", {}, el("dt", { text: "Risks added in B" }), el("dd", { text: String(rc.added_in_b.length) })),
        el("div", {}, el("dt", { text: "Risks removed in B" }), el("dd", { text: String(rc.removed_in_b.length) }))),
      section("Summary", el("p", { class: "summary", id: "summary-text", text: d.overview })),
      rc.added_in_b.length || rc.removed_in_b.length ? section("Risk patterns",
        rc.removed_in_b.length ? el("p", {}, el("strong", { text: "No longer in B: " }), rc.removed_in_b.join(", ")) : null,
        rc.added_in_b.length ? el("p", {}, el("strong", { text: "New in B: " }), rc.added_in_b.join(", ")) : null) : null,
      d.differences.length ? section("Changes that matter", el("ol", { class: "clauses" }, d.differences.map((x) =>
        el("li", { class: `clause clause-${x.risk}` },
          el("div", { class: "clause-head" }, riskTag(x.risk), el("h4", { text: x.topic })),
          el("div", { class: "versions" },
            el("div", {}, el("p", { class: "version-label", text: "Version A" }), el("p", { text: x.doc_a || "Not mentioned" })),
            el("div", {}, el("p", { class: "version-label", text: "Version B" }), el("p", { text: x.doc_b || "Not mentioned" }))),
          x.impact ? el("p", {}, el("strong", { text: "What it means: " }), x.impact) : null,
          el("p", { class: "favors", text: better[x.better_for_you] }))))) : null,
      d.only_in_a.length ? section("Only in version A", list(d.only_in_a)) : null,
      d.only_in_b.length ? section("Only in version B", list(d.only_in_b)) : null,
      d.questions_to_raise.length ? section("Questions to ask before signing", list(d.questions_to_raise)) : null,
    ]);
  }

  /* ------------------------------------------------------------ 4. prepare */
  function renderBrief(d) {
    const copy = async () => {
      try { await navigator.clipboard.writeText(d.email_draft); setStatus("Email copied."); }
      catch { setStatus("Couldn't copy automatically. Select the text and copy it."); }
    };
    showResults("brief", d, [
      el("h2", { text: "Your brief for a legal professional" }),
      helpBanner(d.professional_help, d.urgency),
      section("Summary", el("p", { class: "summary", id: "summary-text", text: d.situation_summary })),
      d.professional_type ? section("Who can help", el("p", { text: d.professional_type }), d.urgency_note ? el("p", { text: d.urgency_note }) : null) : null,
      d.questions_for_professional.length ? section("Questions to ask", list(d.questions_for_professional, true)) : null,
      d.documents_to_gather.length ? section("Documents to bring", list(d.documents_to_gather)) : null,
      d.facts_to_write_down.length ? section("Facts to write down first", list(d.facts_to_write_down)) : null,
      d.options.length ? section("Possible paths", el("ul", {}, d.options.map((o) =>
        el("li", {}, el("strong", { text: o.option }), o.pros ? el("p", { text: `Pros: ${o.pros}` }) : null, o.cons ? el("p", { text: `Cons: ${o.cons}` }) : null)))) : null,
      d.email_draft ? section("Draft email to book a consultation",
        el("pre", { class: "email", tabindex: "0", text: d.email_draft }),
        el("button", { type: "button", class: "ghost", onclick: copy, text: "Copy email" })) : null,
    ]);
  }

  /* ------------------------------------------------------------ download + speech */
  function toPlainText(value, indent = "") {
    if (Array.isArray(value)) return value.map((v) => toPlainText(v, indent + "  ")).join("\n");
    if (value && typeof value === "object") {
      return Object.entries(value)
        .filter(([k, v]) => !["span", "meta", "document", "verified", "source"].includes(k) && v !== "" && !(Array.isArray(v) && !v.length))
        .map(([k, v]) => `${indent}${k.replace(/_/g, " ")}:${typeof v === "object" ? "\n" : " "}${toPlainText(v, indent + "  ")}`).join("\n");
    }
    return `${indent}${value}`;
  }

  function downloadResult() {
    if (!state.lastResult) return;
    const body = `ClauseWise ${state.lastKind}\nGenerated ${new Date().toLocaleString()}\n\n${toPlainText(state.lastResult)}\n\n${state.lastResult.meta ? state.lastResult.meta.disclaimer : ""}\n`;
    const url = URL.createObjectURL(new Blob([body], { type: "text/plain;charset=utf-8" }));
    const a = el("a", { href: url, download: `clausewise-${state.lastKind}.txt` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function toggleSpeech(event) {
    const btn = event.currentTarget;
    if (speechSynthesis.speaking) { speechSynthesis.cancel(); btn.setAttribute("aria-pressed", "false"); btn.textContent = "Read summary aloud"; return; }
    const text = ($("#summary-text") || {}).textContent;
    if (!text) return;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.onend = () => { btn.setAttribute("aria-pressed", "false"); btn.textContent = "Read summary aloud"; };
    btn.setAttribute("aria-pressed", "true"); btn.textContent = "Stop reading";
    speechSynthesis.speak(utterance);
  }

  /* ------------------------------------------------------------ tabs (WAI-ARIA pattern) */
  function initTabs() {
    const tabs = [...document.querySelectorAll("[role=tab]")];
    const select = (tab) => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
      });
    };
    tabs.forEach((tab, i) => {
      tab.addEventListener("click", () => select(tab));
      tab.addEventListener("keydown", (e) => {
        const map = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 };
        if (!(e.key in map)) return;
        e.preventDefault();
        const next = tabs[(map[e.key] + tabs.length) % tabs.length];
        select(next); next.focus();
      });
    });
  }

  /* ------------------------------------------------------------ display prefs */
  function initDisplay() {
    let scale = 1;
    const apply = () => document.documentElement.style.setProperty("--text-scale", String(scale));
    $("#text-larger").addEventListener("click", () => { scale = Math.min(1.5, scale + 0.1); apply(); });
    $("#text-smaller").addEventListener("click", () => { scale = Math.max(0.9, scale - 0.1); apply(); });
    $("#contrast-toggle").addEventListener("click", (e) => {
      const on = document.documentElement.classList.toggle("high-contrast");
      e.currentTarget.setAttribute("aria-pressed", String(on));
    });
  }

  /* ------------------------------------------------------------ wiring */
  function init() {
    initTabs();
    initDisplay();
    const samples = window.CLAUSEWISE_SAMPLES || {};
    const count = () => { $("#doc-count").textContent = `${$("#doc-text").value.length.toLocaleString()} characters`; };
    $("#doc-text").addEventListener("input", count);

    $("#sample-button").addEventListener("click", () => {
      $("#doc-text").value = samples.leaseA || ""; $("#role").value = "tenant"; count();
      setStatus("Sample lease loaded. You are set as the tenant.");
    });
    $("#sample-b-button").addEventListener("click", () => {
      if (!$("#doc-text").value.trim()) { $("#doc-text").value = samples.leaseA || ""; count(); }
      $("#doc-text-b").value = samples.leaseB || "";
      setStatus("Sample revised lease loaded as version B.");
    });

    $("#file-input").addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;
      run(`Reading ${file.name}…`, async () => {
        const form = new FormData();
        form.append("file", file);
        const res = await fetch("/api/extract", { method: "POST", body: form });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || "Could not read the file.");
        $("#doc-text").value = data.text; count();
        setStatus(data.truncated ? "File loaded, but it was long and has been shortened." : `Loaded ${file.name}.`);
      }).finally(() => { e.target.value = ""; });
    });

    $("#run-analyze").addEventListener("click", () => run("Reading your document. This can take up to a minute…",
      async () => renderAnalysis(await api("/api/analyze", { text: docText(), context: getContext() }))));

    $("#ask-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const question = $("#question").value.trim();
      if (!question) { showError("Type a question first."); $("#question").focus(); return; }
      run("Looking for the answer in your document…", async () => {
        const data = await api("/api/ask", { text: docText(), question, history: state.history, context: getContext() });
        state.history = [...state.history, question].slice(-4);
        renderAnswer(question, data);
        $("#question").value = "";
      });
    });

    $("#run-compare").addEventListener("click", () => run("Comparing the two versions…", async () => {
      const b = $("#doc-text-b").value.trim();
      if (b.length < 40) throw new Error("Paste version B to compare (at least 40 characters).");
      renderComparison(await api("/api/compare", { text_a: docText(), text_b: b, context: getContext() }));
    }));

    $("#run-prepare").addEventListener("click", () => run("Building your brief…", async () => {
      renderBrief(await api("/api/prepare", { text: $("#doc-text").value.trim(), situation: $("#situation").value.trim(), context: getContext() }));
    }));

    fetch("/api/health").then((r) => r.json()).then((h) => {
      $("#ai-status").textContent = h.ai_available
        ? "AI explanations are on."
        : "AI is not configured, so you’ll get an automatic pattern scan. Add GEMINI_API_KEY to enable full explanations.";
    }).catch(() => { $("#ai-status").textContent = "Couldn’t reach the server."; });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
