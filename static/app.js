/* Lead Engine front-end: value tool -> result -> qualifying taps -> contact -> consent -> lead.
   Flow is driven entirely by window.LE (campaign config), so every campaign reuses this file. */
(function () {
  const LE = window.LE, C = LE.campaign, $ = (s, r) => (r || document).querySelector(s);
  const ES = LE.lang === "es";
  const STR = ES ? {
    back: "&larr; Atrás", want_q: "¿Quiere opciones reales de un agente con licencia?", want: "Sí, quiero ver mis opciones", emailme: "Solo envíenme mis resultados por correo",
    where_full: "¿Dónde le envía sus opciones el agente con licencia?", where_email: "¿A dónde le enviamos sus resultados?", first: "Nombre", last: "Apellido",
    email: "Correo electrónico", phone: "Teléfono celular", zip: "Código postal (ZIP)", how: "Mejor forma de contactarle", when: "Mejor horario",
    call: "Llamada", text: "Mensaje de texto", mail: "Correo", any: "Cualquier hora", am: "Mañana", pm: "Tarde", eve: "Noche", cont: "Continuar",
    last_step: "Último paso", review: "Revise y confirme.", loading: "Cargando...", get: "Ver mis opciones", sendme: "Enviarme mis resultados",
    sending: "Enviando...", set: "Listo", on_way: "Sus resultados van en camino.", preparing: "Estamos preparando sus opciones.",
    no_call: "Revise su correo. Nadie le va a llamar.", will_reach: "Un agente con licencia asignado por nosotros le contactará por el medio que eligió. Su resultado: ",
    changed: "¿Cambió de opinión?", optout: "Cancele aquí", e_name: "Escriba su nombre.", e_email: "Escriba un correo válido.", e_phone: "Escriba un teléfono de 10 dígitos.",
    e_zip: "Escriba un código postal de 5 dígitos.", e_box: "Marque la casilla de arriba.", e_net: "Error de conexión, intente de nuevo.",
    nobuyer: "Ahora mismo no hay un agente disponible para sus respuestas, así que le enviaremos sus resultados por correo.", other_state: "Otro estado",
    nomatch_t: "No pudimos conectarle en este momento.", nomatch_body: "Ningún agente con licencia pudo tomar su solicitud en este momento, así que nadie le va a llamar. Compara planes reales en el sitio oficial: ",
    nomatch_cta: "HealthCare.gov (sitio oficial)",
    dup_t: "Ya tenemos su solicitud.", dup_body: "Este número ya se envió recientemente, así que no se creó una segunda solicitud. No se compartió nada con una agencia nueva — el consentimiento original sigue en pie.",
    not_state: "Todavía no estamos en su estado.", privacy: "Privacidad", terms: "Términos"
  } : {
    back: "&larr; Back", want_q: "Want real options for this from a licensed agent?", want: "Yes - show me my options", emailme: "Just email me my results",
    where_full: "Where should your licensed agent send your options?", where_email: "Where should we send your results?", first: "First name", last: "Last name",
    email: "Email", phone: "Mobile phone", zip: "ZIP code", how: "Best way to reach you", when: "Best time", call: "Phone call", text: "Text", mail: "Email",
    any: "Anytime", am: "Morning", pm: "Afternoon", eve: "Evening", cont: "Continue", last_step: "Last step", review: "Review and confirm.", loading: "Loading...",
    get: "Get my options", sendme: "Email me my results", sending: "Sending...", set: "You're all set", on_way: "Your results are on the way.",
    preparing: "Your options are being prepared.", no_call: "Check your inbox. No one will call you.", will_reach: "A licensed agent assigned by us will reach out using the method you chose. Your estimate: ",
    changed: "Changed your mind?", optout: "Opt out here", e_name: "Please enter your name.", e_email: "Please enter a valid email.", e_phone: "Please enter a 10-digit phone number.",
    e_zip: "Please enter a 5-digit ZIP.", e_box: "Please confirm the box above.", e_net: "Network error - please try again.",
    nobuyer: "No licensed partner is available for your answers right now, so we'll email your results instead.", other_state: "Another state",
    nomatch_t: "We couldn't connect you right now.", nomatch_body: "No licensed agent could take your request this minute, so nobody will call you. Compare real plans on the official site: ",
    dup_t: "We already have your request.", dup_body: "This number was submitted recently, so we didn't create a second request. Nothing was shared with a new agency — the original consent stands.",
    not_state: "We're not in your state yet.", privacy: "Privacy", terms: "Terms"
  };
  const qs = new URLSearchParams(location.search);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };
  const rid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/-/g, "");
  let vid = store.get("le_vid"); if (!vid) { vid = rid(); store.set("le_vid", vid); }
  const sid = rid();
  const T = {
    utm_source: qs.get("utm_source"), utm_medium: qs.get("utm_medium"), utm_campaign: qs.get("utm_campaign") || C.id,
    utm_content: qs.get("utm_content"), utm_term: qs.get("utm_term"), link_code: qs.get("lc"), partner_id: qs.get("partner") || qs.get("ref"),
    gclid: qs.get("gclid"), fbclid: qs.get("fbclid"), referrer: document.referrer || null, page_url: location.href,
    is_test: (qs.get("test") === "1" || (window.LEA && LEA.isTest)) ? 1 : 0, variant: LE.variant || null
  };
  function track(event, step, data) {
    try {
      fetch("/api/event", { method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
        body: JSON.stringify(Object.assign({ visitor_id: vid, session_id: sid, campaign: C.id, event, step, data }, T)) });
    } catch (e) {}
  }
  const money = n => "$" + Math.round(Math.max(0, n || 0)).toLocaleString("en-US");
  const num = v => parseFloat(String(v || "").replace(/[^0-9.]/g, "")) || 0;
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ------------------------------------------------------------ value tools */
  const NFDA = { burial: 8300, cremation: 6280 }; // NFDA 2023 Member General Price List Study medians (burial excl. vault/cemetery)
  const TOOLS = {
    dime_calc: {
      title: "Let's run your numbers", hint: "Rough numbers are fine. Nothing here is saved unless you ask for options.",
      inputs: () => {
        const x = [["income", "Your yearly income", 60000], ["years", "Years your family would need it", 10, "yrs"],
          ["debts", "Debts (cards, car, loans)", 15000], ["mortgage", "Mortgage balance (0 if renting)", 0]];
        if ((C.tool_extra || []).includes("business_debt")) x.push(["business_debt", "Business loans / lease you personally guarantee", 0]);
        if ((C.tool_extra || []).includes("truck_note")) x.push(["truck_note", "Truck / equipment note", 0]);
        x.push(["kids", "Children still at home", 0, "#"], ["edu", "College fund per child (your estimate)", 0],
          ["existing", "Life insurance you already have", 0], ["savings", "Savings & investments", 5000]);
        return x;
      },
      compute: v => {
        const need = v.debts + v.income * v.years + v.mortgage + v.kids * v.edu + (v.business_debt || 0) + (v.truck_note || 0);
        const gap = need - v.existing - v.savings;
        const months = v.income > 0 ? (v.savings + v.existing) / (v.income / 12) : 0;
        return { headline: money(gap), label: "Estimated protection gap", amount: Math.max(0, gap),
          rows: [["Debts", v.debts], ["Income replacement (" + v.years + " yrs)", v.income * v.years], ["Mortgage", v.mortgage],
            ["Education", v.kids * v.edu], ["Business / truck obligations", (v.business_debt || 0) + (v.truck_note || 0)],
            ["Minus existing coverage", -v.existing], ["Minus savings", -v.savings]],
          note: "Your family could cover about " + months.toFixed(1) + " months of your income today with savings + existing coverage." };
      }
    },
    mortgage_gap: {
      title: "Your mortgage, without your paycheck", hint: "Estimates only.",
      inputs: () => [["balance", "Mortgage balance", 280000], ["payment", "Monthly mortgage payment", 2100],
        ["your_income", "Your yearly income", 65000], ["other_income", "Household income WITHOUT you (yearly)", 40000], ["existing", "Life insurance you already have", 0]],
      compute: v => {
        const share = v.other_income > 0 ? (v.payment * 12) / v.other_income : 1;
        return { headline: money(v.balance - v.existing), label: "Mortgage left unprotected", amount: Math.max(0, v.balance - v.existing),
          rows: [["Mortgage balance", v.balance], ["Existing coverage", -v.existing]],
          note: "Without you, the mortgage payment would take " + Math.round(share * 100) + "% of your household's remaining income." +
            (share > 0.35 ? " That's above the ~28-36% range lenders usually treat as affordable." : "") };
      }
    },
    income_calc: {
      title: "Your paycheck, replaced", hint: "70% is a common planning assumption - change it.",
      inputs: () => [["income", "Your yearly income", 60000], ["pct", "% of income your family would need", 70, "%"],
        ["years", "Years until your youngest is independent / you'd retire", 15, "yrs"], ["existing", "Life insurance you already have", 0]],
      compute: v => {
        const need = v.income * (v.pct / 100) * v.years - v.existing;
        return { headline: money(need), label: "Income replacement needed", amount: Math.max(0, need),
          rows: [["Yearly need", v.income * v.pct / 100], ["x years", v.years], ["Minus existing coverage", -v.existing]], note: "" };
      }
    },
    final_expense_calc: {
      title: "Final expenses, estimated", hint: "Funeral figures are NFDA 2023 national medians - local prices vary.",
      inputs: () => [["kind", "Burial or cremation?", "burial", "sel:burial=Burial with viewing,cremation=Cremation with viewing"],
        ["cemetery", "Cemetery / plot / headstone (your estimate)", 0], ["bills", "Medical or other final bills", 2000], ["extra", "Anything extra to leave family", 0]],
      compute: v => {
        const base = NFDA[v.kind] || NFDA.burial, total = base + v.cemetery + v.bills + v.extra;
        return { headline: money(total), label: "Estimated final expenses", amount: total,
          rows: [[v.kind === "cremation" ? "Funeral w/ viewing + cremation (median)" : "Funeral w/ viewing + burial (median)", base],
            ["Cemetery / plot", v.cemetery], ["Final bills", v.bills], ["Extra for family", v.extra]],
          note: "Source for funeral medians: National Funeral Directors Association 2023 General Price List Study." };
      }
    },
    se_checklist: {
      title: "What your old employer used to cover", hint: "Answer honestly - this builds your checklist.",
      inputs: () => [["health", "Do you have health coverage right now?", "no", "sel:yes=Yes,no=No,unsure=Not sure"],
        ["disability", "Would anything pay you if you couldn't work for 3 months?", "no", "sel:yes=Yes,no=No,unsure=Not sure"],
        ["life", "Do you have life insurance outside of a job?", "no", "sel:yes=Yes,no=No,unsure=Not sure"],
        ["income", "Monthly take-home income", 5000]],
      compute: (v, a) => {
        const items = [];
        const dep = a.dependents === "yes" || a.dependents === "soon";
        if (v.life !== "yes" && dep) items.push(["Life insurance", "People depend on your income and nothing replaces it if you die. A term policy is usually the lowest-cost fix.", 1]);
        if (v.disability !== "yes") items.push(["Income / disability protection", "3 months without work = " + money(v.income * 3) + " gone. Self-employed people have no sick pay.", 1]);
        if (v.health !== "yes") items.push(["Health coverage", "Going 1099 usually counts as a life event - you may qualify for a special enrollment window and subsidies.", 1]);
        if (v.life !== "yes" && !dep) items.push(["Life insurance (lower priority)", "No dependents yet - a small policy can lock in a low rate while you're young.", 2]);
        items.push(["Emergency fund", "Aim for 3-6 months of expenses: " + money(v.income * 3) + "-" + money(v.income * 6) + ".", 2]);
        return { checklist: items, headline: items.filter(i => i[2] === 1).length + " gaps", label: "Priority gaps found", amount: 0, rows: [], note: "" };
      }
    },
    need_quiz: {
      title: "6 honest questions", hint: "",
      inputs: () => [["q1", "Would anyone struggle financially if your income stopped?", "yes", "sel:yes=Yes,no=No"],
        ["q2", "Do you have a mortgage or debts someone else would inherit or co-signed?", "no", "sel:yes=Yes,no=No"],
        ["q3", "Do you have kids or plan to?", "no", "sel:yes=Yes,no=No"],
        ["q4", "Is your only life insurance through your job?", "no", "sel:yes=Yes,no=No,none=I have none"],
        ["q5", "Could your family pay a funeral (~$8k) without borrowing?", "yes", "sel:yes=Yes,no=No"],
        ["q6", "Do you want to leave money behind for someone?", "no", "sel:yes=Yes,no=No"]],
      compute: v => {
        const s = (v.q1 === "yes") * 3 + (v.q2 === "yes") * 2 + (v.q3 === "yes") * 2 + (v.q4 !== "no") * 1 + (v.q5 === "no") * 1 + (v.q6 === "yes") * 1;
        const verdict = s >= 5 ? ["You very likely need it", "People depend on you - coverage protects them from a real financial hit."]
          : s >= 2 ? ["You probably need some", "A modest policy covers the specific gaps you flagged."]
            : ["You may not need it right now", "Nobody depends on your income. Revisit when that changes (house, partner, kids)."];
        return { headline: verdict[0], label: "Your verdict", amount: 0, rows: [], note: verdict[1], verdict: s };
      }
    },
    qualify_quiz: {
      title: "Which types of policies may be open to you?", hint: "General guidance only - no medical details needed.",
      inputs: () => [["major", "Any hospital stay or major health event in the last 2 years?", "no", "sel:no=No,yes=Yes,unsure=Prefer not to say"],
        ["daily", "Do you need help with daily activities (bathing, dressing)?", "no", "sel:no=No,yes=Yes"]],
      compute: (v, a) => {
        const opts = [];
        const hr = a.health_rating || "good";
        if (v.daily === "no" && v.major === "no" && (hr === "excellent" || hr === "good")) opts.push(["Fully underwritten term or whole life", "Usually the lowest prices. Includes a health review."]);
        if (v.daily === "no") opts.push(["Simplified-issue life", "No medical exam - a short set of health questions instead."]);
        opts.push(["Guaranteed-issue life", "No health questions. Smaller amounts, higher cost, and usually a 2-3 year waiting period for full benefits."]);
        return { checklist: opts.map(o => [o[0], o[1], 1]), headline: opts.length + " policy types", label: "May be available to you", amount: 0, rows: [], note: "Only an insurer can confirm eligibility." };
      }
    },
    cost_factors: {
      title: "The 5 things that set your price", hint: "No prices are invented here - an agent pulls real quotes.",
      inputs: () => [["term", "How long do you want coverage?", "20", "sel:10=10 years,20=20 years,30=30 years,life=Lifetime"]],
      compute: (v, a) => {
        const age = { "18-29": 3, "30-39": 3, "40-49": 2, "50-59": 1, "60-69": 1, "70-85": 0 }[a.age_range] ?? 2;
        const tob = a.tobacco === "no" ? 3 : 0;
        const hl = { excellent: 3, good: 2, fair: 1, conditions: 0 }[a.health_rating] ?? 2;
        const amt = { "lt100k": 3, "100-250k": 3, "250-500k": 2, "500k-1m": 2, "1m+": 1, "unsure": 2 }[a.coverage_amount] ?? 2;
        const term = { "10": 3, "20": 2, "30": 1, "life": 0 }[v.term] ?? 2;
        const f = [["Age", age], ["Tobacco", tob], ["Health", hl], ["Amount", amt], ["Length", term]];
        const tot = f.reduce((s, x) => s + x[1], 0);
        return { bars: f, headline: tot >= 11 ? "Price-friendly profile" : tot >= 7 ? "Average profile" : "Specialist profile",
          label: "Your price profile", amount: 0, rows: [], note: "Biggest lever you control: " + (tob === 0 ? "quitting tobacco (insurers usually re-rate after 12 months)." : "locking in before your next birthday.") };
      }
    }
  };

  /* ACA 2027: premium tax credits use the 2026 HHS poverty guidelines (48 states). Enhanced credits ended after 2025,
     so for 2027 the 100%-400% FPL band applies (Florida did not expand Medicaid -> under 100% is the coverage gap). */
  const FPL2026 = n => 15960 + 5680 * (Math.max(1, n) - 1);
  TOOLS.aca_check = {
    title: ES ? "Sus números para 2027" : "Your 2027 numbers",
    hint: ES ? "Use su mejor estimado de ingresos del hogar para 2027 (si trabaja por su cuenta: ganancia neta)." : "Use your best estimate of 2027 household income (for self-employed: net profit).",
    inputs: () => [["hh", ES ? "Personas en su hogar (para impuestos)" : "People in your tax household", ({ me: 1, me_spouse: 2, me_kids: 3, family: 4 })[A.health_who] || 1, "#"],
      ["income", ES ? "Ingreso estimado del hogar en 2027" : "Estimated 2027 household income", 55000],
      ["cov", ES ? "Su cobertura ahora" : "Coverage right now", "none", ES ? "sel:none=Ninguna,ending=Mi cobertura del trabajo/plan termina pronto,marketplace=Plan del Mercado,employer=Plan del empleador,medicaid=Medicaid/CHIP" : "sel:none=None,ending=Job/plan coverage ending soon,marketplace=Marketplace plan,employer=Employer plan,medicaid=Medicaid/CHIP"],
      ["evt", ES ? "¿Pasó algo en los últimos 60 días?" : "Anything happen in the last 60 days?", "no", ES ? "sel:no=No,lostcov=Perdí mi seguro médico,moved=Me mudé de condado/código postal,married=Me casé,baby=Tuve o adopté un hijo,26=Cumplí 26 años" : "sel:no=No,lostcov=Lost health coverage,moved=Moved to a new county/ZIP,married=Got married,baby=Had or adopted a child,26=Turned 26"]],
    compute: v => {
      const fpl = FPL2026(v.hh), pct = v.income / fpl * 100, cap = fpl * 4;
      const today = new Date(), oepStart = new Date("2026-11-01"), oepEnd = new Date("2027-01-15T23:59:59"), dec15 = new Date("2026-12-15T23:59:59");
      let w;
      if (today >= oepStart && today <= oepEnd) w = today <= dec15 ? (ES ? "La Inscripción Abierta está activa. Inscríbase antes del 15 de diciembre para cobertura desde el 1 de enero." : "Open Enrollment is open now. Enroll by Dec 15 for coverage starting Jan 1.")
        : (ES ? "La Inscripción Abierta sigue hasta el 15 de enero. La cobertura empezaría el 1 de febrero." : "Open Enrollment is open through Jan 15. Coverage would start Feb 1.");
      else if (today < oepStart) w = ES ? "La Inscripción Abierta empieza el 1 de noviembre y termina el 15 de enero. Inscríbase antes del 15 de diciembre para cobertura desde el 1 de enero." : "Open Enrollment opens Nov 1 and runs to Jan 15. Enroll by Dec 15 for coverage starting Jan 1.";
      else w = ES ? "La Inscripción Abierta para 2027 ya cerró." : "Open Enrollment for 2027 has closed.";
      if (v.evt !== "no" || v.cov === "ending") w += ES ? " Su cambio reciente también podría darle un Periodo Especial de Inscripción (normalmente 60 días). Pregúntele a su agente." : " Your recent life event may also qualify you for a Special Enrollment Period (usually 60 days) - ask your agent.";
      let band, note;
      if (pct < 100) { band = ES ? "Menos del 100% del nivel de pobreza" : "Below 100% of the poverty level"; note = ES ? "Florida no expandió Medicaid, así que muchos adultos con este ingreso quedan sin opción de ayuda. Un agente con licencia o un Navegador puede revisar Medicaid y otras opciones para su hogar." : "Florida did not expand Medicaid, so many adults at this income fall into a coverage gap. A licensed agent or Navigator can check Medicaid and other options for your household."; }
      else if (pct <= 400) { band = ES ? "Dentro del rango de crédito fiscal" : "In the premium tax credit range"; note = ES ? "Su ingreso estimado está entre el 100% y el 400% del nivel de pobreza, así que podría calificar para ayuda con la prima mensual. La cantidad exacta depende de su condado, edad y los planes disponibles." : "Your estimated income is between 100% and 400% of the poverty level, so you may qualify for help paying monthly premiums. The exact amount depends on your county, age and the plans available."; }
      else { band = ES ? "Por encima del límite de subsidio 2027" : "Above the 2027 subsidy limit"; note = (ES ? "Los subsidios extra terminaron después de 2025. Por encima del 400% del nivel de pobreza (" : "The extra subsidies ended after 2025. Above 400% of the poverty level (") + money(cap) + (ES ? " para su tamaño de hogar) generalmente no hay crédito fiscal en 2027, así que comparar planes y redes con cuidado es aún más importante." : " for your household size) there's generally no premium tax credit in 2027, so comparing plans and network options carefully matters more."); }
      return { headline: band, label: ES ? "Su rango de subsidio 2027 (estimado)" : "Your 2027 subsidy range (estimate)", amount: 0,
        rows: [[ES ? "Tamaño del hogar" : "Household size", String(v.hh)], [ES ? "Su ingreso vs. nivel de pobreza" : "Your estimate vs. poverty level", Math.round(pct) + "%"], [ES ? "Límite del 400% para su hogar" : "400% limit for your household", cap]],
        note: w + " " + note + (ES ? " Solo es un estimado, no una determinación de elegibilidad." : " Estimate only - not an eligibility determination.") };
    }
  };

  /* ------------------------------------------------------------ flow */
  const A = {};            // answers
  let submittedRouted = null;   // what the server did with THIS submission (DELIVERED / NO_BUYER / …)
  let submittedStatus = null;   // save-status of THIS submission (SAVED / DUPLICATE / …) — drives the honest done screen
  let toolVals = {}, toolResult = null, emailOnly = false, route = null;
  const steps = LE.steps.slice();
  const first = steps.shift();
  const tool = TOOLS[C.tool];
  const screens = [];
  // Tools that read the answers (age, health, dependents...) go after the taps; pure calculators go first
  // so people get value on screen 2.
  const answerDriven = ["cost_factors", "qualify_quiz", "se_checklist", "aca_check"].includes(C.tool);
  screens.push({ kind: "q", q: first });
  if (answerDriven) steps.forEach(q => screens.push({ kind: "q", q }));
  screens.push({ kind: "tool" });
  screens.push({ kind: "result" });
  if (!answerDriven) steps.forEach(q => screens.push({ kind: "q", q }));
  screens.push({ kind: "contact" });
  screens.push({ kind: "consent" });
  screens.push({ kind: "done" });
  let idx = 0, toolStarted = false;
  const root = $("#steps");

  function progress() { $("#prog").style.width = Math.round((idx + 1) / screens.length * 100) + "%"; }
  function backBtn() { return idx > 0 && screens[idx].kind !== "done" ? '<button class="back" type="button" id="back">' + STR.back + '</button>' : ""; }

  function render() {
    const s = screens[idx];
    progress();
    let h = "";
    if (s.kind === "q") {
      const q = s.q;
      h = `<p class="q">${esc(q.label)}</p>`;
      if (q.type === "state") {
        h += `<div class="opts">${LE.states.map(st => `<button class="opt" data-v="${st}">${st === "FL" ? "Florida" : st}</button>`).join("")}<button class="opt" data-v="OTHER">${STR.other_state}</button></div>`;
      } else {
        const AL = { "18-29": [18, 29], "30-39": [30, 39], "40-49": [40, 49], "50-59": [50, 59], "60-69": [60, 69], "70-85": [70, 85] };
        const opts = q.id === "age_range" ? q.options.filter(o => !AL[o[0]] || (AL[o[0]][1] >= C.age_min && AL[o[0]][0] <= C.age_max)) : q.options;
        h += `<div class="opts">${opts.map(o => `<button class="opt ${A[q.id] === o[0] ? "sel" : ""}" data-v="${o[0]}">${esc(o[1])}</button>`).join("")}</div>`;
      }
      h += backBtn();
    } else if (s.kind === "tool") {
      h = `<p class="q">${esc(tool.title)}</p>${tool.hint ? `<p class="hint">${esc(tool.hint)}</p>` : ""}`;
      tool.inputs().forEach(([k, label, def, unit]) => {
        const val = toolVals[k] ?? def;
        if (unit && String(unit).startsWith("sel:")) {
          const opts = unit.slice(4).split(",").map(p => p.split("="));
          h += `<label class="f">${esc(label)}</label><select class="t" data-k="${k}">${opts.map(o => `<option value="${o[0]}" ${String(val) === o[0] ? "selected" : ""}>${esc(o[1])}</option>`).join("")}</select>`;
        } else {
          const shown = unit ? val : Number(val).toLocaleString("en-US");
          h += `<label class="f">${esc(label)}${unit && unit !== "#" ? " (" + unit + ")" : ""}</label><input class="t" inputmode="numeric" data-k="${k}" data-unit="${unit || "$"}" value="${unit ? val : "$" + shown}">`;
        }
      });
      h += `<button class="btn" id="calc">${esc(C.cta)}</button>` + backBtn();
    } else if (s.kind === "result") {
      const r = toolResult;
      h = `<div class="result"><div class="lbl">${esc(r.label)}</div><div class="big">${esc(r.headline)}</div>`;
      if (r.rows && r.rows.length) h += `<table class="break">${r.rows.filter(x => x[1]).map(x => `<tr><td>${esc(x[0])}</td><td>${typeof x[1] === "number" && x[0] !== "x years" ? (x[1] < 0 ? "-" + money(-x[1]) : money(x[1])) : esc(x[1])}</td></tr>`).join("")}</table>`;
      if (r.checklist) h += `<ol class="check">${r.checklist.map(i => `<li><span class="${i[2] === 1 ? "pri" : ""}">${esc(i[0])}</span> - ${esc(i[1])}</li>`).join("")}</ol>`;
      if (r.bars) h += r.bars.map(b => `<div class="bar ${b[1] >= 3 ? "good" : b[1] >= 2 ? "mid" : "low"}"><b>${esc(b[0])}</b><span><i style="width:${(b[1] + 1) * 25}%"></i></span></div>`).join("");
      if (r.note) h += `<p class="small" style="margin:10px 0 0">${esc(r.note)}</p>`;
      h += `</div><p class="q" style="font-size:18px">${STR.want_q}</p>
        <button class="btn" id="want">${STR.want}</button><button class="btn ghost" id="emailme">${STR.emailme}</button>` + backBtn();
    } else if (s.kind === "contact") {
      h = `<p class="q">${emailOnly ? STR.where_email : STR.where_full}</p>
        <div class="row2"><div><label class="f">${STR.first}</label><input class="t" id="first_name" autocomplete="given-name"></div><div><label class="f">${STR.last}</label><input class="t" id="last_name" autocomplete="family-name"></div></div>
        <label class="f">${STR.email}</label><input class="t" id="email" type="email" autocomplete="email">
        ${emailOnly ? "" : `<label class="f">${STR.phone}</label><input class="t" id="phone" type="tel" inputmode="tel" autocomplete="tel">`}
        <label class="f">${STR.zip}</label><input class="t" id="zip" inputmode="numeric" maxlength="5" autocomplete="postal-code">
        ${emailOnly ? "" : `<label class="f">${STR.how}</label><select class="t" id="preferred_contact"><option value="phone">${STR.call}</option><option value="text">${STR.text}</option><option value="email">${STR.mail}</option></select>
        <label class="f">${STR.when}</label><select class="t" id="best_time"><option value="anytime">${STR.any}</option><option value="morning">${STR.am}</option><option value="afternoon">${STR.pm}</option><option value="evening">${STR.eve}</option></select>`}
        <div class="err" id="cerr"></div><button class="btn" id="tocon">${STR.cont}</button>` + backBtn();
    } else if (s.kind === "consent") {
      h = `<p class="q">${STR.last_step}</p><p class="hint">${STR.review}</p><div id="ctext"><p class="small">${STR.loading}</p></div><div class="err" id="serr"></div>
        <button class="btn" id="submit" disabled>${emailOnly ? STR.sendme : STR.get}</button>` + backBtn();
    } else if (s.kind === "done") {
      if (!emailOnly && submittedStatus === "DUPLICATE" && submittedRouted === "NOT_ROUTABLE") {
        // the engine refused this as a repeat submission — promising a second round of options or a call
        // nobody will make would lie; the ORIGINAL request and its consent are the ones that count
        h = `<div class="result"><div class="lbl">${STR.set}</div><div class="big" style="font-size:24px">${STR.dup_t}</div>
          <p class="small">${STR.dup_body}</p>
          <p class="small">${STR.changed} <a href="/optout">${STR.optout}</a></p>`;
      } else if (!emailOnly && submittedRouted === "NO_BUYER") {
        // availability race at submit: never promise a call nobody is going to make
        h = `<div class="result"><div class="lbl">${STR.set}</div><div class="big" style="font-size:24px">${STR.nomatch_t}</div>
          <p class="small">${STR.nomatch_body}${A.email ? " " + STR.on_way : ""}</p>
          <a class="btn ghost" href="https://www.healthcare.gov/" rel="noopener">${STR.nomatch_cta}</a></div>
          <p class="small">${STR.changed} <a href="/optout">${STR.optout}</a></p>`;
      } else {
        h = `<div class="result"><div class="lbl">${STR.set}</div><div class="big" style="font-size:26px">${emailOnly ? STR.on_way : STR.preparing}</div>
          <p class="small">${emailOnly ? STR.no_call : STR.will_reach + "<b>" + esc(toolResult.headline) + "</b>."}</p></div>
          <p class="small">${STR.changed} <a href="/optout">${STR.optout}</a></p>`;
      }
    }
    root.innerHTML = `<div class="step on">${h}</div>`;
    wire(s);
  }

  function next() { idx = Math.min(idx + 1, screens.length - 1); render(); track("step", screens[idx].kind === "q" ? screens[idx].q.id : screens[idx].kind); if (!LE.embed) window.scrollTo({ top: $("#app").offsetTop - 10, behavior: "smooth" }); }

  function wire(s) {
    const b = $("#back"); if (b) b.onclick = () => { idx--; render(); };
    if (s.kind === "q") {
      root.querySelectorAll(".opt").forEach(o => o.onclick = () => {
        A[s.q.id] = o.dataset.v;
        if (s.q.id === "state" && o.dataset.v === "OTHER") { root.innerHTML = `<div class="result"><b>${STR.not_state}</b><p class="small">Right now our licensed partners only serve ${LE.states.join(", ")}. Nothing you entered was saved.</p></div>`; track("out_of_area", "state"); return; }
        next();
      });
    } else if (s.kind === "tool") {
      root.querySelectorAll("[data-k]").forEach(i => {
        i.addEventListener("focus", () => { if (!toolStarted) { toolStarted = true; track("tool_start", C.tool); } }, { once: true });
        if (i.tagName === "INPUT" && i.dataset.unit === "$") i.addEventListener("blur", () => { i.value = "$" + num(i.value).toLocaleString("en-US"); });
      });
      $("#calc").onclick = () => {
        root.querySelectorAll("[data-k]").forEach(i => toolVals[i.dataset.k] = i.tagName === "SELECT" ? i.value : num(i.value));
        if (!toolStarted) { toolStarted = true; track("tool_start", C.tool); }
        toolResult = tool.compute(toolVals, A);
        track("tool_complete", C.tool, { headline: toolResult.headline });
        next();
      };
    } else if (s.kind === "result") {
      $("#want").onclick = () => { emailOnly = false; track("form_start", "full"); next(); };
      $("#emailme").onclick = () => { emailOnly = true; track("form_start", "email_only"); next(); };
      if (!LE.emailFallback) $("#emailme").style.display = "none";   // lead gen only: we don't email consumers by default
    } else if (s.kind === "contact") {
      ["first_name", "last_name", "email", "phone", "zip", "preferred_contact", "best_time"].forEach(k => { const el = $("#" + k); if (el && A[k]) el.value = A[k]; });
      $("#tocon").onclick = () => {
        ["first_name", "last_name", "email", "phone", "zip", "preferred_contact", "best_time"].forEach(k => { const el = $("#" + k); if (el) A[k] = el.value.trim(); });
        const e = [];
        if (!A.first_name || !A.last_name) e.push(STR.e_name);
        if (!/^[^@\s]+@[^@\s]+\.[a-z]{2,}$/i.test(A.email || "")) e.push(STR.e_email);
        if (!emailOnly && (A.phone || "").replace(/\D/g, "").length < 10) e.push(STR.e_phone);
        if (!/^\d{5}$/.test(A.zip || "")) e.push(STR.e_zip);
        $("#cerr").textContent = e.join(" ");
        if (!e.length) next();
      };
    } else if (s.kind === "consent") {
      const st = A.state || LE.states[0];
      fetch(`/api/route-preview?test=${(qs.get("test")==="1" || (window.LEA && LEA.isTest))?1:0}&campaign=${encodeURIComponent(C.id)}&state=${encodeURIComponent(st)}&age_range=${encodeURIComponent(A.age_range || "")}`)
        .then(r => r.json()).then(r => {
          route = r;
          if (!r.available && !emailOnly) emailOnly = true;
          const text = emailOnly ? r.consent_email_only : r.consent_text;
          $("#ctext").innerHTML = (!r.available && !A._noted ? `<p class="small">${STR.nobuyer}</p>` : "") +
            `<label class="consent"><input type="checkbox" id="cbox"><span id="ctxt">${esc(text)} <a href="/privacy" target="_blank">${STR.privacy}</a> · <a href="/terms" target="_blank">${STR.terms}</a></span></label>`;
          $("#submit").textContent = emailOnly ? STR.sendme : STR.get;
          $("#cbox").onchange = e => $("#submit").disabled = !e.target.checked;
        });
      $("#submit").onclick = submit;
    }
  }

  function submit() {
    const btn = $("#submit"); btn.disabled = true; btn.textContent = STR.sending;
    const snapshot = $("#app").outerHTML;
    const body = Object.assign({}, T, A, {
      visitor_id: vid, campaign: C.id, answers: A, tool_result: { tool: C.tool, inputs: toolVals, headline: toolResult && toolResult.headline, amount: toolResult && toolResult.amount },
      consent_checked: !emailOnly && $("#cbox").checked,
      email_consent_checked: emailOnly && $("#cbox").checked, form_snapshot: snapshot, hpw: ($("#hpw") || {}).value || ""
      // 'website' (the old trap name) is accepted server-side for pre-update cached pages
    });
    fetch("/api/lead", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(r => r.json()).then(r => {        if (r.ok) { submittedRouted = r.routed; submittedStatus = r.status; track("submit", r.status); try { if (window.fbq && !T.is_test) fbq("track", "Lead", {}, { eventID: r.lead_id }); if (window.gtag && !T.is_test) gtag("event", "generate_lead"); } catch (e) {} next(); } else { $("#serr").textContent = STR.e_box; btn.disabled = false; } })
      .catch(() => { $("#serr").textContent = STR.e_net; btn.disabled = false; });
  }

  track("page_view");
  render();
})();
