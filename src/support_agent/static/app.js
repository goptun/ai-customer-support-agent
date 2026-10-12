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

const money = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
const day = new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'UTC' });

let session = null; // { token, expiresAt, conversationId }
let busy = false;
let widgetId = null;
let pendingNotice = null; // shown once the next admission succeeds

// Per-viewer convenience only: the page works without storage.
function loadSession() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    if (stored && typeof stored.token === 'string' && stored.expiresAt > Date.now() + 60000) {
      return stored;
    }
  } catch (_) { /* storage unavailable */ }
  return null;
}

function saveSession() {
  try {
    if (session) localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
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

function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (session) headers.Authorization = `Bearer ${session.token}`;
  if (options.body) headers['Content-Type'] = 'application/json';
  return fetch(path, { ...options, headers });
}

async function refreshOrders() {
  const response = await api('orders');
  if (!response.ok) return response.status;
  const orders = await response.json();
  renderOrders(orders);
  renderChips(orders);
  return 200;
}

async function restoreConversation() {
  if (!session.conversationId) return;
  const response = await api(`conversations/${session.conversationId}/messages`);
  if (!response.ok) {
    session.conversationId = null;
    saveSession();
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
  let config;
  try {
    config = await (await fetch('demo/config')).json();
  } catch (_) {
    config = {};
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
  const visitor = await response.json();
  session = {
    token: visitor.access_token,
    expiresAt: Date.now() + visitor.expires_in * 1000,
    conversationId: null,
  };
  saveSession();
  renderOrders(visitor.orders);
  renderChips(visitor.orders);
  resetConversation();
  if (pendingNotice) addMessage('notice', pendingNotice);
  pendingNotice = null;
  setStatus('');
  setReady(true);
}

async function startOver(notice) {
  session = null;
  saveSession();
  await admit(notice);
}

async function send(text) {
  const message = text.trim();
  if (!message || busy || !session) return;
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
      body: JSON.stringify({ message, conversation_id: session.conversationId }),
    });
    typing.remove();
    if (response.status === 401) {
      busy = false;
      await startOver('Seu acesso à demo expirou; começamos uma nova com outros pedidos.');
      return;
    }
    if (response.status === 404) {
      session.conversationId = null;
      saveSession();
      addMessage('error', 'Essa conversa não existe mais. Envie de novo para começar outra.');
      return;
    }
    if (response.status === 429) {
      const body = await response.json().catch(() => ({}));
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
    session.conversationId = turn.conversation_id;
    saveSession();
    turn.notices.forEach((notice) => addMessage('notice', notice));
    addMessage('agent', turn.reply);
    refreshOrders(); // a Cancellation changes an Order's status
  } catch (_) {
    typing.remove();
    addMessage('error', 'Sem conexão com o servidor. Tente de novo.');
  } finally {
    busy = false;
    if (session) setReady(true);
  }
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

newConversationBtn.addEventListener('click', () => {
  if (!session) return;
  session.conversationId = null;
  saveSession();
  resetConversation();
  messageEl.focus();
});

async function start() {
  session = loadSession();
  if (session) {
    setStatus('Retomando sua demo…');
    const status = await refreshOrders().catch(() => 0);
    if (status === 200) {
      resetConversation();
      await restoreConversation();
      setStatus('');
      setReady(true);
      return;
    }
    session = null;
    saveSession();
  }
  await admit(null);
}

start();
