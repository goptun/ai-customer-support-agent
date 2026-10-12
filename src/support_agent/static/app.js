'use strict';

// Every path is relative: the page is served under /projects/support/ by nginx.
const STORAGE_KEY = 'support-demo';

const STATUS_LABELS = {
  pending_payment: 'Aguardando pagamento',
  paid: 'Pago',
  processing: 'Em separação',
  shipped: 'Enviado',
  delivered: 'Entregue',
  cancelled: 'Cancelado',
};

const WELCOME =
  'Olá! Sou o assistente virtual da loja. Posso consultar seus pedidos e o rastreio, ' +
  'cancelar um pedido ainda não enviado, pedir um reembolso e tirar dúvidas sobre as ' +
  'políticas da loja. Como posso ajudar?';

const ordersEl = document.getElementById('orders');
const messagesEl = document.getElementById('messages');
const chipsEl = document.getElementById('chips');
const composerEl = document.getElementById('composer');
const messageEl = document.getElementById('message');
const sendBtn = document.getElementById('send');
const newConversationBtn = document.getElementById('new-conversation');
const statusEl = document.getElementById('status');
const turnstileEl = document.getElementById('turnstile');
const unavailableEl = document.getElementById('unavailable');
const replayAgainBtn = document.getElementById('replay-again');
const attendantEl = document.getElementById('attendant');
const approvalsEl = document.getElementById('approvals');
const ticketsEl = document.getElementById('tickets');

const GROUND_LABELS = {
  delivered_recently: 'entregue dentro do prazo de devolução',
  shipment_lost: 'extravio confirmado pela transportadora',
  late: 'entrega muito atrasada',
};

const TICKET_REASONS = {
  customer_requested: 'Cliente pediu atendimento humano',
  tool_failure: 'Falha repetida de um sistema',
  clarification_failed: 'O agente não entendeu o pedido',
};

const PRIORITY_LABELS = { low: 'baixa', normal: 'normal', high: 'alta' };

const RESTARTED = 'A demo foi reiniciada: você recebeu um novo cliente com outros pedidos.';
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const money = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
const day = new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'UTC' });
const moment = new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

let visitor = null; // { token, expiresAt, conversationId }
let busy = false;
let widgetId = null;
let pendingNotice = null; // shown once the next admission succeeds
let replaying = false; // the daily LLM cap is exhausted: no chat, a recorded Conversation
let replay = null; // the recorded Conversation, once fetched

// Per-viewer convenience only: the page works without storage.
function loadVisitor() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    if (stored && typeof stored.token === 'string') return stored;
  } catch (_) { /* storage unavailable */ }
  return null;
}

// Tokens aren't renewed: one about to expire is as good as expired.
function isExpiring(stored) {
  return !(stored.expiresAt > Date.now() + 60000);
}

async function fetchConfig() {
  try {
    return await (await fetch('demo/config')).json();
  } catch (_) {
    return {};
  }
}

function saveVisitor() {
  try {
    if (visitor) localStorage.setItem(STORAGE_KEY, JSON.stringify(visitor));
    else localStorage.removeItem(STORAGE_KEY);
  } catch (_) { /* storage unavailable */ }
}

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle('is-error', isError);
}

function setReady(ready) {
  const enabled = ready && !busy;
  messageEl.disabled = !enabled;
  sendBtn.disabled = !enabled;
  newConversationBtn.disabled = !enabled;
  chipsEl.querySelectorAll('button').forEach((chip) => { chip.disabled = !enabled; });
  approvalsEl.querySelectorAll('.approval').forEach((card) => {
    card.querySelector('.approve').disabled = !enabled;
    card.querySelector('.reject').disabled = !enabled || !card.querySelector('textarea').value.trim();
  });
}

function addMessage(kind, text) {
  const div = document.createElement('div');
  div.className = `msg msg-${kind}`;
  div.textContent = text;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function resetConversation() {
  messagesEl.replaceChildren();
  addMessage('agent', WELCOME);
}

function renderOrders(orders) {
  ordersEl.replaceChildren();
  orders.forEach((order) => {
    const li = document.createElement('li');
    li.className = 'order';

    const head = document.createElement('div');
    head.className = 'order-head';
    const id = document.createElement('span');
    id.className = 'order-id';
    id.textContent = order.order_id;
    const status = document.createElement('span');
    status.className = `order-status is-${order.status}`;
    status.textContent = STATUS_LABELS[order.status] || order.status;
    head.append(id, status);

    const items = document.createElement('div');
    items.className = 'order-items';
    items.textContent = order.items.map((item) => `${item.qty}× ${item.name}`).join(', ');

    const meta = document.createElement('div');
    meta.className = 'order-meta';
    const when = order.delivered_at
      ? `entregue em ${day.format(new Date(order.delivered_at))}`
      : order.status === 'cancelled' || !order.promised_delivery_date
        ? `pedido em ${day.format(new Date(order.placed_at))}`
        : `previsão ${day.format(new Date(order.promised_delivery_date))}`;
    meta.textContent = `${money.format(order.total_paid_cents / 100)} · ${when}`;

    li.append(head, items, meta);
    ordersEl.appendChild(li);
  });
}

function renderChips(orders) {
  const first = (status) => orders.find((o) => o.status === status);
  const delivered = orders
    .filter((o) => o.status === 'delivered')
    .sort((a, b) => (a.delivered_at < b.delivered_at ? 1 : -1));
  const suggestions = [];
  if (first('shipped')) suggestions.push(`Onde está o meu pedido ${first('shipped').order_id}?`);
  if (first('processing')) suggestions.push(`Quero cancelar o pedido ${first('processing').order_id}`);
  if (delivered[0]) suggestions.push(`Quero reembolso do pedido ${delivered[0].order_id}, veio com defeito`);
  suggestions.push('Qual o prazo para devolver um produto?');
  suggestions.push('Quero falar com um atendente');

  chipsEl.replaceChildren();
  suggestions.forEach((text) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.textContent = text;
    chip.addEventListener('click', () => send(text));
    chipsEl.appendChild(chip);
  });
}

function element(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function policyText(approval) {
  const policy = approval.policy_result || {};
  const ground = GROUND_LABELS[policy.ground] || policy.ground;
  const max = money.format((policy.max_cents || 0) / 100);
  const capped = policy.capped ? ' — o cliente pediu mais e o valor foi limitado' : '';
  return `Política de Reembolso: elegível (${ground}), até ${max}${capped}.`;
}

function renderApprovals(approvals) {
  approvalsEl.replaceChildren();
  if (!approvals.length) {
    approvalsEl.appendChild(element('li', 'placeholder', 'Nenhum reembolso aguardando decisão.'));
    return;
  }
  approvals.forEach((approval) => {
    const card = element('li', 'card approval');
    const head = element('div', 'card-head');
    head.append(
      element('span', 'order-id', approval.order_id),
      element('span', 'card-amount', money.format(approval.amount_cents / 100)),
    );
    const reason = element('p', 'card-text', `Motivo do cliente: “${approval.refund_reason}”`);
    const policy = element('p', 'card-meta', policyText(approval));

    const fieldId = `reason-${approval.approval_request_id}`;
    const label = element('label', 'card-label', 'Motivo (obrigatório para rejeitar)');
    label.htmlFor = fieldId;
    const field = element('textarea');
    field.id = fieldId;
    field.rows = 2;
    field.maxLength = 1000;
    const actions = element('div', 'card-actions');
    const approve = element('button', 'approve', 'Aprovar');
    approve.type = 'button';
    const reject = element('button', 'ghost reject', 'Rejeitar');
    reject.type = 'button';
    reject.disabled = true;
    const feedback = element('p', 'card-error');
    feedback.setAttribute('role', 'alert');
    actions.append(approve, reject);

    field.addEventListener('input', () => {
      reject.disabled = busy || !field.value.trim();
    });
    approve.addEventListener('click', () => decide(approval, 'approve', null, feedback));
    reject.addEventListener('click', () => decide(approval, 'reject', field.value, feedback));

    card.append(head, reason, policy, label, field, actions, feedback);
    approvalsEl.appendChild(card);
  });
}

function renderTickets(tickets) {
  ticketsEl.replaceChildren();
  if (!tickets.length) {
    ticketsEl.appendChild(element('li', 'placeholder', 'Nenhum ticket aberto.'));
    return;
  }
  tickets.forEach((ticket) => {
    const card = element('li', 'card');
    const head = element('div', 'card-head');
    head.append(
      element('span', 'order-id', `Ticket #${ticket.ticket_id}`),
      element('span', `order-status is-priority-${ticket.priority}`, `Prioridade ${PRIORITY_LABELS[ticket.priority] || ticket.priority}`),
    );
    const order = ticket.order_id ? ` · pedido ${ticket.order_id}` : '';
    card.append(
      head,
      element('p', 'card-text', TICKET_REASONS[ticket.reason] || ticket.reason),
      element('p', 'card-meta', `Aberto em ${moment.format(new Date(ticket.created_at))}${order}`),
    );
    ticketsEl.appendChild(card);
  });
}

function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (visitor) headers.Authorization = `Bearer ${visitor.token}`;
  if (options.body) headers['Content-Type'] = 'application/json';
  return fetch(path, { ...options, headers });
}

// Whether the visitor is still admitted: false once the token expired or a Demo Reset ran.
async function refreshOrders() {
  const response = await api('orders');
  if (response.status === 401) return false;
  if (!response.ok) return true; // a transient failure; keep the Orders shown
  const orders = await response.json();
  renderOrders(orders);
  renderChips(orders);
  return true;
}

// Whether the visitor is still admitted, as for refreshOrders.
async function refreshAttendant() {
  const [approvals, tickets] = await Promise.all([api('approvals'), api('tickets')]);
  if (approvals.status === 401 || tickets.status === 401) return false;
  if (approvals.ok) renderApprovals(await approvals.json());
  if (tickets.ok) renderTickets(await tickets.json());
  attendantEl.hidden = false;
  setReady(!!visitor && !replaying);
  return true;
}

function showAttendant() {
  refreshAttendant().then((admitted) => { if (!admitted) startOver(RESTARTED); }).catch(() => {});
}

async function decide(approval, decision, reason, feedback) {
  if (busy || !visitor) return;
  busy = true;
  setReady(false);
  feedback.textContent = '';
  try {
    const response = await api(`approvals/${approval.approval_request_id}`, {
      method: 'POST',
      body: JSON.stringify({ decision, reason }),
    });
    if (response.status === 401) {
      busy = false;
      await startOver(RESTARTED);
      return;
    }
    if (response.status === 422) {
      feedback.textContent = 'Informe o motivo para rejeitar.';
      return;
    }
    if (!response.ok && response.status !== 404 && response.status !== 409) {
      feedback.textContent = 'Não foi possível registrar a decisão. Tente de novo.';
      return;
    }
    // The decision is relayed to the Conversation the Refund was asked in.
    if (visitor.conversationId) {
      resetConversation();
      await restoreConversation();
    }
    setStatus(response.ok
      ? 'Decisão registrada pelo atendente e enviada ao cliente na conversa.'
      : 'Esse reembolso já tinha sido decidido.');
    await refreshAttendant();
  } catch (_) {
    feedback.textContent = 'Sem conexão com o servidor. Tente de novo.';
  } finally {
    busy = false;
    if (visitor && !replaying) setReady(true);
  }
}

async function restoreConversation() {
  if (!visitor.conversationId) return;
  const response = await api(`conversations/${visitor.conversationId}/messages`);
  if (!response.ok) {
    visitor.conversationId = null;
    saveVisitor();
    return;
  }
  const messages = await response.json();
  messages.forEach((m) => addMessage(m.role === 'customer' ? 'customer' : 'agent', m.content));
}

function whenTurnstileLoads() {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const poll = () => {
      if (window.turnstile) resolve(window.turnstile);
      else if (Date.now() - started > 15000) reject(new Error('turnstile'));
      else setTimeout(poll, 100);
    };
    poll();
  });
}

async function admit(notice) {
  pendingNotice = notice;
  setReady(false);
  setStatus('Preparando sua demo…');
  const config = await fetchConfig();
  if (config.llm_cap_exhausted) {
    await startReplay();
    return;
  }
  if (!config.turnstile_site_key) {
    setStatus('A demo está indisponível no momento. Tente de novo mais tarde.', true);
    return;
  }
  let turnstile;
  try {
    turnstile = await whenTurnstileLoads();
  } catch (_) {
    setStatus('Não foi possível carregar a verificação anti-robô. Recarregue a página.', true);
    return;
  }
  if (widgetId !== null) {
    turnstile.reset(widgetId); // a token admits once; a fresh check calls back again
    return;
  }
  widgetId = turnstile.render(turnstileEl, {
    sitekey: config.turnstile_site_key,
    appearance: 'interaction-only',
    language: 'pt-br',
    callback: onVerified,
    'error-callback': () => {
      setStatus('A verificação anti-robô falhou. Recarregue a página.', true);
    },
  });
}

async function onVerified(turnstileToken) {
  const response = await fetch('demo/visitors', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ turnstile_token: turnstileToken }),
  });
  if (!response.ok) {
    setStatus('Não foi possível iniciar a demo. Recarregue a página.', true);
    return;
  }
  const admitted = await response.json();
  if (admitted.llm_cap_exhausted) {
    await startReplay();
    return;
  }
  visitor = {
    token: admitted.access_token,
    expiresAt: Date.now() + admitted.expires_in * 1000,
    conversationId: null,
  };
  saveVisitor();
  renderOrders(admitted.orders);
  renderChips(admitted.orders);
  resetConversation();
  if (pendingNotice) addMessage('notice', pendingNotice);
  pendingNotice = null;
  setStatus('');
  setReady(true);
  showAttendant();
}

async function startOver(notice) {
  visitor = null;
  saveVisitor();
  attendantEl.hidden = true;
  await admit(notice);
}

async function send(text) {
  const message = text.trim();
  if (!message || busy || !visitor) return;
  busy = true;
  setReady(false);
  setStatus('');
  addMessage('customer', message);
  messageEl.value = '';
  const typing = addMessage('agent', 'Digitando…');
  typing.classList.add('msg-typing');

  try {
    const response = await api('chat', {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ message, conversation_id: visitor.conversationId }),
    });
    typing.remove();
    if (response.status === 401) {
      // Expired, or caught by a Demo Reset: tokens aren't renewed, a new visitor is admitted.
      busy = false;
      await startOver(RESTARTED);
      return;
    }
    if (response.status === 404) {
      visitor.conversationId = null;
      saveVisitor();
      addMessage('error', 'Essa conversa não existe mais. Envie de novo para começar outra.');
      return;
    }
    if (response.status === 429) {
      const body = await response.json().catch(() => ({}));
      if (body.code === 'llm_cap_exhausted') {
        busy = false;
        await startReplay();
        return;
      }
      const wait = response.headers.get('Retry-After');
      const suffix = wait ? ` Tente de novo em ${wait} s.` : '';
      addMessage('error', `${body.detail || 'Muitas mensagens em pouco tempo.'}${suffix}`);
      return;
    }
    if (!response.ok) {
      addMessage('error', 'Algo deu errado ao enviar sua mensagem. Tente de novo.');
      return;
    }
    const turn = await response.json();
    visitor.conversationId = turn.conversation_id;
    saveVisitor();
    turn.notices.forEach((notice) => addMessage('notice', notice));
    addMessage('agent', turn.reply);
    // A Cancellation changes an Order's status; a Refund or an escalation reaches the Attendant.
    Promise.all([refreshOrders(), refreshAttendant()])
      .then((admitted) => { if (admitted.includes(false)) startOver(RESTARTED); })
      .catch(() => {});
  } catch (_) {
    typing.remove();
    addMessage('error', 'Sem conexão com o servidor. Tente de novo.');
  } finally {
    busy = false;
    if (visitor && !replaying) setReady(true);
  }
}

// Replay mode: a recorded real Conversation, typed out in the same UI; no LLM calls.
const pause = (ms) => new Promise((resolve) => { setTimeout(resolve, reducedMotion ? 0 : ms); });

async function typeOut(kind, text, msPerChar) {
  const div = addMessage(kind, '');
  if (reducedMotion) {
    div.textContent = text;
    return;
  }
  for (let i = 1; i <= text.length; i += 1) {
    div.textContent = text.slice(0, i);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    await pause(msPerChar);
  }
}

async function playReplay(replay) {
  replayAgainBtn.disabled = true;
  messagesEl.replaceChildren();
  addMessage('agent', WELCOME);
  for (const turn of replay.turns) {
    await pause(900);
    await typeOut('customer', turn.message, 35);
    await pause(400);
    const typing = addMessage('agent', 'Digitando…');
    typing.classList.add('msg-typing');
    await pause(1400);
    typing.remove();
    turn.notices.forEach((notice) => addMessage('notice', notice));
    await typeOut('agent', turn.reply, 8);
  }
  replayAgainBtn.disabled = false;
}

async function startReplay() {
  replaying = true;
  visitor = null;
  saveVisitor();
  setReady(false);
  chipsEl.replaceChildren();
  composerEl.hidden = true;
  newConversationBtn.hidden = true;
  unavailableEl.hidden = false;
  attendantEl.hidden = true;
  setStatus('');
  try {
    replay = replay || await (await fetch('replay.json')).json();
  } catch (_) {
    setStatus('Não foi possível carregar o replay. Recarregue a página.', true);
    return;
  }
  renderOrders(replay.orders);
  replayAgainBtn.hidden = false;
  await playReplay(replay);
}

composerEl.addEventListener('submit', (event) => {
  event.preventDefault();
  send(messageEl.value);
});

messageEl.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    send(messageEl.value);
  }
});

replayAgainBtn.addEventListener('click', () => { if (replay) playReplay(replay); });

newConversationBtn.addEventListener('click', () => {
  if (!visitor) return;
  visitor.conversationId = null;
  saveVisitor();
  resetConversation();
  messageEl.focus();
});

async function start() {
  visitor = loadVisitor();
  if (visitor && isExpiring(visitor)) {
    await startOver(RESTARTED);
    return;
  }
  if (visitor && (await fetchConfig()).llm_cap_exhausted) {
    await startReplay();
    return;
  }
  if (visitor) {
    setStatus('Retomando sua demo…');
    let admitted = null; // null: the server couldn't be reached
    try {
      admitted = await refreshOrders();
    } catch (_) { /* start over, without claiming a restart */ }
    if (admitted) {
      resetConversation();
      await restoreConversation();
      setStatus('');
      setReady(true);
      showAttendant();
      return;
    }
    await startOver(admitted === false ? RESTARTED : null);
    return;
  }
  await admit(null);
}

start();
