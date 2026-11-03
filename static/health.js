/* Health Coverage Check - branching, progressive-qualification flow (EN/ES).
   Config: window.HF = {flow, angleId, angle, lang, campaignId, served}. Everything copy-related comes from
   config/health_flow.json so new hooks/angles never require code changes. */
(function () {
  const HF = window.HF, F = HF.flow, ES = HF.lang === "es", $ = s => document.querySelector(s);
  const qs = new URLSearchParams(location.search);
  const L = (o) => (o && typeof o === "object") ? (ES ? o.es : o.en) : o;
  const tx = (en, es) => ES ? es : en;
  const LEA = window.LEA;                    // shared first-party attribution (static/attr.js)
  const vid = LEA.vid, sid = LEA.sid;
  const PV = (F.phone_copy && F.phone_copy[LEA.pv]) ? LEA.pv : "A", PC = (F.phone_copy || {})[PV] || {};   // phone-step wording variant
  const T = Object.assign({}, LEA.fields, { phone_variant: PV, utm_campaign: LEA.fields.utm_campaign || HF.campaignId, variant: HF.angleId, angle: HF.angleId,
    lang: HF.lang, page_url: location.href });
  function track(event, step, data) { LEA.track(event, step, data, { campaign: HF.campaignId, utm_campaign: T.utm_campaign, variant: HF.angleId, angle: HF.angleId, lang: HF.lang, page_url: location.href }); }
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const money = n => "$" + Math.round(n).toLocaleString("en-US");
  const fpl = n => F.facts.fpl_base + F.facts.fpl_add * (Math.max(1, n) - 1);

  /* ---------------- state */
  const A = { need: HF.angle.need || null };
  let screens = [], idx = 0, result = null, emailOnly = false, routeId = null, submittedRouted = null, submittedStatus = null, submittedOptOut = false;

  function build() {
    screens = [];
    if (!HF.angle.need) screens.push({ k: "need" });
    if (A.need && F.paths[A.need]) F.paths[A.need].forEach(q => screens.push({ k: "q", q }));
    screens.push({ k: "result" }); if (!HF.noBuyer) screens.push({ k: "contact" }, { k: "consent" }, { k: "done" });
  }
  build();

  const root = $("#steps");
  const prog = () => { $("#prog").style.width = Math.round((idx + 1) / Math.max(screens.length, 6) * 100) + "%"; };
  const visible = () => screens.filter(s => ["need", "q", "result"].includes(s.k) && shown(s));
  const stepLabel = s => { const v = visible(), i = v.indexOf(s); if (s.k === "need") return `<p class="stepno"><i class="senal"></i>${tx("Step", "Paso")} 1</p>`; return i < 0 ? "" : `<p class="stepno"><i class="senal"></i>${tx("Step", "Paso")} ${i + 1} ${tx("of", "de")} ${Math.max(v.length, i + 1)}</p>`; };
  const WHY = {
    age: tx("Why we ask: age changes the usual price range, and Medicare (65+) works differently.", "Por qué preguntamos: la edad cambia el rango de precios y Medicare (65+) funciona diferente."),
    income: tx("Why we ask: a rough yearly household income shows whether it falls in the range where help paying premiums usually applies. A range is fine; you can pick 'Not sure'.", "Por qué preguntamos: un ingreso anual aproximado del hogar muestra si cae en el rango donde normalmente hay ayuda con la prima. Un rango basta; puedes elegir 'No estoy seguro(a)'."),
    household: tx("Why we ask: the income ranges depend on household size.", "Por qué preguntamos: los rangos de ingreso dependen del tamaño del hogar."),
    lost_when: tx("Why we ask: the Special Enrollment window is usually 60 days.", "Por qué preguntamos: el periodo especial normalmente dura 60 días."),
    zip: tx("Why we ask: plans and rules depend on where you live. This check currently covers Florida ZIP codes.", "Por qué preguntamos: los planes y reglas dependen de dónde vives. Por ahora el chequeo cubre códigos postales de Florida.")
  };
  const why = id => WHY[id] ? `<p class="why">${esc(WHY[id])}</p>` : "";
  const back = () => idx > 0 && screens[idx].k !== "done" ? `<button class="back" type="button" id="back">&larr; ${tx("Back", "Atrás")}</button>` : "";

  function incomeOptions() {
    const n = Math.min(parseInt(A.household || "1", 10), 8), b = fpl(n);
    return [["lt100", tx("Under ", "Menos de ") + money(b)], ["100-150", money(b) + " - " + money(b * 1.5)], ["150-250", money(b * 1.5) + " - " + money(b * 2.5)],
      ["250-400", money(b * 2.5) + " - " + money(b * 4)], ["gt400", tx("Over ", "Más de ") + money(b * 4)], ["unsure", tx("Not sure", "No estoy seguro(a)")]];
  }

  function exitScreen(kind) {
    track("exit", kind);
    const es_ = HF.lang === "es", hg = es_ ? "https://www.cuidadodesalud.gov/es/" : "https://www.healthcare.gov/";
    const m = {
      medicare: tx("This check is for Marketplace coverage and doesn't cover Medicare. For official, free help: Medicare.gov or 1-800-MEDICARE (1-800-633-4227). We didn't save your answers.",
                   "Este chequeo es para cobertura del Mercado y no cubre Medicare. Para ayuda oficial y gratuita: Medicare.gov o 1-800-MEDICARE (1-800-633-4227). No guardamos tus respuestas."),
      area: tx("This check currently covers Florida ZIP codes only. For other states, HealthCare.gov shows your state's plans and dates. We didn't save your answers.",
               "Por ahora este chequeo cubre solo códigos postales de Florida. Para otros estados, CuidadoDeSalud.gov muestra los planes y fechas de tu estado. No guardamos tus respuestas.")
    }[kind];
    const link = kind === "area" ? `<p class="small"><a href="${hg}" rel="noopener">${es_ ? "CuidadoDeSalud.gov" : "HealthCare.gov"}</a></p>` : "";
    root.innerHTML = `<div class="step on"><div class="result"><b style="font-family:var(--serif);font-size:24px;display:block;line-height:1.15">${kind === "medicare" ? "Medicare" : tx("Not available for your ZIP yet", "Todavía no disponible para tu código postal")}</b><p class="small" style="margin-top:8px">${esc(m)}</p>${link}</div></div>`;
  }

  function computeResult() {
    const n = parseInt(A.household || (A.need === "individual" ? "1" : "1"), 10) || 1;
    const today = HF.today ? new Date(HF.today) : new Date();
    const oS = new Date(F.facts.oep_start + "T00:00:00"), d15 = new Date(F.facts.dec15 + "T23:59:59"), oE = new Date(F.facts.oep_end + "T23:59:59");
    const sep = A.need === "lost" && (A.lost_when === "soon" || A.lost_when === "recent");
    let window_, situation;
    if (today >= oS && today <= oE) { situation = "OEP_OPEN"; window_ = today <= d15 ? tx("Open Enrollment is open. Enroll by Dec 15 for coverage starting Jan 1.", "La Inscripción Abierta está activa. Inscríbete antes del 15 de diciembre para cobertura desde el 1 de enero.") : tx("Open Enrollment is open through Jan 15. Coverage would start Feb 1.", "La Inscripción Abierta sigue hasta el 15 de enero. La cobertura empezaría el 1 de febrero."); }
    else if (today < oS) { situation = sep ? "SEP_LIKELY" : "OEP_UPCOMING"; window_ = tx("Open Enrollment runs Nov 1 - Jan 15. Enroll by Dec 15 for coverage starting Jan 1.", "La Inscripción Abierta va del 1 de noviembre al 15 de enero. Inscríbete antes del 15 de diciembre para cobertura desde el 1 de enero."); }
    else { situation = sep ? "SEP_LIKELY" : "OUTSIDE_OEP"; window_ = tx("Open Enrollment for 2027 has closed; you'd need a qualifying life event to enroll now.", "La Inscripción Abierta 2027 cerró; necesitarías un evento de vida que califique para inscribirte ahora."); }
    if (sep) window_ += " " + tx("Losing coverage usually opens a 60-day Special Enrollment Period - don't wait.", "Perder la cobertura normalmente abre un Periodo Especial de Inscripción de 60 días. No esperes.");
    let pcFlag = null;
    if (A.need === "plan_change") {                                        // 2027 discontinuation / renewal letters
      const exiting = (F.facts.exiting_2027 || []).includes(A.current_insurer);
      if (A.letter_type === "ending" || exiting) {
        pcFlag = A.bought_where === "direct" ? "plan_ending_offexchange" : "plan_ending";
        window_ = tx("Your current plan ends Dec 31, 2026. If you do nothing, HealthCare.gov says it will move you to a plan from a different insurance company - one you didn't pick, whose doctor network may not include yours. Choose your own 2027 plan by Dec 15 for Jan 1 coverage (Open Enrollment runs to Jan 15). Check your doctors and medicines first.",
                     "Tu plan actual termina el 31 de diciembre de 2026. Si no haces nada, CuidadoDeSalud.gov dice que te inscribirá en un plan de otra compañía de seguros, que tú no escogiste y cuya red puede no incluir a tus médicos. Elige tu propio plan 2027 antes del 15 de diciembre para cobertura desde el 1 de enero (la Inscripción Abierta sigue hasta el 15 de enero). Revisa primero tus médicos y medicinas.");
        if (A.bought_where === "direct") window_ = tx("You bought this plan directly from the insurance company, so nobody moves you automatically: when the company leaves, your plan simply ends Dec 31, 2026. Losing it opens a 60-day Special Enrollment Period, but choose by Dec 15 so you're covered on Jan 1.",
                     "Compraste este plan directamente con la aseguradora, así que nadie te pasa a otro plan automáticamente: cuando la compañía se va, tu plan simplemente termina el 31 de diciembre de 2026. Perderlo abre un Periodo Especial de Inscripción de 60 días, pero elige antes del 15 de diciembre para tener cobertura el 1 de enero.");
      } else {
        pcFlag = "renewal_increase";
        window_ = tx("Open Enrollment (Nov 1 - Jan 15) is when you can switch plans for 2027. Compare before Dec 15 so a new plan starts Jan 1; if you do nothing, you're usually renewed at the new price.",
                     "La Inscripción Abierta (1 nov - 15 ene) es cuando puedes cambiar de plan para 2027. Compara antes del 15 de diciembre para que el plan nuevo empiece el 1 de enero; si no haces nada, normalmente te renuevan con el precio nuevo.");
      }
    }
    if (A.need === "lost" && A.lost_when === "older") window_ += " " + tx("If your coverage ended more than 60 days ago, that window has usually passed.", "Si tu cobertura terminó hace más de 60 días, normalmente ese periodo ya pasó.");
    const band = A.income;
    const b = fpl(n);
    let head, note, flags = [];
    if (band === "lt100") {
      head = tx("Below the subsidy range", "Por debajo del rango de subsidio");
      note = tx("Florida hasn't expanded Medicaid. Adults under 100% of the poverty level usually don't get Marketplace subsidies, and adults without children generally can't get Florida Medicaid. A licensed agent or a free Navigator can check what's available for your household.",
                "Florida no ha expandido Medicaid. Los adultos por debajo del 100% del nivel de pobreza normalmente no reciben subsidios del Mercado, y los adultos sin hijos generalmente no califican para Medicaid en Florida. Un agente con licencia o un Navegador gratuito puede revisar qué hay para tu hogar.");
      if (n === 1) flags.push("coverage_gap_risk");
    } else if (band === "gt400") {
      head = tx("Above the 2027 subsidy range", "Por encima del rango de subsidio 2027");
      note = tx("The extra subsidies ended after 2025. Above 400% of the poverty level (" + money(b * 4) + " for your household) there's generally no premium tax credit in 2027 under current law, so comparing plans carefully matters more.",
                "Los subsidios extra terminaron después de 2025. Por encima del 400% del nivel de pobreza (" + money(b * 4) + " para tu hogar) generalmente no hay crédito fiscal en 2027 según la ley actual, así que comparar planes con cuidado es aún más importante.");
    } else if (band === "unsure") {
      head = tx("Let's narrow it down", "Vamos a aclararlo");
      note = tx("For 2027, help with premiums is generally available between " + money(b) + " and " + money(b * 4) + " for your household size. A licensed agent can help you estimate.",
                "Para 2027, la ayuda con la prima generalmente aplica entre " + money(b) + " y " + money(b * 4) + " para tu tamaño de hogar. Un agente con licencia puede ayudarte a estimar.");
    } else {
      head = tx("You may qualify for help paying premiums", "Podrías calificar para ayuda con la prima");
      note = tx("Your estimated income is in the 100%-400% range, where premium tax credits generally apply. The amount depends on your county, ages and the plans available.",
                "Tu ingreso estimado está en el rango del 100% al 400%, donde generalmente aplica el crédito fiscal. La cantidad depende de tu condado, edades y los planes disponibles.");
      if (band === "100-150" || band === "150-250") note += " " + tx("At this income, some silver plans may also have lower out-of-pocket costs.", "Con este ingreso, algunos planes plata también podrían tener menos gastos de bolsillo.");
    }
    if (pcFlag) flags.push(pcFlag);
    const urgency = (pcFlag === "plan_ending" || pcFlag === "plan_ending_offexchange") ? "HIGH" : pcFlag === "renewal_increase" ? "MEDIUM" : (A.need === "lost" && A.lost_when !== "older") ? "HIGH" : (A.timing === "asap" ? "HIGH" : A.timing === "jan1" ? "MEDIUM" : A.timing === "renewal" ? "MEDIUM" : "LOW");
    return { head, window_, note, situation, urgency, product_category: n > 1 ? "ACA_FAMILY" : "ACA_INDIVIDUAL", flags, household: n };
  }

  /* ---------------- render */
  function render() {
    const s = screens[idx]; prog(); let h = "";
    if (s.k === "need") {
      h = stepLabel(s) + `<p class="why">${tx("We ask a few simple questions (ZIP, age, household size, rough income) only to show your enrollment window and the income range that usually gets help with premiums. We never ask for your name, phone, email, Social Security number or health conditions in this check.", "Hacemos unas preguntas sencillas (código postal, edad, tamaño del hogar e ingreso aproximado) solo para mostrarte tu periodo de inscripción y el rango de ingreso que normalmente recibe ayuda con la prima. En este chequeo nunca pedimos nombre, teléfono, correo, Seguro Social ni condiciones de salud.")}</p><p class="q">${tx("What are you looking for?", "¿Qué estás buscando?")}</p><div class="opts">${F.needs.map(n => `<button class="opt" data-v="${n.id}">${esc(L(n))}</button>`).join("")}</div>`;
    } else if (s.k === "q") {
      const q = F.questions[s.q];
      h = stepLabel(s) + `<p class="q">${esc(ES ? q.es : q.en)}</p>` + ((ES ? q.hint_es : q.hint_en) ? `<p class="hint">${esc(ES ? q.hint_es : q.hint_en)}</p>` : "") + why(s.q);
      if (q.type === "zip") h += `<input class="t" id="zipin" inputmode="numeric" maxlength="5" autocomplete="postal-code" value="${esc(A.zip || "")}"><div class="err" id="zerr"></div><button class="btn" id="zipgo">${tx("Continue", "Continuar")}</button>`;
      else {
        const opts = q.type === "income" ? incomeOptions() : q.options.map(o => [o[0], L(o[1])]);
        h += `<div class="opts">${opts.map(o => `<button class="opt ${A[s.q] === o[0] ? "sel" : ""}" data-v="${o[0]}">${esc(o[1])}</button>`).join("")}</div>`;
      }
      h += back();
    } else if (s.k === "result") {
      result = computeResult();
      const es_ = HF.lang === "es", hg = es_ ? "https://www.cuidadodesalud.gov/es/" : "https://www.healthcare.gov/";
      const guide = ({ lost: es_ ? "/aprende/me-despidieron-seguro-medico-florida" : "/learn", plan_change: es_ ? "/aprende/cigna-se-va-de-florida-2027" : "/learn", self_employed: es_ ? "/aprende" : "/learn" })[A.need] || (es_ ? "/inscripcion-abierta-2027" : "/open-enrollment-2027");
      h = `<p class="stepno"><i class="senal"></i>${tx("Your result", "Tu resultado")}</p><div class="chips"><span class="chip"><i class="senal"></i>${tx("Estimate, not a quote", "Estimado, no una cotización")}</span><span class="chip">${tx("Dates: HealthCare.gov", "Fechas: CuidadoDeSalud.gov")}</span></div>
        <div class="result"><div class="lbl">${tx("Your 2027 snapshot (estimate)", "Tu resumen 2027 (estimado)")}</div><div class="big" style="font-size:28px">${esc(result.head)}</div>
        <p class="small" style="margin:10px 0 0"><b>${tx("Enrollment window", "Periodo de inscripción")}:</b> ${esc(result.window_)}</p><p class="small" style="margin:8px 0 0">${esc(result.note)}</p>
        <p class="small" style="margin:8px 0 0">${tx("Estimate only - not an eligibility determination. The official decision comes from the Marketplace.", "Solo es un estimado, no una determinación de elegibilidad. La decisión oficial la toma el Mercado.")}</p></div>`;
      if (HF.noBuyer) {
        h += `<p class="q" style="font-size:20px">${tx("Your next step", "Tu siguiente paso")}</p><div class="next">
          <a href="${hg}" rel="noopener"><i class="senal" style="width:7px;height:15px;min-width:7px;margin-top:.25em"></i><span>${tx("Compare real plans and prices on HealthCare.gov", "Compara planes y precios reales en CuidadoDeSalud.gov")}<small>${tx("The official site. Your ZIP, ages and income decide the real prices.", "El sitio oficial. Tu código postal, edades e ingreso deciden los precios reales.")}</small></span></a>
          <a href="https://localhelp.healthcare.gov/" rel="noopener"><i class="senal" style="width:7px;height:15px;min-width:7px;margin-top:.25em"></i><span>${tx("Find free local help (navigators and agents)", "Encuentra ayuda local gratuita (navegadores y agentes)")}<small>${tx("Run by the Marketplace. You choose who to contact; we don't pass your information to anyone.", "La ofrece el Mercado. Tú decides a quién contactar; no pasamos tu información a nadie.")}</small></span></a>
          <a href="${guide}"><i class="senal" style="width:7px;height:15px;min-width:7px;margin-top:.25em"></i><span>${tx("Read the guide for your situation", "Lee la guía de tu situación")}<small>${tx("Plain-language steps with official sources.", "Pasos en lenguaje sencillo, con fuentes oficiales.")}</small></span></a></div>
          <button class="btn ghost" type="button" id="printme">${tx("Print or save this summary", "Imprimir o guardar este resumen")}</button>
          <p class="small" style="margin-top:12px">${tx("We didn't ask for your name, phone or email, and we are not connecting you with anyone. We only count which steps people complete, with no personal data.", "No pedimos tu nombre, teléfono ni correo, y no te estamos conectando con nadie. Solo contamos qué pasos completan las personas, sin datos personales.")}</p>` + back();
      } else {
        h += `<p class="q" style="font-size:18px">${tx("Optional: want a licensed agent to contact you about real plans and prices?", "Opcional: ¿quieres que un agente con licencia te contacte sobre planes y precios reales?")}</p>
        <p class="small">${tx("If you continue, the next screens ask for your contact details and name the one agency that would receive them. Nothing is shared unless you check the consent box.", "Si continúas, las siguientes pantallas piden tus datos de contacto y nombran a la única agencia que los recibiría. No se comparte nada si no marcas la casilla de consentimiento.")}</p>
        <button class="btn" id="want">${tx("Yes, I want to be contacted", "Sí, quiero que me contacten")}</button><button class="btn ghost" id="emailme">${tx("Just email me this summary", "Solo envíame este resumen por correo")}</button>` + back();
      }
      track("tool_complete", "health_check", { head: result.head, situation: result.situation });
    } else if (s.k === "contact") {
      h = (emailOnly ? `<p class="q">${tx("Where should we send your summary?", "¿A dónde te enviamos el resumen?")}</p>
        <div class="row2"><div><label class="f">${tx("First name", "Nombre")}</label><input class="t" id="first_name" autocomplete="given-name"></div><div><label class="f">${tx("Last name", "Apellido")}</label><input class="t" id="last_name" autocomplete="family-name"></div></div>
        <label class="f">${tx("Email", "Correo electrónico")}</label><input class="t" id="email" type="email" autocomplete="email">
        <p class="small">${tx("Nobody will call you.", "Nadie te va a llamar.")}</p>`
      : `<p class="q">${esc(L(PC.q))}</p>
        <p class="hint">${esc(L(PC.hint))}</p>
        <div class="row2"><div><label class="f">${tx("First name", "Nombre")}</label><input class="t" id="first_name" autocomplete="given-name"></div><div><label class="f">${tx("Last name", "Apellido")}</label><input class="t" id="last_name" autocomplete="family-name"></div></div>
        <label class="f">${tx("Mobile phone", "Celular")}</label><input class="t" id="phone" type="tel" inputmode="tel" autocomplete="tel-national" placeholder="(305) 555-0123">
        <div class="err" id="perr"></div>
        <label class="f">${tx("Best way to reach you", "Mejor forma de contactarte")}</label><select class="t" id="preferred_contact"><option value="phone">${tx("Phone call", "Llamada")}</option><option value="text">${tx("Text", "Mensaje de texto")}</option></select>
        <label class="f">${tx("Best time", "Mejor horario")}</label><select class="t" id="best_time"><option value="anytime">${tx("Anytime (8am-8pm)", "Cualquier hora (8am-8pm)")}</option><option value="morning">${tx("Morning", "Mañana")}</option><option value="afternoon">${tx("Afternoon", "Tarde")}</option><option value="evening">${tx("Evening (until 8pm)", "Noche (hasta 8pm)")}</option></select>
        <label class="f">${tx("Email (optional, for a copy of your summary)", "Correo (opcional, para una copia de tu resumen)")}</label><input class="t" id="email" type="email" autocomplete="email">`);
      h += `<p class="why"><b>${tx("What we ask and why", "Qué pedimos y por qué")}:</b> ${tx("your name and phone are used only so the agency named on the next screen can contact you. No Social Security number, health conditions or immigration status. You can stop here and keep your summary.", "tu nombre y teléfono se usan solo para que la agencia que verás en la siguiente pantalla te contacte. No pedimos Seguro Social, condiciones de salud ni estatus migratorio. Puedes parar aquí y quedarte con tu resumen.")}</p><div class="err" id="cerr"></div><button class="btn" id="tocon">${tx("Continue", "Continuar")}</button>` + back();
    } else if (s.k === "consent") {
      h = `<p class="q">${tx("Last step", "Último paso")}</p><div id="ctext"><p class="small">…</p></div><div class="err" id="serr"></div>
        <button class="btn" id="submit" disabled>${emailOnly ? tx("Email my summary", "Enviar mi resumen") : tx("Connect me with an agent", "Conectarme con un agente")}</button>` + back();
    } else if (s.k === "done") {
      if (submittedOptOut) {
        // an earlier opt-out is honored, not overwritten by this form: say exactly that
        h = `<div class="result"><div class="lbl">${tx("Done", "Listo")}</div>
          <div class="big" style="font-size:24px">${tx("You asked us not to contact you.", "Nos pediste que no te contactáramos.")}</div>
          <p class="small">${tx("That request was already on our opt-out list, so we honored it: nothing was shared with any agency, and nobody will call, text or email you. We recorded only that you asked.", "Esa solicitud ya estaba en nuestra lista de cancelación, así que la respetamos: no se compartió nada con ninguna agencia y nadie te llamará, escribirá ni enviará correos. Solo registramos que lo pediste.")}</p></div>
          <p class="small"><a href="/optout">${tx("Review or update your opt-out", "Revisa o actualiza tu cancelación")}</a></p>`;
      } else if (submittedRouted === "NOT_ROUTABLE" && submittedStatus === "DUPLICATE") {
        // the engine refused this as a repeat submission — promising a call nobody will make would lie
        const es_ = HF.lang === "es", hg = es_ ? "https://www.cuidadodesalud.gov/es/" : "https://www.healthcare.gov/";
        h = `<div class="result"><div class="lbl">${tx("Done", "Listo")}</div>
          <div class="big" style="font-size:24px">${tx("We already have your request.", "Ya tenemos tu solicitud.")}</div>
          <p class="small">${tx("This number was submitted recently, so we didn't create a second request. Nothing was shared with a new agency — the original consent stands.", "Este número ya se envió recientemente, así que no se creó una segunda solicitud. No se compartió nada con una agencia nueva — el consentimiento original sigue en pie.")}</p>
          <a class="btn ghost" href="${hg}" rel="noopener">${tx("Compare plans yourself on HealthCare.gov", "Compara planes tú mismo en CuidadoDeSalud.gov")}</a></div>
          <p class="small">${tx("If nobody has called you yet, use the opt-out above or write to us through the contact page.", "Si nadie te ha llamado todavía, usa el enlace de cancelación de arriba o escríbenos por la página de contacto.")}</p>`;
      } else if (submittedRouted === "NOT_ROUTABLE") {
        const es_ = HF.lang === "es", hg = es_ ? "https://www.cuidadodesalud.gov/es/" : "https://www.healthcare.gov/";
        h = `<div class="result"><div class="lbl">${tx("Done", "Listo")}</div>
          <div class="big" style="font-size:24px">${tx("We couldn't pass your request on this time.", "No pudimos pasar tu solicitud esta vez.")}</div>
          <p class="small">${tx("Your request didn't meet the requirements the receiving agency has registered. Nothing was shared with anyone beyond your consent record.", "Tu solicitud no cumplió los requisitos registrados por la agencia receptora. No se compartió nada más allá de tu registro de consentimiento.")}</p>
          <a class="btn ghost" href="${hg}" rel="noopener">${tx("Compare plans yourself on HealthCare.gov", "Compara planes tú mismo en CuidadoDeSalud.gov")}</a></div>
          <p class="small">${tx("You can redo the check any time.", "Puedes repetir el chequeo cuando quieras.")} <a href="/optout">${tx("Opt out here", "Cancela aquí")}</a></p>`;
      } else if (!emailOnly && submittedRouted === "NO_BUYER") {
        // every eligible buyer was full when this person submitted (availability race at preview time):
        // never promise a call nobody is going to make — the honest outcome + real next steps instead
        const es_ = HF.lang === "es", hg = es_ ? "https://www.cuidadodesalud.gov/es/" : "https://www.healthcare.gov/";
        h = `<div class="result"><div class="lbl">${tx("Done", "Listo")}</div>
          <div class="big" style="font-size:24px">${tx("We couldn't connect you right now.", "No pudimos conectarte en este momento.")}</div>
          <p class="small">${tx("No licensed agent could take your request this minute, so nobody will call you.", "Ningún agente con licencia pudo tomar tu solicitud en este momento, así que nadie te va a llamar.")}
          ${A.email ? tx("The summary you saw is on its way to your email.", "El resumen que viste va en camino a tu correo.") : tx("Your summary is right here on this page — print or save it.", "Tu resumen está aquí — imprímelo o guárdalo.")}</p>
          <a class="btn ghost" href="${hg}" rel="noopener">${tx("Compare plans yourself on HealthCare.gov", "Compara planes tú mismo en CuidadoDeSalud.gov")}</a>
          </div>
          <p class="small">${tx("You can redo the check any time — agents take new requests as their availability changes.", "Puedes repetir el chequeo cuando quieras: la disponibilidad de los agentes cambia.")} <a href="/optout">${tx("Opt out here, once we have your contact details", "Cancela aquí si nos diste tus datos")}</a></p>`;
      } else {
        const byMethod = (A.preferred_contact === "text")
          ? tx("by text message", "por mensaje de texto")
          : tx("by phone call", "por llamada");
        h = `<div class="result"><div class="lbl">${tx("Done", "Listo")}</div><div class="big" style="font-size:24px">${emailOnly ? tx("Your summary is on its way.", "Tu resumen va en camino.") : tx("A licensed agent will reach out.", "Un agente con licencia te contactará.")}</div>
          <p class="small">${emailOnly ? tx("Nobody will call you.", "Nadie te va a llamar.") : tx("A licensed agent assigned by us will contact you using the method you chose.", "Un agente con licencia asignado por nosotros te contactará por el medio que elegiste.")}</p></div>
          <p class="small" style="margin:10px 0 0"><b>${tx("What happens next", "Qué pasa ahora")}</b></p>
          <ul class="small" style="margin:4px 0 10px 18px">
            ${emailOnly ? "" : `<li>${tx("One licensed agent contacts you " + byMethod + ". We never sell the same request to a second agency.", "Un agente con licencia te contacta " + byMethod + ". Nunca vendemos la misma solicitud a una segunda agencia.")}</li>`}
            <li>${tx("You pay nothing for this request. A licensed agent is paid by the insurance company, not by you.", "No pagas nada por esta solicitud. A un agente con licencia le paga la aseguradora, no tú.")}</li>
            <li>${tx("Your summary stays on this page, and the official sites are linked above if you prefer to compare plans yourself.", "Tu resumen se queda en esta página, y los sitios oficiales están enlazados arriba si prefieres comparar planes por tu cuenta.")}</li>
          </ul>
          <p class="small">${tx("Changed your mind?", "¿Cambiaste de opinión?")} <a href="/optout">${tx("Opt out here", "Cancela aquí")}</a></p>`;
      }
    }
    root.innerHTML = `<div class="step on">${h}</div>`;
    wire(s);
  }

  // conditional questions: "show_if": {"answer_key": ["value", ...]} or {"exiting_insurer": true}
  const shown = s => { if (s.k !== "q") return true; const c = (F.questions[s.q] || {}).show_if; if (!c) return true;
    return Object.entries(c).every(([k, v]) => k === "exiting_or_ending" ? ((F.facts.exiting_2027 || []).includes(A.current_insurer) || A.letter_type === "ending") : (v || []).includes(A[k])); };
  function next() { idx = Math.min(idx + 1, screens.length - 1); while (!shown(screens[idx]) && idx < screens.length - 1) { delete A[screens[idx].q]; idx++; } render(); const s = screens[idx]; track("step", s.k === "q" ? s.q : s.k); if (!HF.embed) { const a = $("#app"); if (a) window.scrollTo({ top: a.getBoundingClientRect().top + window.scrollY - 10, behavior: "smooth" }); } }

  function wire(s) {
    const b = $("#back"); if (b) b.onclick = () => { idx--; while (idx > 0 && !shown(screens[idx])) idx--; render(); };
    if (s.k === "need") root.querySelectorAll(".opt").forEach(o => o.onclick = () => {
      A.need = o.dataset.v; track("tool_start", "need:" + A.need);
      if (A.need === "medicare") return exitScreen("medicare");
      build(); idx = 0; next();
    });
    if (s.k === "q") {
      if (F.questions[s.q].type === "zip") {
        const go = () => { const z = $("#zipin").value.trim();
          if (!/^\d{5}$/.test(z)) { $("#zerr").textContent = tx("Enter a 5-digit ZIP.", "Escribe un código postal de 5 dígitos."); return; }
          const p = parseInt(z.slice(0, 3), 10); A.zip = z;
          if (!(p >= 320 && p <= 349)) return exitScreen("area");
          A.state = "FL"; if (idx === 0 || (idx === 1 && !HF.angle.need)) track("tool_start", "zip"); next(); };
        $("#zipgo").onclick = go; $("#zipin").onkeydown = e => { if (e.key === "Enter") go(); }; $("#zipin").focus();
      } else root.querySelectorAll(".opt").forEach(o => o.onclick = () => {
        A[s.q] = o.dataset.v;
        if (s.q === "age" && A.age === "65+") return exitScreen("medicare");
        if (s.q === "household" && !A.household) A.household = o.dataset.v;
        next();
      });
    }
    if (s.k === "result" && HF.noBuyer) { const pm = $("#printme"); if (pm) pm.onclick = () => { track("print_summary", "result"); window.print(); }; }
    else if (s.k === "result") {
      $("#want").onclick = () => { emailOnly = false; track("form_start", "full"); next(); };
      $("#emailme").onclick = () => { emailOnly = true; track("form_start", "email_only"); next(); };
      if (!HF.emailFallback || qs.get("eo") === "0") $("#emailme").style.display = "none";   // we don't email consumers by default (lead gen only)
      // no licensed agent can take this person right now -> don't ask for a phone we couldn't legally pass on
      fetch(`/api/route-preview?flow=check&test=${T.is_test}&campaign=${encodeURIComponent(HF.campaignId)}&state=FL&age_range=${encodeURIComponent(A.age || "")}`)
        .then(r => r.json()).then(r => { if (!r.available && $("#want")) { $("#want").remove(); track("agent_unavailable", "result");
          const es_ = HF.lang === "es", hg = es_ ? "https://www.cuidadodesalud.gov/es/" : "https://www.healthcare.gov/";
          $("#emailme").insertAdjacentHTML("beforebegin", `<p class="small">${tx("No licensed agent is available to take new requests right now, and we won't collect your contact details we can't pass on. You can compare plans yourself at", "Ahora no hay un agente con licencia disponible para nuevas solicitudes, y no recogeremos tus datos de contacto si no podemos usarlos. Puedes comparar planes tú mismo en")} <a href="${hg}" rel="noopener">${es_ ? "CuidadoDeSalud.gov" : "HealthCare.gov"}</a>.</p>`);
          if (HF.emailFallback && qs.get("eo") !== "0") { $("#emailme").classList.remove("ghost"); $("#emailme").style.display = ""; } } })
        .catch(() => {});
    }
    if (s.k === "contact") {
      if (!emailOnly) track("phone_step_view", PV);
      const F_ = ["first_name", "last_name", "email", "phone", "preferred_contact", "best_time"];
      F_.forEach(k => { const el = $("#" + k); if (el && A[k]) el.value = A[k]; });
      const ph = $("#phone");
      if (ph) ph.oninput = () => {               // live (305) 555-0123 formatting
        let d = ph.value.replace(/\D/g, ""); if (d.length === 11 && d[0] === "1") d = d.slice(1); d = d.slice(0, 10);
        ph.value = d.length > 6 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : d.length > 3 ? `(${d.slice(0, 3)}) ${d.slice(3)}` : d;
        $("#perr").textContent = "";
      };
      const PERR = { not_10_digits: tx("Enter all 10 digits, area code first.", "Escribe los 10 dígitos, con el código de área."),
        toll_free_not_personal: tx("Please use your own mobile number, not a toll-free number.", "Usa tu número personal, no uno gratuito (800)."),
        missing: tx("Enter your phone number.", "Escribe tu número de teléfono.") };
      $("#tocon").onclick = async () => {
        F_.forEach(k => { const el = $("#" + k); if (el) A[k] = el.value.trim(); });
        const e = [];
        if (!A.first_name || !A.last_name) e.push(tx("Enter your name.", "Escribe tu nombre."));
        if ((emailOnly || A.email) && !/^[^@\s]+@[^@\s]+\.[a-z]{2,}$/i.test(A.email || "")) e.push(tx("Enter a valid email.", "Escribe un correo válido."));
        $("#cerr").textContent = e.join(" ");
        if (!emailOnly) {
          let r = { valid: false, reason: "missing" };
          try { r = await (await fetch("/api/phone-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ phone: A.phone, test: T.is_test }) })).json(); } catch (x) { r = { valid: (A.phone || "").replace(/\D/g, "").length >= 10, reason: "offline" }; }
          track("contact_submit", r.valid ? "phone_valid" : "phone_invalid:" + r.reason);     // pass/fail only - the number itself is never logged
          if (!r.valid) { $("#perr").textContent = PERR[r.reason] || tx("That number doesn't look right. Please check it.", "Ese número no parece correcto. Revísalo, por favor."); return; }
          if (r.pretty) { A.phone = r.pretty; ph.value = r.pretty; }
        } else track("contact_submit", "no_phone_email_only");
        if (!e.length) next();
      };
    }
    if (s.k === "consent") {
      const draw = (r) => {
        if (!r.available && !emailOnly) { emailOnly = true; if (!A.email) { idx--; render(); return; } }   // agent became unavailable: ask for email instead
        const text = emailOnly ? r.consent_email_only : r.consent_text;
        $("#ctext").innerHTML = (!r.available ? `<p class="small">${tx("No licensed agent is available right now, so we'll email your summary instead.", "Ahora no hay un agente disponible, así que te enviaremos el resumen por correo.")}</p>` : "") +
          (r.available && !emailOnly ? `<p class="why"><b>${tx("Who receives your details", "Quién recibe tus datos")}:</b> ${tx("one licensed agent, assigned by us after you check the box", "un agente con licencia, asignado por nosotros cuando marques la casilla")}. ${tx("No one else. Nothing is shared unless you check the box.", "Nadie más. No se comparte nada si no marcas la casilla.")}</p>` : "") + `<label class="consent"><input type="checkbox" id="cbox"><span>${esc(text)} <a href="${ES ? "/privacidad" : "/privacy"}" target="_blank">${tx("Privacy", "Privacidad")}</a> · <a href="${ES ? "/terminos" : "/terms"}" target="_blank">${tx("Terms", "Términos")}</a></span></label>`;
        $("#submit").textContent = emailOnly ? tx("Email my summary", "Enviar mi resumen") : tx("Connect me with an agent", "Conectarme con un agente");
        $("#cbox").onchange = e => $("#submit").disabled = !e.target.checked;
      };
      // the consent wording ALWAYS comes from the server (versioned + audited), so a failed load must
      // never fall back to a client-copied text: show the error and retry the same fetch instead.
      let trying = false;
      const load = () => { if (trying) return; trying = true;
        fetch(`/api/route-preview?flow=check&test=${T.is_test}&campaign=${encodeURIComponent(HF.campaignId)}&state=FL&age_range=${encodeURIComponent(A.age || "")}`)
          .then(r_ => { if (!r_.ok) throw 0; return r_.json(); }).then(r_ => { trying = false; routeId = r_; draw(r_); })
          .catch(() => { trying = false; $("#serr").textContent = tx("We couldn't load the consent step (network problem). Nothing was sent.", "No pudimos cargar el paso de consentimiento (problema de red). No se envió nada.");
            const r2 = document.createElement("button"); r2.className = "btn ghost"; r2.type = "button"; r2.textContent = tx("Try again", "Reintentar"); r2.onclick = load;
            $("#submit").insertAdjacentElement("beforebegin", r2); });
      };
      load();
      $("#submit").onclick = submit;
    }
  }

  function submit() {
    const btn = $("#submit"); btn.disabled = true; btn.textContent = tx("Sending...", "Enviando...");
    const n = result.household;
    const body = Object.assign({}, T, {
      visitor_id: vid, flow: "check", campaign: HF.campaignId, first_name: A.first_name, last_name: A.last_name, email: A.email, phone: A.phone, zip: A.zip, state: "FL",
      age_range: A.age, preferred_contact: A.preferred_contact, best_time: A.best_time, health_timeframe: A.timing || (A.need === "lost" ? "asap" : ""),
      need_path: A.need, household_size: n, income_band: A.income, enrollment_situation: result.situation, urgency: result.urgency,
      product_category: result.product_category, flags: result.flags, answers: A, tool_result: { tool: "health_check", headline: result.head, situation: result.situation },
      consent_checked: !emailOnly && $("#cbox").checked, email_consent_checked: emailOnly && $("#cbox").checked,
      form_snapshot: $("#app").outerHTML, website: ($("#website") || {}).value || ""
    });
    fetch("/api/lead", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(r => r.json()).then(r => {
        if (r.ok) { submittedRouted = r.routed; submittedStatus = r.status; submittedOptOut = !!r.opted_out; track("submit", r.status); try { if (window.fbq && !T.is_test) fbq("track", "Lead", {}, { eventID: r.lead_id }); if (window.gtag && !T.is_test) gtag("event", "generate_lead"); } catch (e) {} next(); }
        else {
          // honest failure states: the server can refuse for real reasons other than the checkbox,
          // and a person must never be stuck re-checking a box that is already ticked
          const M_ = { consent_required: tx("Please check the box above.", "Marca la casilla de arriba."),
                       not_accepting_requests: tx("We couldn't pass your request right now — no licensed agent could take new requests at this moment. Nothing was sent; your summary is on the previous screen.", "No pudimos pasar tu solicitud en este momento — ningún agente con licencia pudo tomar solicitudes nuevas. No se envió nada; tu resumen está en la pantalla anterior."),
                       too_many_requests: tx("Too many requests just now — wait a minute and try again. Nothing was sent.", "Demasiadas solicitudes ahora mismo — espera un minuto y vuelve a intentar. No se envió nada.") };
          $("#serr").textContent = M_[r.error] || (r.error ? tx("That didn't go through (" + r.error + "). Nothing was sent — wait a moment and try again.", "No se pudo enviar (" + r.error + "). No se envió nada — espera un momento y reintenta.") : tx("Please check the box above.", "Marca la casilla de arriba."));
          btn.disabled = false; btn.textContent = emailOnly ? tx("Email my summary", "Enviar mi resumen") : tx("Connect me with an agent", "Conectarme con un agente");
        }
      }).catch(() => { $("#serr").textContent = tx("Network error - try again.", "Error de conexión, intenta de nuevo."); btn.disabled = false; btn.textContent = emailOnly ? tx("Email my summary", "Enviar mi resumen") : tx("Connect me with an agent", "Conectarme con un agente"); });
  }

  track("page_view");
  render();
})();
