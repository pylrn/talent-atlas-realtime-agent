(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const state = { socket: null, startedAt: 0, firstRetrievalAt: null, scenario: 'compound', streaming: false, traceCount: 0 };

  const scenarios = {
    compound: [
      'Find a backend engineer',
      'Find a backend engineer with Python and PostgreSQL',
      'Find a backend engineer with Python and PostgreSQL, plus payments experience',
      'Find a backend engineer with Python and PostgreSQL, plus payments experience, and at least five years in Bengaluru.'
    ],
    refine: [
      'Find a machine learning engineer with Python and production deployment experience.',
      'Find a machine learning engineer with Python and production deployment experience, preferably located in Pune.'
    ],
    suppress: ['Put that answer into two bullets.']
  };

  function connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${location.host}/live-rag/ws`);
    state.socket = socket;
    socket.addEventListener('open', () => setConnection(true));
    socket.addEventListener('close', () => { setConnection(false); setTimeout(connect, 1800); });
    socket.addEventListener('message', (message) => handleEvent(JSON.parse(message.data)));
  }

  function setConnection(online) {
    $('#socketDot').classList.toggle('online', online);
    $('#socketLabel').textContent = online ? 'Ephemeral session online' : 'Reconnecting';
  }

  function send(type, payload = {}) {
    if (state.socket?.readyState === WebSocket.OPEN) state.socket.send(JSON.stringify({ type, ...payload }));
  }

  function handleEvent(event) {
    addTrace(event);
    switch (event.type) {
      case 'session.started': resetVisuals(); break;
      case 'transcript.delta': setNode('controller'); break;
      case 'controller.decision': renderDecision(event); break;
      case 'query.decomposed': renderSubqueries(event); break;
      case 'tool.started': markBranch(event.query, 'active'); break;
      case 'tool.completed': markBranch(event.query, 'done'); break;
      case 'fusion.updated': renderCandidates(event); break;
      case 'answer.version': renderAnswer(event); break;
      case 'metrics.updated': renderMetrics(event); break;
      case 'retrieval.cancelled': $('#speechState').textContent = 'REVISING'; break;
      case 'error': $('#speechState').textContent = 'ERROR'; break;
    }
  }

  function renderDecision(event) {
    const decision = event.decision;
    $('#decisionMetric').textContent = decision;
    $('#controllerReadout').innerHTML = `<span class="decision-badge ${decision.toLowerCase()}">${decision}</span><p>${escapeHtml(event.reason)}</p>`;
    if (decision === 'RETRIEVE' && state.firstRetrievalAt === null) {
      state.firstRetrievalAt = performance.now() - state.startedAt;
      $('#ttfrMetric').textContent = `${Math.round(state.firstRetrievalAt)} ms`;
    }
    if (decision === 'SUPPRESS') setNode('answer');
  }

  function renderSubqueries(event) {
    setNode('decompose');
    const host = $('#retrievalBranches');
    host.innerHTML = event.subqueries.map((query, index) => {
      const reused = event.reused.includes(query);
      return `<div class="retrieval-branch${reused ? ' reused done' : ''}" data-query="${encodeURIComponent(query)}"><span>${reused ? 'reused from session' : `03.${index + 1} queued`}</span><b>${escapeHtml(query)}</b></div>`;
    }).join('');
  }

  function markBranch(query, status) {
    if (!query) return;
    const branch = document.querySelector(`[data-query="${CSS.escape(encodeURIComponent(query))}"]`);
    if (branch) {
      branch.classList.toggle('done', status === 'done');
      branch.querySelector('span').textContent = status === 'done' ? 'retrieval complete' : 'hybrid search running';
    }
  }

  function renderCandidates(event) {
    setNode('fusion');
    const candidates = event.candidates || [];
    const list = $('#candidateList');
    if (!candidates.length) return;
    list.innerHTML = candidates.map((candidate, index) => `
      <section class="candidate-card${index === 0 ? ' open' : ''}">
        <button class="candidate-summary" type="button" aria-expanded="${index === 0}">
          <span class="candidate-rank">${String(index + 1).padStart(2, '0')}</span>
          <span class="candidate-name"><b>${escapeHtml(candidate.full_name || 'Unnamed candidate')}</b><small>${escapeHtml([candidate.city, candidate.country, `${candidate.years_exp} yrs`].filter(Boolean).join(' · '))}</small></span>
          <span class="candidate-score">${Number(candidate.fusion_score).toFixed(4)}</span>
        </button>
        <div class="candidate-evidence">${(candidate.evidence || []).map(item => `<div class="evidence-item"><code>chunk:${escapeHtml(item.chunk_id)} · doc:${escapeHtml(item.document_id || 'n/a')}</code><p>${escapeHtml(item.content)}</p></div>`).join('')}</div>
      </section>`).join('');
    $$('.candidate-summary').forEach(button => button.addEventListener('click', () => {
      const card = button.closest('.candidate-card');
      card.classList.toggle('open');
      button.setAttribute('aria-expanded', card.classList.contains('open'));
    }));
  }

  function renderAnswer(event) {
    setNode('answer');
    $('#answerText').textContent = event.summary;
    $('#answerVersion').textContent = `VERSION ${event.version}`;
    $('#versionMetric').textContent = `v${event.version}`;
    $('#citationCount').textContent = `${event.citations.length} CITATIONS`;
    $('#answerStatus').textContent = event.retrieval_suppressed ? 'REUSED EVIDENCE' : 'CORPUS VERIFIED';
    $('#answerStatus').style.color = event.retrieval_suppressed ? 'var(--blue)' : 'var(--cyan-dark)';
  }

  function renderMetrics(event) {
    $('#latencyMetric').textContent = `${Math.round(event.retrieval_ms)} ms`;
    $('#coverageMetric').textContent = `${Math.round(event.citation_coverage * 100)}%`;
    $('#costMetric').textContent = `$${Number(event.estimated_cost_usd).toFixed(4)}`;
    $('#speechState').textContent = 'COMPLETE';
  }

  function setNode(name) {
    $$('.pipeline-node').forEach(node => node.classList.toggle('active', node.dataset.node === name));
  }

  function addTrace(event) {
    if (event.type === 'transcript.delta') return;
    const li = document.createElement('li');
    const detail = event.query || event.reason || event.message || (event.candidates ? `${event.candidates.length} candidates` : '');
    li.innerHTML = `<time>${String(Math.round(event.timestamp_ms || 0)).padStart(4, '0')}ms</time><code>${escapeHtml(event.type)}</code><p title="${escapeHtml(detail)}">${escapeHtml(detail)}</p>`;
    $('#eventLog').prepend(li);
    while ($('#eventLog').children.length > 28) $('#eventLog').lastElementChild.remove();
  }

  async function runScenario(name) {
    if (state.streaming) return;
    state.scenario = name;
    state.streaming = true;
    state.startedAt = performance.now();
    state.firstRetrievalAt = null;
    $('#speechState').textContent = 'STREAMING';
    $('.transcript-stage').classList.add('streaming');
    $$('.scenario').forEach(button => button.classList.toggle('is-active', button.dataset.scenario === name));

    const fragments = scenarios[name];
    for (let index = 0; index < fragments.length; index += 1) {
      const text = fragments[index];
      $('#transcriptText').textContent = text;
      $('#queryInput').value = text;
      send('transcript.chunk', { text, elapsed_ms: Math.round(performance.now() - state.startedAt), is_final: index === fragments.length - 1 });
      await delay(name === 'suppress' ? 350 : name === 'refine' ? 4200 : 1250);
    }
    $('.transcript-stage').classList.remove('streaming');
    state.streaming = false;
  }

  function resetVisuals() {
    state.startedAt = performance.now();
    state.firstRetrievalAt = null;
    $('#decisionMetric').textContent = 'WAIT'; $('#ttfrMetric').textContent = '—'; $('#latencyMetric').textContent = '—'; $('#coverageMetric').textContent = '—'; $('#versionMetric').textContent = 'v0';
    $('#answerVersion').textContent = 'VERSION 0'; $('#citationCount').textContent = '0 CITATIONS'; $('#answerStatus').textContent = 'NO ANSWER';
    $('#answerText').textContent = 'The answer will be assembled only from retrieved corpus evidence.';
    $('#candidateList').innerHTML = '<div class="empty-state"><span>NO EVIDENCE YET</span><p>Candidate cards will carry the exact chunk and document identifiers used for grounding.</p></div>';
    $('#retrievalBranches').innerHTML = '<div class="branch-placeholder">Subqueries appear here as the utterance becomes specific.</div>';
    $('#controllerReadout').innerHTML = '<span class="decision-badge wait">WAIT</span><p>Waiting for a stable corpus-seeking intent.</p>';
    setNode('controller');
  }

  async function loadTools() {
    const response = await fetch('/live-rag/api/tools');
    const payload = await response.json();
    $('#toolCount').textContent = payload.count;
    $('#toolList').innerHTML = payload.tools.map(tool => `<article class="tool-card"><header><h3>${escapeHtml(tool.label)}</h3><code>${escapeHtml(tool.id)}</code></header><p>${escapeHtml(tool.purpose)}</p><details><summary>Inputs, outputs and policy</summary><pre>${escapeHtml(JSON.stringify({ trigger: tool.trigger, input: tool.input, output: tool.output, guardrails: tool.guardrails, side_effects: tool.side_effects }, null, 2))}</pre></details></article>`).join('');
  }

  function escapeHtml(value) { return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char])); }
  const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));

  $$('.scenario').forEach(button => button.addEventListener('click', () => runScenario(button.dataset.scenario)));
  $('#queryForm').addEventListener('submit', event => { event.preventDefault(); const text = $('#queryInput').value.trim(); if (text) { $('#transcriptText').textContent = text; send('transcript.chunk', { text, is_final: true, elapsed_ms: 0 }); } });
  $('#partialButton').addEventListener('click', () => { const text = $('#queryInput').value.trim(); if (text) { $('#transcriptText').textContent = text; send('transcript.chunk', { text, is_final: false, elapsed_ms: 0 }); } });
  $('#resetButton').addEventListener('click', () => { send('reset'); $('#transcriptText').textContent = 'Choose a guided run, or type a request below.'; $('#queryInput').value = ''; $('#eventLog').innerHTML = ''; });
  $('#clearTrace').addEventListener('click', () => { $('#eventLog').innerHTML = ''; });
  const openTools = () => $('#toolDialog').showModal();
  $('#toolsButton').addEventListener('click', openTools); $('#inspectToolsButton').addEventListener('click', openTools); $('#closeTools').addEventListener('click', () => $('#toolDialog').close());
  $('#toolDialog').addEventListener('click', event => { if (event.target === $('#toolDialog')) $('#toolDialog').close(); });

  loadTools().catch(() => { $('#toolList').innerHTML = '<p>Tool registry unavailable.</p>'; });
  connect();
})();
