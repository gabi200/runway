const money = value => new Intl.NumberFormat('en-IE', { style: 'currency', currency: 'EUR' }).format(value / 100);
let forecast;
let scenarioInflow = 0;
const namespace = 'http://www.w3.org/2000/svg';

function node(tag, attrs, text) {
  const element = document.createElementNS(namespace, tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (text !== undefined) element.textContent = text;
  return element;
}

function drawChart(container, compact = false) {
  const width = compact ? 300 : Math.min(980, Math.max(320, window.innerWidth - (window.innerWidth <= 640 ? 62 : window.innerWidth <= 1100 ? 270 : 350)));
  const height = compact ? 172 : 300;
  const margin = compact ? { left: 8, right: 8, top: 12, bottom: 25 } : { left: 68, right: 20, top: 20, bottom: 40 };
  const svg = node('svg', { viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Projected closing cash across 13 weeks. Baseline falls below zero in weeks 6 and 10.' });
  svg.append(node('title', {}, '13-week cash forecast from generated sample data'));
  const bottom = height - margin.bottom;
  const right = width - margin.right;
  const max = Math.max(100000, ...forecast.weeks.map(w => (w.closing_cents + scenarioInflow) / 100));
  const min = -25000;
  const x = i => margin.left + i / 12 * (right - margin.left);
  const y = v => bottom - (v - min) / (max - min) * (bottom - margin.top);
  for (const tick of compact ? [0] : [-20000, 0, 40000, 80000]) {
    svg.append(node('line', { x1: margin.left, y1: y(tick), x2: right, y2: y(tick), stroke: tick === 0 ? 'var(--muted)' : 'var(--chart-grid)', 'stroke-dasharray': tick === 0 ? '5 5' : '0', 'stroke-width': 1 }));
    if (!compact) svg.append(node('text', { x: margin.left - 12, y: y(tick) + 4, 'text-anchor': 'end', fill: 'var(--muted)', 'font-size': 11 }, `${tick < 0 ? '−' : ''}€${Math.abs(tick / 1000)}k`));
  }
  const points = forecast.weeks.map((w, i) => [x(i), y(w.closing_cents / 100)]);
  const line = points.map(([px, py], i) => `${i ? 'L' : 'M'}${px},${py}`).join(' ');
  svg.append(node('path', { d: `${line} L${right},${y(0)} L${margin.left},${y(0)} Z`, fill: 'var(--area)', opacity: .65 }));
  svg.append(node('path', { d: line, fill: 'none', stroke: 'var(--chart)', 'stroke-width': compact ? 2 : 2.5, 'stroke-linejoin': 'round' }));
  if (scenarioInflow && !compact) {
    const scenario = forecast.weeks.map((w, i) => `${i ? 'L' : 'M'}${x(i)},${y((w.closing_cents + (i >= 5 ? scenarioInflow : 0)) / 100)}`).join(' ');
    svg.append(node('path', { d: scenario, fill: 'none', stroke: 'var(--warning)', 'stroke-width': 2.5, 'stroke-dasharray': '6 4' }));
  }
  forecast.weeks.forEach((week, i) => {
    const short = week.closing_cents < 0;
    const dot = node('circle', { cx: x(i), cy: y(week.closing_cents / 100), r: short ? 4.5 : compact ? 0 : 3, fill: short ? 'var(--danger)' : 'var(--canvas)', stroke: short ? 'var(--danger)' : 'var(--chart)', 'stroke-width': 1.5 });
    dot.append(node('title', {}, `Week ${week.index}: ${money(week.closing_cents)}${short ? ', shortfall' : ''}`));
    svg.append(dot);
    const showLabel = compact ? [0, 5, 9, 12].includes(i) : width > 500 || [0, 2, 5, 7, 9, 12].includes(i);
    if (showLabel) svg.append(node('text', { x: x(i), y: bottom + 22, 'text-anchor': 'middle', fill: short ? 'var(--danger)' : 'var(--muted)', 'font-size': compact ? 11 : 12, 'font-weight': short ? 600 : 400 }, `W${week.index}`));
  });
  container.replaceChildren(svg);
}

function selectWeek(index) {
  const week = forecast.weeks[index - 1];
  document.querySelectorAll('#week-rows tr').forEach(row => row.classList.toggle('selected', Number(row.dataset.week) === index));
  document.querySelectorAll('#week-rows button').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.week) === index)));
  document.querySelector('#week-title').textContent = `Week ${index} explained`;
  document.querySelector('#week-dates').textContent = `${week.start} to ${week.end}`;
  const lines = document.querySelector('#week-lines');
  lines.replaceChildren();
  for (const item of week.lines) {
    const line = document.createElement('div');
    line.className = 'flow-line';
    const label = document.createElement('span');
    label.textContent = item.label;
    const refs = document.createElement('small');
    refs.textContent = item.source_refs.join(' · ');
    label.append(refs);
    const amount = document.createElement('strong');
    amount.textContent = money(item.amount_cents);
    line.append(label, amount);
    lines.append(line);
  }
  if (!week.lines.length) {
    const empty = document.createElement('p');
    empty.className = 'fine-print';
    empty.textContent = 'No receipts or payments forecast this week. Closing cash carries forward from the opening balance.';
    lines.append(empty);
  }
  const total = document.createElement('div');
  total.className = 'flow-line';
  const label = document.createElement('strong');
  label.textContent = 'Closing cash';
  const value = document.createElement('strong');
  value.textContent = money(week.closing_cents);
  if (week.closing_cents < 0) value.className = 'danger-text';
  total.append(label, value);
  lines.append(total);
}

function route() {
  const requested = location.hash.slice(1);
  const page = ['chat', 'forecast', 'approvals'].includes(requested) ? requested : 'chat';
  document.querySelectorAll('.screen').forEach(screen => { screen.hidden = screen.id !== page; });
  document.querySelectorAll('[data-page]').forEach(link => {
    if (link.dataset.page === page) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  document.querySelector('#breadcrumb').textContent = `Workspace / ${ { chat: 'Finance copilot', forecast: 'Cash forecast', approvals: 'Approvals' }[page] }`;
}

window.addEventListener('hashchange', route);
route();
document.querySelector('#theme-toggle').addEventListener('click', event => {
  const dark = document.body.classList.toggle('dark');
  event.currentTarget.textContent = dark ? 'View light option' : 'View dark option';
  event.currentTarget.setAttribute('aria-pressed', String(dark));
});
const dialog = document.querySelector('#session-dialog');
document.querySelector('#session-toggle').addEventListener('click', () => dialog.showModal());
document.querySelector('#mobile-session').addEventListener('click', () => dialog.showModal());
document.querySelector('#session-close').addEventListener('click', () => dialog.close());
document.querySelector('#brief-button').addEventListener('click', event => {
  event.currentTarget.textContent = 'Sample brief displayed';
  event.currentTarget.disabled = true;
  document.querySelector('.assistant-message').scrollIntoView({ block: 'start' });
});
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
  document.querySelector('#message').value = button.dataset.prompt;
  document.querySelector('#message').focus();
}));
document.querySelector('#chat-form').addEventListener('submit', event => {
  event.preventDefault();
  const input = document.querySelector('#message');
  if (!input.value.trim()) return;
  const user = document.createElement('div');
  user.className = 'user-message';
  user.textContent = input.value.trim();
  const reply = document.createElement('div');
  reply.className = 'mock-response';
  const title = document.createElement('strong');
  title.textContent = 'Preview response';
  const text = document.createElement('p');
  text.textContent = 'This mockup demonstrates the conversation layout. In the live app, Runway will answer using your ledger and checked tools. Explore the cash forecast or approval screen to inspect the proposed design.';
  reply.append(title, text);
  document.querySelector('#mock-replies').append(user, reply);
  input.value = '';
});
document.querySelectorAll('[data-decision]').forEach(button => button.addEventListener('click', () => {
  const card = document.getElementById(button.dataset.card);
  const approved = button.dataset.decision === 'approve';
  const notice = document.createElement('div');
  notice.className = 'decision-notice';
  notice.setAttribute('role', 'status');
  notice.textContent = approved ? button.dataset.card === 'finance-card' ? 'Preview: owner approval recorded. A separate accountant session is still required.' : 'Preview: owner approved the payment change. No payment has been changed.' : 'Preview: request rejected. No live request has been changed.';
  card.append(notice);
  card.querySelectorAll('[data-decision]').forEach(control => { control.disabled = true; });
  const state = card.querySelector('.state');
  state.textContent = approved ? button.dataset.card === 'finance-card' ? 'Waiting for accountant' : 'Approved in preview' : 'Rejected in preview';
}));

async function loadForecast() {
  try {
    const response = await fetch('sample-forecast.json');
    if (!response.ok) throw new Error('Forecast could not be loaded');
    forecast = await response.json();
    drawChart(document.querySelector('#mini-chart'), true);
    drawChart(document.querySelector('#full-chart'));
    const rows = document.querySelector('#week-rows');
    for (const week of forecast.weeks) {
      const row = document.createElement('tr');
      row.dataset.week = week.index;
      const cell = document.createElement('td');
      const button = document.createElement('button');
      button.textContent = `W${week.index}`;
      button.dataset.week = week.index;
      button.setAttribute('aria-label', `Inspect week ${week.index}`);
      button.addEventListener('click', () => selectWeek(week.index));
      cell.append(button);
      row.append(cell);
      const date = new Date(`${week.start}T12:00:00`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
      [date, money(week.receipts_cents), money(week.payments_cents), money(week.closing_cents)].forEach((value, i) => {
        const td = document.createElement('td');
        td.textContent = value;
        if (i === 3 && week.closing_cents < 0) td.className = 'danger-text';
        row.append(td);
      });
      rows.append(row);
    }
    selectWeek(6);
    document.querySelector('#scenario-apply').addEventListener('click', () => {
      const input = document.querySelector('#inflow');
      if (!input.checkValidity()) { input.reportValidity(); return; }
      scenarioInflow = Math.round(Number(input.value) * 100);
      drawChart(document.querySelector('#full-chart'));
      document.querySelector('#scenario-label').hidden = !scenarioInflow;
      document.querySelector('.scenario-legend').hidden = !scenarioInflow;
    });
    document.querySelector('#scenario-clear').addEventListener('click', () => {
      scenarioInflow = 0;
      drawChart(document.querySelector('#full-chart'));
      document.querySelector('#scenario-label').hidden = true;
      document.querySelector('.scenario-legend').hidden = true;
    });
  } catch {
    for (const id of ['mini-chart', 'full-chart']) {
      const error = document.createElement('p');
      error.className = 'fine-print';
      error.textContent = 'Sample forecast could not load. Open this preview through the local HTTP server, then reload.';
      document.getElementById(id).append(error);
    }
  }
}
loadForecast();
window.addEventListener('resize', () => {
  if (forecast) {
    drawChart(document.querySelector('#mini-chart'), true);
    drawChart(document.querySelector('#full-chart'));
  }
});
