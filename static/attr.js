/* First-party attribution, loaded on every public page (before page scripts).
   - Remembers the last tracked touch (utm_*, lc, x, partner, gclid, fbclid, pv) for 30 days, so a visitor who
     lands on a /learn page, reads, then clicks through to the Check (or comes back tomorrow) is still
     credited to the experiment that brought them.
   - entry_page = the page that touch landed on (lets us compare landing pages).
   - TEST traffic: ?test=1 on the URL or the le_qa cookie (set once at /qa?on=1 on your own phone/laptop).
   - Phone-step wording variant (pv): ?pv= > experiment link > sticky per browser > random A/B.
   Never stores or sends contact details. */
(function () {
  const qs = new URLSearchParams(location.search);
  const S = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
              set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} } };
  const rid = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/-/g, "");
  let vid = S.get("le_vid"); if (!vid) { vid = rid(); S.set("le_vid", vid); }
  const sid = (function () { try { let s = sessionStorage.getItem("le_sid"); if (!s) { s = rid(); sessionStorage.setItem("le_sid", s); } return s; } catch (e) { return rid(); } })();
  const KEYS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "lc", "x", "partner", "gclid", "fbclid", "pv"];
  // TEST persists for the whole browser session once ?test=1 is seen (so clicking from an article to the Check stays TEST)
  let sessTest = false; try { if (qs.get("test") === "1") sessionStorage.setItem("le_test", "1"); sessTest = sessionStorage.getItem("le_test") === "1"; } catch (e) {}
  const isTest = qs.get("test") === "1" || sessTest || /(?:^|;\s*)le_qa=1/.test(document.cookie);
  let attr = null;
  if (KEYS.some(k => qs.get(k) && k !== "pv")) {                       // a new tracked touch
    attr = { ts: Date.now(), entry: location.pathname };
    KEYS.forEach(k => { if (qs.get(k)) attr[k] = qs.get(k); });
    if (!attr.partner && qs.get("ref")) attr.partner = qs.get("ref");
    S.set("le_attr", JSON.stringify(attr));
  } else {
    try { attr = JSON.parse(S.get("le_attr") || "null"); } catch (e) { attr = null; }
    if (attr && Date.now() - (attr.ts || 0) > 30 * 864e5) attr = null;  // 30-day window
  }
  if (!attr && document.referrer) {                                     // untagged arrival from another site: classify the referrer
    try {
      const h = new URL(document.referrer).hostname.replace(/^www\./, "");
      if (h && h !== location.hostname) {
        const ai = /(chatgpt\.com|chat\.openai\.com|perplexity\.ai|gemini\.google\.com|copilot\.microsoft\.com|claude\.ai)$/.test(h);
        const se = /(^|\.)(google|bing|duckduckgo|yahoo|ecosia|brave)\./.test(h);
        attr = { ts: Date.now(), entry: location.pathname, utm_source: h.split(".").slice(-2, -1)[0] || h, utm_medium: ai ? "ai_answer" : se ? "organic" : "referral" };
        S.set("le_attr", JSON.stringify(attr));
      }
    } catch (e) {}
  }
  if (!attr) {                                                          // direct / untracked: remember where this session entered
    let entry = null; try { entry = sessionStorage.getItem("le_entry"); if (!entry) { entry = location.pathname; sessionStorage.setItem("le_entry", entry); } } catch (e) { entry = location.pathname; }
    attr = { entry };
  }
  let pv = qs.get("pv") || attr.pv || S.get("le_pv") || (Math.random() < 0.5 ? "A" : "B");
  pv = /^[A-Z]$/.test(pv) ? pv : "A";
  S.set("le_pv", pv);
  const fields = {
    utm_source: attr.utm_source || null, utm_medium: attr.utm_medium || null, utm_campaign: attr.utm_campaign || null,
    utm_content: attr.utm_content || null, utm_term: attr.utm_term || null, link_code: attr.lc || null, experiment_id: attr.x || null,
    partner_id: attr.partner || null, gclid: attr.gclid || null, fbclid: attr.fbclid || null, entry_page: attr.entry || location.pathname,
    phone_variant: pv, referrer: document.referrer || null, page_url: location.href, is_test: isTest ? 1 : 0
  };
  function track(event, step, data, extra) {
    try {
      fetch("/api/event", { method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
        body: JSON.stringify(Object.assign({ visitor_id: vid, session_id: sid, event, step, data }, fields, extra || {})) });
    } catch (e) {}
  }
  window.LEA = { vid, sid, fields, isTest, pv, track };
  document.addEventListener("DOMContentLoaded", () => {                  // pages without their own tracker (e.g. /learn)
    if (!window.LE_NO_AUTO_PV && !/^\/(admin|agent|partner|qa)(\/|$)/.test(location.pathname)) track("page_view", location.pathname, null, window.LE_PAGE || {});
  });
})();
