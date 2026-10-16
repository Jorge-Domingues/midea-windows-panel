/* Ponte de hardware para o painel (design do usuário, arquivo intocado).
   No navegador (sem pywebview): mantém o mock original do design.
   No app (com pywebview): os comandos vão direto pro ar via UDP 6444. */
(function () {
  "use strict";

  const ready = () => !!window.pywebview && !!window.pywebview.api;
  const api = () => window.pywebview.api;

  /* enums Midea → índices do design (modes/speeds/dirs do script do usuário) */
  const MODE_I = { COOL: 0, HEAT: 1, FAN_ONLY: 2, DRY: 3, SMART_DRY: 3, AUTO: 4 };
  const SPEED_I = { LOW: 0, MEDIUM: 1, HIGH: 2, AUTO: 1 };
  const DIR_I = { OFF: 0, VERTICAL: 1, HORIZONTAL: 1, BOTH: 1 };

  /* snapshot do python → estado do mock (s) */
  function apply(st) {
    if (!st || st.ok === false) return;
    if (st.power !== undefined) s.on = !!st.power;
    if (st.temp !== undefined && st.temp !== null) s.temp = Number(st.temp);
    if (st.mode && MODE_I[st.mode] !== undefined) s.mode = MODE_I[st.mode];
    if (st.fan && SPEED_I[st.fan] !== undefined) s.speed = SPEED_I[st.fan];
    if (st.swing && DIR_I[st.swing] !== undefined) s.dir = DIR_I[st.swing];
    if (st.eco !== undefined) s.eco = !!st.eco;
    if (st.display_on !== undefined && st.display_on !== null) s.led = !!st.display_on;
    if (Array.isArray(st.modes) && st.modes.length) s.__mods = st.modes;
    render();
  }

  function conn(text, ok, color) {
    const c = document.querySelector(".conn");
    if (!c) return;
    const last = c.childNodes[c.childNodes.length - 1];
    if (last && last.nodeType === 3) last.textContent = text;
    const dot = c.querySelector(".dot");
    if (dot) dot.style.background = color || (ok ? "" : "#f87171");
  }

  /* índice do design → nome do enum Midea (mesma ordem dos arrays do card).
     CYCLE_MODE = ordem do giro SEM aquecer (o AC não tem)+ */
  const IDX_MODE = ["COOL", "HEAT", "FAN_ONLY", "DRY", "AUTO"];
  const CYCLE_MODE = ["COOL", "FAN_ONLY", "DRY", "AUTO"];
  const IDX_FAN  = ["LOW", "MEDIUM", "HIGH"];
  const IDX_DIR  = ["OFF", "VERTICAL"];

  window.addEventListener("midea-device-selected", (event) => {
    const result = event.detail;
    if (!result || !result.ok) return;
    apply(result.state);
    conn("Atualizado", true);
  });

  /* tela de conectar fecha após descoberta com sucesso → sincroniza já */
  window.addEventListener("midea-sync-requested", sync);

  async function sync() {
    if (!ready()) return;
    try {
      const st = await api().refresh();
      /* polling passivo NÃO sobrescreve o selo: ele só reflete o que
         você apertou. Falha de rede sim vira "Sem conexão". */
      if (st && st.ok !== false) { apply(st); }
      else { conn("Sem conexão", false); }
    } catch (e) { conn("Sem conexão", false); }
  }

  /* executar um comando do usuário: selo passa por 3 estados —
     "Enviando…" (âmbar, feedback óptico imediato) → "Atualizado" (ar
     confirmou) ou "Não aplicado" (falhou → sincroniza a verdade). */
  async function act(call) {
    if (!ready()) return false;
    conn("Enviando…", true, "#fbbf24");
    try {
      const st = await call();
      if (!st || st.ok === false) {
        conn("Não aplicado", false);
        sync();
        return false;
      }
      apply(st);
      conn("Atualizado", true);
      return true;
    } catch (e) {
      conn("Não aplicado", false);
      sync();
      return false;
    }
  }

  /* ---------- UI óptima + coalescência ----------
     1. o card reage NA HORA do clique (estado local + render);
     2. comandos coalescidos: cliques rápidos viram UM envio
        com o VALOR FINAL (a última intenção vence);
     3. o Python confirma contra o ar e reaplica o alvo se necessário. */
  const pend = {};
  function schedule(kind, delay, call) {
    clearTimeout(pend[kind]);
    pend[kind] = setTimeout(call, delay);
  }

  const clamp = (t) => Math.min(30, Math.max(16, t));

  function updTemp(t) {
    t = clamp(t);
    s.temp = t; render(true);                     // óptico imediato
    if (ready()) schedule("temp", 600, () => act(() => api().set_temp(t)));
  }

  /* re-vincula os botões do design (sobrepõe os handlers de mock) */
  $("plus").onclick  = () => updTemp(s.temp + 1);
  $("minus").onclick = () => updTemp(s.temp - 1);

  slider.oninput = (e) => setTemp(+e.target.value);          // visual só
  slider.onchange = (e) => updTemp(+e.target.value);         // envia real

  $("speed").onclick = () => {
    s.speed = (s.speed + 1) % speeds.length; render(true);   // óptico
    if (ready()) schedule("fan", 300,
      () => act(() => api().set_fan(IDX_FAN[s.speed])));
  };
  $("dir").onclick = () => {
    s.dir = (s.dir + 1) % dirs.length; render();
    if (ready()) schedule("dir", 300,
      () => act(() => api().set_dir(IDX_DIR[s.dir])));
  };
  $("mode").onclick = () => {
    const list = (Array.isArray(s.__mods) && s.__mods.length)
      ? s.__mods.filter((m) => m !== "HEAT") : CYCLE_MODE;
    const cur = IDX_MODE[s.mode] || "COOL";
    const nxt = list[(list.indexOf(cur) + 1) % list.length] || "COOL";
    if (MODE_I[nxt] !== undefined) { s.mode = MODE_I[nxt]; render(true); }
    if (ready()) schedule("mode", 300,
      () => act(() => api().set_mode(nxt)));
  };
  $("eco").onclick = () => {
    s.eco = !s.eco; render();
    if (ready()) schedule("eco", 300, () => act(() => api().set_eco(s.eco)));
  };
  $("power").onclick = () => {
    s.on = !s.on; render(true);
    if (ready()) schedule("power", 400, () => act(() => api().set_power(s.on)));
  };

  /* Esc esconde o painel (reencontrar pelo ícone da bandeja) */
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && ready()) api().hide_panel();
  });

  /* encaixa a janela no tamanho real do card (depois das fontes) */
  function fitPanel() {
    const r = document.querySelector(".card").getBoundingClientRect();
    if (ready()) api().fit(Math.ceil(r.width), Math.ceil(r.height));
    else setTimeout(fitPanel, 300);
  }
  (async () => {
    try { await document.fonts.ready; } catch (e) {}
    setTimeout(() => {
      fitPanel();
      document.documentElement.classList.add("appmode");
    }, 400);
  })();

  /* primeira sincronização + refresh periódico */
  window.addEventListener("pywebviewready", sync);
  setInterval(sync, 30000);
  window.pywebview && sync();
})();
