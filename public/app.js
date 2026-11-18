const get = id => document.getElementById(id);
const actor = get('actor');
const dialog = get('dialog');
const labels = {
  create: 'New travel request', resubmit: 'Revise request', approve_trip: 'Approve trip',
  return_trip: 'Return for revision', reject_trip: 'Reject trip', book: 'Add booking', receipt: 'Add receipt',
  submit_claim: 'Submit expense claim', approve_claim: 'Approve expense claim',
  return_claim: 'Return claim', reconcile: 'Reconcile provider statement',
  voucher: 'Generate voucher', pay: 'Record payment', archive: 'Archive trip',
  cancel_booking: 'Cancel booking', amend_booking: 'Amend booking', refund_booking: 'Record refund',
  statement: 'Import / correct statement', correct_receipt: 'Correct receipt', verify_receipt: 'Verify receipt',
  policy: 'Publish policy version', policy_new: 'New policy template', policy_role: 'Configure policy role',
  delete_policy_templates: 'Delete policy templates', delete_policy_role: 'Delete policy role', unbind_policy_role: 'Remove employee policy binding',
  bind_policy_role: 'Bind employee policy role', workspace_config: 'Workspace settings', grant: 'Authorize agent', revoke: 'Revoke authorization'
};
let state = {trips: [], people: {}, catalog: {}};
let selection;
let view = 'overview';
let page = 0;
let tourIndex = 0;
let sessionBusy = false;
let refreshVersion = 0;
const pageSize = 5;
const format = new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'});
const usd = cents => format.format(cents / 100);
const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
function button(text, click, secondary = false) {
  const node = element('button', text, secondary ? 'secondary' : '');
  node.type = 'button';
  node.addEventListener('click', click);
  return node;
}
async function request(path, payload) {
  const options = {headers: {}};
  if (payload) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-Travel-Demo'] = '1';
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.error || 'The server could not complete this request.');
    error.status = response.status;
    throw error;
  }
  return response;
}
function notice(text, failure = false) {
  get('notice').textContent = text;
  get('notice').hidden = !text;
  get('notice').classList.toggle('failure', failure);
}
function updateState(next, resetFilters = false) {
  state = next;
  get('workspace-name').textContent = state.workspace_name;
  actor.value = state.actor;
  get('welcome').hidden = true;
  get('workspace-shell').hidden = false;
  get('session-controls').hidden = false;
  const select = get('filter');
  const previous = resetFilters ? '' : select.value;
  if (resetFilters) {
    page = 0;
    get('search').value = '';
  }
  select.replaceChildren(new Option('All statuses', ''));
  [...new Set(state.trips.map(t => t.status))].sort().forEach(status => select.add(new Option(status)));
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
  render();
}
function showWelcome() {
  get('welcome').hidden = false;
  get('workspace-shell').hidden = true;
  get('session-controls').hidden = true;
  get('workspace-name').textContent = 'Demo workspace';
}
async function refresh(initial = false) {
  const version = ++refreshVersion;
  if (initial) notice('Loading workspace…');
  try {
    const next = await (await request('/api/trips')).json();
    if (version !== refreshVersion) return;
    updateState(next);
    if (next.session_restored) notice('Your previous session ended. A fresh demo workspace is ready.');
    else if (initial) notice('');
  } catch (error) {
    if (version !== refreshVersion) return;
    if (initial || error.status === 403) showWelcome();
    notice(initial && error.status === 403 ? '' : error.message, true);
  }
}
function list(title, items) {
  const section = element('section');
  const ul = element('ul');
  for (const item of items.length ? items : ['None yet']) ul.append(element('li', item));
  section.append(element('h4', title), ul);
  return section;
}
function policyLimits(rules) {
  const caps = Object.entries(rules).filter(([key]) => !key.startsWith('_'))
    .map(([key, value]) => (key === 'request_cap' ? 'Request total' : key) + ' ' + usd(value));
  const controls = rules._controls;
  if (!controls) return caps.join(' · ');
  const basis = controls.hotel_limit_basis === 'night' ? 'per night' : 'per booking';
  const cities = controls.hotel_city_rules.map(rule => rule.group + ' (' + rule.cities.join(', ') + ') ' + usd(rule.limit));
  const restrictions = ['Hotel caps ' + basis, 'Flight ' + controls.flight_cabins.join(', '),
    'Rail ' + controls.rail_seats.join(', '), 'Payment ' + controls.payment_modes.join(', ')];
  return [...caps,...cities,...restrictions].join(' · ');
}
function renderTrip(t) {
  const article = element('article');
  const top = element('div', undefined, 'trip-top');
  const title = element('div');
  title.append(element('h3', t.destination), element('p', t.purpose, 'trip-meta'));
  const status = element('span', t.status, 'pill');
  status.dataset.tone = /Rejected|returned|Discrepancy/.test(t.status) ? 'danger' : /^Pending/.test(t.status) ? 'pending' : 'success';
  top.append(title, element('div', '#' + t.id + ' · ' + state.people[t.owner], 'table-cell'),
    element('div', t.start, 'table-cell'), element('div', usd(t.budget), 'table-cell'), status);
  article.append(top,
    element('p', `Trip ${t.id} · ${state.people[t.owner]} · ${t.cost_center} · ${t.start} → ${t.end} · Budget ${usd(t.budget)}`, 'trip-meta'));
  const details = element('details');
  details.append(element('summary', 'Bookings, expenses and activity'));
  const columns = element('div', undefined, 'details');
  const booking = t.provider_net;
  const items = t.bookings.map(x => `B-${String(x.id).padStart(5,'0')} · ${x.kind} · ${x.state} · ${usd(x.cents)} · refund ${usd(x.refund)} · fees ${usd(x.fee)} · ${x.service_class || ''} ${x.payment_mode || ''} ${x.hotel_group || ''}`);
  if (t.delegated) items.push('Agent access: receipts and claim submission only');
  if (t.provider_total !== null) items.push(`Statement ${usd(t.provider_total)} · variance ${usd(t.provider_total - booking)}`);
  if (t.voucher) items.push(`Voucher ${t.voucher} · provider ${usd(booking)} · employee ${usd(t.employee_total)}`);
  if (t.paid_at) items.push(`Payment recorded ${t.paid_at} UTC`);
  if (t.policy_snapshot) {
    const frozen = JSON.parse(t.policy_snapshot);
    const meta = frozen._policy;
    items.push(meta?.version_id ? `Approval policy: ${meta.template_name} · ${meta.role_name} · version ${meta.version_id} · trip start ${meta.date}` : 'Approval policy: preserved legacy snapshot');
    items.push('Snapshot limits · ' + policyLimits(frozen));
  }
  columns.append(list('Provider bookings', items),
    list('Employee receipts', t.receipts.map(x => `#${x.id} · ${x.kind} · ${usd(x.cents)} · ${x.issued || 'Date not recorded'} · ${x.recognition} / ${x.verification} · ${x.reference} ${x.error}`)),
    list('Activity', t.events.map(x => `${x.created} UTC · ${state.people[x.actor]} · ${x.detail}`)),
    list('Supplier statement', t.statement_lines.map(x => `${x.reference} · ${usd(x.cents)}`)),
    list('Reconciliation differences', t.differences));
  details.append(columns);
  article.append(details);
  const actions = element('div', undefined, 'actions');
  for (const action of t.actions) actions.append(button(labels[action], () => openAction(action, t), actions.childNodes.length > 0));
  if (t.actions.length) article.append(actions);
  return article;
}
function render() {
  const pages = {overview: ['Overview', 'Travel overview'], requests: ['Travel requests', 'Travel requests'], policy: ['Policies & reports', 'Policies & reports']};
  const [location, title] = pages[view];
  get('location-name').textContent = location;
  get('page-title').textContent = title;
  get('overview-panel').hidden = view !== 'overview';
  get('requests-panel').hidden = view === 'policy';
  get('policy-panel').hidden = view !== 'policy';
  get('page-title').closest('.hero').classList.toggle('stacked', view === 'policy' && state.roles[state.actor] === 'admin');
  for (const link of document.querySelectorAll('[data-view]')) {
    if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  get('greeting').textContent = state.people[actor.value] || '';
  const toolbar = get('toolbar');
  toolbar.replaceChildren();
  if (state.roles[state.actor] === 'employee') {
    if (view !== 'policy') toolbar.append(button('+ ' + labels.create, () => openAction('create')));
    else toolbar.append(button(labels.grant, () => openAction('grant')), button(labels.revoke, () => openAction('revoke'), true));
  }
  if (state.roles[state.actor] === 'admin' && view === 'policy') {
    for (const action of ['policy','policy_new','policy_role','bind_policy_role','delete_policy_templates','delete_policy_role','unbind_policy_role','workspace_config']) {
      toolbar.append(button(labels[action], () => openAction(action), action !== 'policy'));
    }
  }
  if (state.roles[state.actor] === 'finance' && view === 'policy') toolbar.append(button('Download CSV', downloadReport));
  if (['manager','finance'].includes(state.roles[state.actor]) && view === 'policy') toolbar.append(button('Summary CSV', () => downloadReport('/api/summary.csv','travel-summary.csv'), true));
  const metrics = get('metrics');
  metrics.replaceChildren();
  const a = state.analytics;
  for (const [label, value, icon] of [['Total travel requests',a.requests,'▤'],['Pending approvals',a.pending,'◷'],
    ['Approved / rejected',a.approved + ' / ' + a.rejected,'✓'],['Total travel expenses',usd(a.expenses),'$'],['Policy violations',a.violations,'◇']]) {
    const metric = element('div', undefined, 'metric');
    metric.append(element('div',icon,'metric-icon'), element('span',label), element('strong',value));
    metrics.append(metric);
  }
  const chart = element('section', undefined, 'analytics-card');
  chart.append(element('h3','Monthly spending'),element('p','Net recorded expenses by trip start month','muted'));
  const months = Object.entries(a.monthly).slice(-6);
  const max = Math.max(1, ...months.map(([, cents]) => cents));
  for (const [month, cents] of months) {
    const row = element('div',undefined,'chart-row');
    const bar = element('progress');
    bar.max = max;
    bar.value = cents;
    bar.setAttribute('aria-label',month + ' spending');
    row.append(element('span',month),bar,element('strong',usd(cents)));
    chart.append(row);
  }
  if (!months.length) chart.append(element('p','No spending recorded.','muted'));
  const claims = list('Reimbursement status', Object.entries(a.reimbursements).map(([status,n]) => status + ' · ' + n));
  const checks = list('Recent policy checks', a.checks.map(check => check.reason));
  claims.className = checks.className = 'analytics-card';
  get('analytics').replaceChildren(chart,claims,checks);
  renderRequests();
  const workspace = get('workspace');
  const config = state.policy_config;
  workspace.replaceChildren(list('Policy templates · version history (USD)', config.versions.map(v => `${config.templates.find(t => t.id === v.template_id)?.name || v.template_id + ' (deleted)'} · v${v.id} · ${v.effective_from} to ${v.effective_until || 'open'} · ${policyLimits(v.rules)}`)),
    list('Policy roles', config.roles.map(r => `${r.name} (${r.id}) → ${r.template_id}`)),
    list('Employee policy bindings', config.bindings.map(b => `${state.people[b.employee]} → ${b.role_id}`)),
    list('Authorizations · receipts and claim submission', state.grants.map(g => `${state.people[g.owner]} → ${state.people[g.agent]} · expires ${g.expires} · ${g.active ? 'Active' : 'Revoked'}`)));
  const reportNames = {employee:'Employee', department:'Department', cost_center:'Cost center'};
  for (const [group, rows] of Object.entries(state.reports)) workspace.append(list(`${reportNames[group]} report`, Object.entries(rows).map(([name,r]) => `${name}: ${r.trips} trips · provider ${usd(r.provider)} · employee ${usd(r.employee)}`)));
  if (state.admin_events.length) workspace.append(list('Administration activity', state.admin_events.map(e => `${e.created} UTC · ${state.people[e.actor]} · ${e.detail}`)));
}
function renderRequests() {
  const status = get('filter').value;
  const query = get('search').value.trim().toLowerCase();
  const trips = state.trips.filter(t => (!status || t.status === status) &&
    [t.destination, t.purpose, state.people[t.owner], '#' + t.id].some(value => value.toLowerCase().includes(query)));
  page = Math.min(page, Math.max(0, Math.ceil(trips.length / pageSize) - 1));
  const start = page * pageSize;
  const rows = get('trips');
  const head = element('div',undefined,'table-head');
  for (const name of ['Destination','Traveler','Departure','Budget','Status']) head.append(element('span',name));
  rows.replaceChildren(...(trips.length ? [head,...trips.slice(start,start + pageSize).map(renderTrip)] :
    [element('p','No trips match this view.','empty')]));
  const previous = button('Previous', () => { page--; renderRequests(); }, true);
  const next = button('Next', () => { page++; renderRequests(); }, true);
  previous.disabled = page === 0;
  next.disabled = start + pageSize >= trips.length;
  get('pagination').replaceChildren(element('span',trips.length ? (start + 1) + '–' + Math.min(start + pageSize,trips.length) + ' of ' + trips.length : '0 requests'),previous,next);
}
function field(name, label, {value = '', type = 'text', choices, required = true} = {}) {
  const wrapper = element('label', label);
  const input = element(choices ? 'select' : type === 'textarea' ? 'textarea' : 'input');
  input.name = name;
  if (choices) for (const [key, description] of choices) input.add(new Option(description, key));
  else if (type !== 'textarea') input.type = type;
  if (type === 'number') { input.step = '0.01'; input.min = '0'; }
  if (!choices && ['destination','purpose','reason','reference','role_name','template_name'].includes(name)) {
    input.maxLength = ['purpose','reason'].includes(name) ? 250 : 120;
  }
  if (value !== '' || !choices) input.value = value;
  input.required = required;
  wrapper.append(input);
  return wrapper;
}
function openAction(action, t) {
  if (sessionBusy) return;
  //新增需求：同一表单网络重试沿用请求标识，防止重复入账。
  selection = {action, id:t?.id, actor:state.actor, revision:state.revision, request_id:crypto.randomUUID()};
  get('dialog-title').textContent = labels[action];
  get('error').textContent = '';
  const fields = get('fields');
  fields.replaceChildren();
  if (action === 'workspace_config') fields.append(field('name','Workspace display name',{value:state.workspace_name}));
  if (action === 'create' || action === 'resubmit') fields.append(
    field('destination','Destination',{value:t?.destination}), field('purpose','Purpose',{value:t?.purpose}),
    field('start','Start date',{type:'date',value:t?.start}), field('end','End date',{type:'date',value:t?.end}),
    field('budget','Budget (USD)',{type:'number',value:t ? t.budget/100 : ''}),
    field('cost_center','Cost center',{value:t?.cost_center,choices:['Engineering','Operations','Sales'].map(x => [x,x])}));
  if (action === 'book') {
    const booked = new Set(t.bookings.map(order => order.kind));
    const choices = Object.entries(state.catalog).filter(([key]) => !booked.has(key));
    const kindField = field('kind','Inventory',{choices:choices.map(([key,cents]) => [key,`${key} · ${usd(cents)}`])});
    const terms = element('div');
    const renderTerms = kind => {
      const controls = JSON.parse(t.policy_snapshot || '{}')._controls;
      terms.replaceChildren();
      if (!controls) return;
      terms.append(field('payment_mode','Payment mode',{choices:controls.payment_modes.map(x => [x,x])}));
      if (kind === 'Flight' || kind === 'Rail') terms.append(field('service_class','Cabin / seat',{choices:(kind === 'Flight' ? controls.flight_cabins : controls.rail_seats).map(x => [x,x])}));
    };
    fields.append(kindField,terms);
    kindField.querySelector('select').addEventListener('change',e => renderTerms(e.target.value));
    renderTerms(kindField.querySelector('select').value);
  }
  if (action === 'receipt') fields.append(
    element('p','Use DEMO-RAIL, DEMO-HOTEL, DEMO-TAXI, or enter receipt fields. FAIL- references exercise verification failure.','muted'),
    field('reference','Receipt reference'),field('kind','Manual expense category',{required:false}),
    field('amount','Manual amount (USD)',{type:'number',required:false}),
    field('issued','Receipt date',{type:'date',value:t.start,required:false}));
  if (action === 'reconcile') fields.append(
    element('p',`Match every imported statement line against orders. Expected net provider total: ${usd(t.provider_net)}.`,'muted'));
  if (action === 'statement') fields.append(
    element('p','One supplier line per row: B-00001,245.00. Missing orders, unknown references and amount differences are reported individually.','muted'),
    field('lines','Statement lines',{type:'textarea',value:t.statement_lines.map(x => `${x.reference},${(x.cents/100).toFixed(2)}`).join('\n')}),
    field('reason','Import / correction reason'));
  if (['cancel_booking','amend_booking','refund_booking'].includes(action)) {
    const expectedState = action === 'refund_booking' ? 'Refund pending' : 'Booked';
    fields.append(field('booking_id','Order',{choices:t.bookings.filter(b => b.state === expectedState).map(b => [b.id,`B-${String(b.id).padStart(5,'0')} · ${b.kind} · ${usd(b.cents)}`])}));
    if (action !== 'cancel_booking') fields.append(field('amount',action === 'refund_booking' ? 'Refund received (USD)' : 'New fare (USD)',{type:'number'}));
    if (action !== 'refund_booking') fields.append(field('fee','Fee (USD)',{type:'number',value:0}));
    if (action === 'refund_booking') fields.append(field('final','Refund completed?',{choices:[['yes','Yes · close refund'],['no','No · more refund expected']]}));
    fields.append(field('reason','Reason'));
  }
  if (['correct_receipt','verify_receipt'].includes(action)) {
    fields.append(field('receipt_id','Receipt',{choices:t.receipts.map(r => [r.id,`${r.reference} · ${r.verification}`])}));
    if (action === 'correct_receipt') fields.append(field('reference','Corrected reference'),field('kind','Category'),field('amount','Amount (USD)',{type:'number'}),field('issued','Receipt date (YYYY-MM-DD)',{value:t.start}));
  }
  if (action === 'policy' || action === 'policy_new') {
    //新增需求：审批时保存政策快照；这里的调整仅影响后续审批。
    selection.action = 'policy';
    fields.append(element('p','Trip start date selects the effective version at approval. Existing approved trips retain their snapshot. Amounts are fictional.','muted'));
    const config = state.policy_config;
    const templateField = field('template_id','Template',{choices:config.templates.map(t => [t.id,t.name])});
    const editor = element('div');
    const renderPolicyEditor = id => {
      const versions = config.versions.filter(v => v.template_id === id);
      const v = versions[versions.length - 1];
      editor.replaceChildren(field('template_name','Template name',{value:config.templates.find(t => t.id === id)?.name || ''}),
        field('effective_from','Effective from (trip start)',{type:'date',value:v?.effective_from || '2027-01-01'}),
        field('effective_until','Effective through (optional)',{type:'date',value:v?.effective_until || '',required:false}));
      for (const [key,value] of Object.entries(v?.rules || state.policy).filter(([k]) => !k.startsWith('_'))) editor.append(field(key,`${key} limit (USD)`,{type:'number',value:value/100}));
      const controls = v?.rules._controls;
      editor.append(field('flight_cabins','Allowed flight cabins (comma separated)',{value:(controls?.flight_cabins || ['Economy']).join(',')}),
        field('rail_seats','Allowed rail seats (comma separated)',{value:(controls?.rail_seats || ['Second class']).join(',')}),
        field('payment_modes','Allowed payment modes: Company,Personal',{value:(controls?.payment_modes || ['Company','Personal']).join(',')}),
        field('hotel_city_rules','Hotel city groups (JSON, amounts in USD)',{type:'textarea',value:JSON.stringify((controls?.hotel_city_rules || []).map(r => ({group:r.group,cities:r.cities,amount:(r.limit/100).toFixed(2)})),null,2)}),
        field('hotel_limit_basis','Hotel limit basis',{value:controls?.hotel_limit_basis || 'booking',choices:[['booking','Per booking'],['night','Per night (fictional rule)']]}),
        element('p','The fictional nightly rule uses the difference between trip dates, with a minimum of one night. The catalog shows a simulated total quote.','muted'));
    };
    if (action === 'policy_new') fields.append(field('template_id','New template identifier'));
    else {
      fields.append(templateField);
      templateField.querySelector('select').addEventListener('change', event => renderPolicyEditor(event.target.value));
    }
    fields.append(editor);
    renderPolicyEditor(action === 'policy_new' ? '' : config.templates[0]?.id);
  }
  if (action === 'policy_role') fields.append(field('role_id','Policy role identifier'),field('role_name','Policy role name'),
    field('template_id','Policy template',{choices:state.policy_config.templates.map(t => [t.id,t.name])}));
  if (action === 'bind_policy_role') {
    //新增需求：政策角色与登录权限分离；绑定只能由经理维护。
    fields.append(field('employee','Employee',{choices:Object.entries(state.people).filter(([id]) => ['alex','casey'].includes(id))}),
      field('role_id','Policy role',{choices:state.policy_config.roles.map(r => [r.id,`${r.name} → ${r.template_id}`])}));
  }
  if (action === 'delete_policy_templates') fields.append(field('template_ids','Template identifiers (comma separated)'),element('p','Remove or reassign policy roles first. Historical versions and approved snapshots are retained.','muted'));
  if (action === 'delete_policy_role') fields.append(field('role_id','Policy role',{choices:state.policy_config.roles.map(r => [r.id,r.name])}),element('p','This removes employee bindings for the selected policy role. Approved snapshots are retained.','muted'));
  if (action === 'unbind_policy_role') fields.append(field('employee','Employee',{choices:Object.entries(state.people).filter(([id]) => ['alex','casey'].includes(id))}));
  if (action === 'grant' || action === 'revoke') {
    //新增需求：授权仅允许票据处理和提交报销，不包含预订、审批或付款。
    fields.append(field('agent','Agent',{choices:Object.entries(state.people).filter(([id]) => ['alex','casey'].includes(id) && id !== actor.value)}));
    if (action === 'grant') fields.append(field('expires','Valid through',{type:'date'}));
  }
  //新增需求：付款失败保留待付款状态，可重试；成功后不再提供付款动作。
  if (action === 'pay') fields.append(field('outcome','Payment result',{choices:[['success','Success'],['failure','Failure · retry later']]}));
  if (action.startsWith('return_') || action === 'reject_trip') fields.append(field('reason','Reason'));
  if (!fields.childNodes.length) fields.append(element('p',`Apply this action to trip ${t?.id}?`,'muted'));
  dialog.showModal();
}
async function downloadReport(path = '/api/report.csv', filename = 'travel-finance-report.csv') {
  try {
    if (typeof path !== 'string') path = '/api/report.csv';
    const url = URL.createObjectURL(await (await request(path)).blob());
    const link = element('a');
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { notice(error.message, true); }
}
get('action-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = get('submit');
  sessionBusy = true;
  submit.disabled = true;
  submit.textContent = 'Saving…';
  get('cancel').disabled = actor.disabled = get('reset').disabled = true;
  try {
    const body = Object.fromEntries(new FormData(event.currentTarget));
    await request('/api/trips', {...body, ...selection});
    dialog.close();
    get('filter').value = '';
    notice('Changes saved.');
    await refresh();
  } catch (error) {
    if (error.status === 422 || error.status === 403) await refresh();
    get('error').textContent = error.message;
  } finally {
    sessionBusy = false;
    submit.textContent = 'Confirm';
    submit.disabled = get('cancel').disabled = actor.disabled = get('reset').disabled = false;
  }
});
get('cancel').addEventListener('click', () => dialog.close());
dialog.addEventListener('cancel', event => { if (sessionBusy) event.preventDefault(); });

async function updateSession(path, payload, message) {
  if (sessionBusy) return;
  sessionBusy = true;
  refreshVersion++;
  actor.disabled = get('start').disabled = get('reset').disabled = true;
  notice('Loading workspace…');
  try {
    const next = await (await request(path, payload)).json();
    if (path !== '/api/role') view = 'overview';
    else if (next.roles[next.actor] === 'admin') view = 'policy';
    updateState(next, true);
    notice(message);
  } catch (error) {
    if (state.actor) actor.value = state.actor;
    if (error.status === 403) showWelcome();
    notice(error.message, true);
  } finally {
    sessionBusy = false;
    actor.disabled = get('start').disabled = get('reset').disabled = false;
  }
}
get('start').addEventListener('click', () => updateSession('/api/session', {}, 'Your demo is ready.'));
actor.addEventListener('change', () => updateSession('/api/role', {actor:actor.value}, 'Demo role updated.'));
const resetDialog = get('reset-dialog');
get('reset').addEventListener('click', () => { resetDialog.returnValue = ''; resetDialog.showModal(); });
resetDialog.addEventListener('close', () => {
  if (resetDialog.returnValue === 'reset') updateSession('/api/reset', {}, 'Demo reset to the original sample data.');
});
for (const control of [get('filter'),get('search')]) {
  control.addEventListener('input', () => { page = 0; renderRequests(); });
}
for (const link of document.querySelectorAll('[data-view]')) {
  link.addEventListener('click', () => {
    if (get('workspace-shell').hidden) {
      get('start').focus();
      return;
    }
    view = link.dataset.view;
    page = 0;
    render();
    get('page-title').scrollIntoView({block:'nearest'});
  });
}

const tour = get('tour');
const tourSteps = [
  ['Employee · Submit a request', 'Create a trip with a budget of $750 and matching dates. Try $2,500 first to see the fictional policy cap reject the request. For the fastest walkthrough, use the pending Seattle sample.'],
  ['Manager · Review the request', 'Choose Manager in View as. Approve the request to freeze its policy version. Return it to request a revision, or reject it and save a reason.'],
  ['Employee · Book and claim expenses', 'Switch back to Alex. Add a company Flight for $245 and a receipt with reference DEMO-RAIL. Submit the expense claim.'],
  ['Manager · Approve the expense claim', 'Switch to Manager and approve the claim to send it to Finance.'],
  ['Finance · Reconcile and settle', 'Switch to Finance. Open the trip details and copy its flight order ID. Import the line as ORDER-ID,245.00 with a reason. Reconcile, generate a voucher, and record a simulated successful payment. The overview updates.'],
  ['Finance and Admin · Explore the other scenarios', 'As Finance, record a $68 final refund for Denver. Its $8 fee remains. As Admin, open Policies & reports to inspect templates and employee bindings. Reset demo restores the sample records.'],
];
function renderTour() {
  get('tour-progress').textContent = 'Step ' + (tourIndex + 1) + ' of ' + tourSteps.length;
  [get('tour-step').textContent,get('tour-copy').textContent] = tourSteps[tourIndex];
  get('tour-back').disabled = tourIndex === 0;
  get('tour-next').textContent = tourIndex === tourSteps.length - 1 ? 'Done' : 'Next';
}
get('tour-open').addEventListener('click', () => { renderTour(); tour.showModal(); });
get('tour-close').addEventListener('click', () => tour.close());
get('tour-back').addEventListener('click', () => { tourIndex--; renderTour(); });
get('tour-next').addEventListener('click', () => {
  if (tourIndex === tourSteps.length - 1) tour.close();
  else { tourIndex++; renderTour(); }
});
refresh(true);
