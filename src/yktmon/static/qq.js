/* QQ live status is deliberately separate from editable form/secret state. */
(() => {
  const el = id => document.getElementById(id);
  let state = null, editing = false, bindingId = '', targetCourse = null;
  let confirmAction = null, historyPrint = '', groupsPrint = '', candidatePrint = '';
  let credentialIdentity = null, secretEpoch = 0, secretOrigin = '', statusEpoch = 0;
  let aliasEdited = false;

  function confirm(title, description, action) {
    el('qqConfirmTitle').textContent = title;
    el('qqConfirmDescription').textContent = description;
    confirmAction = action;
    el('qqConfirmDialog').showModal();
  }
  const dt = value => value ? new Date(value * 1000).toLocaleString() : '尚无';
  async function request(path, method = 'GET', data) {
    const r = await fetch('/api/' + path, {
      method, headers: {'Content-Type': 'application/json', 'X-Yktmon': 'local'},
      ...(data !== undefined ? {body: JSON.stringify(data)} : {})
    });
    const d = await r.json();
    if (!r.ok) throw Error(typeof d.detail === 'string' ? d.detail : '操作失败');
    return d;
  }
  async function work(button, fn) {
    statusEpoch++;
    button.disabled = true;
    try { await fn(); }
    catch (e) { el('qqError').textContent = e.message; notice(e.message); }
    finally { button.disabled = false; }
  }
  function setSecretVisibility(show) {
    el('qqSecret').type = show ? 'text' : 'password';
    el('qqToggleSecret').setAttribute('aria-label', show ? '隐藏 AppSecret' : '显示 AppSecret');
    el('qqToggleSecret').setAttribute('aria-pressed', String(show));
    el('qqToggleSecret').classList.toggle('revealed', show);
  }
  async function restoreSecret(appId, epoch) {
    try {
      const d = await request('qq/secret', 'POST');
      // Do not replace edits or attach a previous app's credential after an async response.
      if (epoch !== secretEpoch || editing || el('qqAppId').value.trim() !== appId || d.app_id !== appId) return;
      el('qqSecret').value = d.secret || '';
      el('qqSecret').placeholder = '请输入 AppSecret';
      secretOrigin = 'saved';
      el('qqSecretState').textContent = '已保存，可用眼睛显示或隐藏';
    } catch (e) {
      if (epoch === secretEpoch && !editing) el('qqSecretState').textContent = '读取已保存 AppSecret 失败：' + e.message;
    }
  }
  function syncCredentials(s, force = false) {
    const identity = JSON.stringify([s.app_id, s.secret_configured]);
    if (!force && (editing || credentialIdentity === identity)) return;
    credentialIdentity = identity;
    const epoch = ++secretEpoch;
    el('qqAppId').value = s.app_id || '';
    el('qqEnabled').checked = !!s.enabled;
    el('qqSecret').value = '';
    el('qqSecret').placeholder = s.secret_configured ? '正在读取已保存 AppSecret…' : '请输入 AppSecret';
    setSecretVisibility(false);
    secretOrigin = '';
    el('qqSecretState').textContent = s.secret_configured ? 'AppSecret 已保存' : '尚未保存 AppSecret';
    if (s.secret_configured) restoreSecret(s.app_id, epoch);
  }
  function renderGroups(s) {
    const fingerprint = JSON.stringify(s.groups || []);
    el('qqGroupCount').textContent = (s.groups || []).length + ' 个群';
    if (fingerprint === groupsPrint) return;
    groupsPrint = fingerprint;
    const prior = el('qqTestGroup').value;
    el('qqGroups').replaceChildren(); el('qqTestGroup').replaceChildren();
    for (const g of s.groups || []) {
      const row = node('div', undefined, 'qq-group-row');
      row.append(node('strong', g.name), node('span', g.permission, 'hint'));
      const toggle = node('button', g.enabled ? '停用' : '启用', 'secondary'); toggle.type = 'button';
      toggle.onclick = () => work(toggle, async () => render(await request('qq/groups/' + g.id, 'PUT', {enabled: !g.enabled})));
      const rename = node('button', '改名', 'text-button'); rename.type = 'button';
      rename.onclick = () => {
        const name = prompt('通知群名称', g.name);
        if (name?.trim()) work(rename, async () => render(await request('qq/groups/' + g.id, 'PUT', {name: name.trim()})));
      };
      row.append(toggle, rename); el('qqGroups').append(row);
      if (g.enabled) { const o = node('option', g.name); o.value = g.id; el('qqTestGroup').append(o); }
    }
    if (!(s.groups || []).length) el('qqGroups').append(node('p', '尚未绑定通知群。添加并确认后会永久保存。', 'hint'));
    if (!el('qqTestGroup').options.length) {const o = node('option', '请先添加并启用通知群'); o.value = ''; el('qqTestGroup').append(o);}
    if ([...el('qqTestGroup').options].some(o => o.value === prior)) el('qqTestGroup').value = prior;
  }
  function renderBinding(s) {
    const b = s.binding;
    el('qqBinding').hidden = !b;
    if (!b) {bindingId = ''; candidatePrint = ''; return;}
    if (bindingId !== b.id) {
      bindingId = b.id; el('qqGroupName').value = ''; aliasEdited = false;
      candidatePrint = ''; el('qqBinding').open = true;
    }
    el('qqBindCommand').textContent = '@你的机器人 绑定 ' + b.code;
    el('qqBindExpiry').textContent = '剩余 ' + b.seconds + ' 秒；过期不影响已保存的群。';
    const fingerprint = JSON.stringify(b.candidates || []);
    if (fingerprint !== candidatePrint) {
      candidatePrint = fingerprint;
      const prior = el('qqCandidates').value; el('qqCandidates').replaceChildren();
      for (const c of b.candidates || []) {const o = node('option', c.label); o.value = c.id; el('qqCandidates').append(o);}
      if (!el('qqCandidates').options.length) {const o = node('option', '等待目标群发送绑定指令…'); o.value = ''; el('qqCandidates').append(o);}
      if ([...el('qqCandidates').options].some(o => o.value === prior)) el('qqCandidates').value = prior;
      suggestAlias();
    }
    el('qqConfirm').disabled = !(b.candidates || []).length;
  }
  function suggestAlias() {
    if (aliasEdited) return;
    const candidate = state?.binding?.candidates.find(c => c.id === el('qqCandidates').value);
    el('qqGroupName').value = candidate && !/^候选群 \d+$/.test(candidate.label) ? candidate.label : '';
  }
  function renderHistory(s) {
    const entries = (s.history || []).slice(0, 10);
    el('qqHistoryCount').textContent = '最近 ' + entries.length + ' 条 / 最多 10 条';
    const fingerprint = JSON.stringify(entries);
    if (fingerprint === historyPrint) return;
    historyPrint = fingerprint;
    const scroll = el('qqHistory').scrollTop;
    captureDisclosures(el('qqHistory')); el('qqHistory').replaceChildren();
    for (const r of entries) {
      const row = node('div', undefined, 'qq-history-row');
      const description = node('div');
      description.append(node('strong', r.group_name + ' · ' + ({lesson_started:'上课通知', lesson_finished:'下课通知', reminder:'新题提醒', image:'原题图片', result:'Markdown 解答', 'test-text':'文字测试', 'test-image':'图片测试', 'test-markdown':'Markdown 测试'}[r.phase] || r.phase)),
        node('p', ({pending:'排队中', sending:'发送中', sent:'QQ 已返回消息 ID', failed:'发送失败', unknown:'结果未知，需核对', cancelled:'已取消'}[r.state] || r.state) + ' · 尝试 ' + r.attempts + ' 次 · ' + dt(r.created), 'hint'));
      if (r.error) description.append(node('p', r.error, 'error'));
      if (r.message_id) {
        const detail = node('details', undefined, 'receipt-details fold-panel');
        detail.append(node('summary', '查看消息回执'), node('code', r.message_id, 'message-id'));
        rememberDisclosure(detail, 'qq-receipt:' + r.id); description.append(detail);
      }
      row.append(description);
      if (['failed', 'unknown', 'pending'].includes(r.state)) {
        const b = node('button', r.state === 'pending' ? '取消待发' : '重试', 'secondary'); b.type = 'button';
        b.onclick = () => {
          const action = r.state === 'pending' ? 'cancel' : 'retry';
          confirm(action === 'cancel' ? '取消这条待发消息' : '重试这条消息', r.state === 'unknown' ? '发送结果未知，请先检查群消息。确认后可能产生重复消息。' : '目标仍为“' + r.group_name + '”，不会改到其他群。',
            async () => render(await request(`qq/outbox/${r.id}/${action}`, 'POST', {confirm_unknown: r.state === 'unknown'})));
        };
        row.append(b);
      }
      el('qqHistory').append(row);
    }
    if (!entries.length) el('qqHistory').append(node('p', '暂无投递记录。查看历史课堂不会补发通知。', 'hint'));
    el('qqHistory').scrollTop = scroll;
  }
  function render(s) {
    state = s;
    el('qqState').textContent = s.state + (s.name ? ' · ' + s.name : '');
    el('qqChannelState').textContent = s.state;
    el('qqError').textContent = s.error || '';
    syncCredentials(s);
    renderGroups(s); renderBinding(s); renderHistory(s);
    el('qqNewBinding').disabled = s.state !== '已连接';
    el('qqTestSend').disabled = s.state !== '已连接' || !el('qqTestGroup').value;
    const d = s.diagnostics || {};
    el('qqDiagnostics').textContent = `Token 最近刷新：${dt(d.token_refreshed_at)}；心跳确认：${dt(d.heartbeat_at)}；最近重连：${dt(d.reconnect_at)}`;
  }
  async function poll() {
    const epoch = statusEpoch;
    try {if (!document.hidden) {const s = await request('qq/status'); if (epoch === statusEpoch) render(s);}}
    catch {el('qqState').textContent = '无法读取 QQ 连接状态';}
    setTimeout(poll, 2000);
  }
  // QQ has its own save action; edits here must not dirty the unrelated model form.
  el('qqSettings').addEventListener('input', e => e.stopPropagation());
  el('qqToggleSecret').onclick = () => setSecretVisibility(el('qqSecret').type === 'password');
  for (const id of ['qqAppId', 'qqSecret', 'qqEnabled']) el(id).addEventListener('input', e => {
    e.stopPropagation(); secretEpoch++; editing = true;
    if (id === 'qqSecret') secretOrigin = 'typed';
    if (id === 'qqAppId' && secretOrigin === 'saved') {el('qqSecret').value = ''; secretOrigin = ''; setSecretVisibility(false);}
    el('qqSecretState').textContent = 'QQ 配置有未保存的更改'; el('qqSecretState').classList.add('unsaved');
  });
  el('qqSave').onclick = () => work(el('qqSave'), async () => {
    const epoch = secretEpoch;
    const s = await request('qq/config', 'POST', {app_id: el('qqAppId').value.trim(), secret: el('qqSecret').value, enabled: el('qqEnabled').checked});
    if (epoch === secretEpoch) {
      editing = false; el('qqSecretState').classList.remove('unsaved');
      syncCredentials(s, true);
    }
    render(s); notice('QQ 配置已保存；群绑定和课程选群会保留。');
  });
  el('qqConnect').onclick = () => work(el('qqConnect'), async () => render(await request('qq/connect', 'POST')));
  el('qqDisconnect').onclick = () => work(el('qqDisconnect'), async () => render(await request('qq/disconnect', 'POST')));
  el('qqClear').onclick = () => confirm('清除 QQ 凭据', '将断开并清除 AppSecret，已绑定群保留。恢复同一机器人的凭据后可继续使用。', async () => {
    const s = await request('qq/clear', 'POST'); editing = false;
    el('qqSecretState').classList.remove('unsaved'); syncCredentials(s, true); render(s);
  });
  el('qqNewBinding').onclick = () => work(el('qqNewBinding'), async () => render(await request('qq/binding', 'POST')));
  el('qqGroupName').addEventListener('input', () => {aliasEdited = true;});
  el('qqCandidates').onchange = suggestAlias;
  el('qqConfirm').onclick = () => work(el('qqConfirm'), async () => render(await request('qq/binding/confirm', 'POST', {id: bindingId, candidate: el('qqCandidates').value, name: el('qqGroupName').value.trim()})));
  el('qqTestSend').onclick = () => {
    const group = state?.groups.find(g => String(g.id) === el('qqTestGroup').value);
    if (!group) {notice('请先选择已绑定并启用的群'); return;}
    const kind = el('qqTestKind').value;
    confirm('发送一次 QQ 测试', '将向“' + group.name + '”发送：' + el('qqTestKind').selectedOptions[0].text + '。', async () => {
      await request('qq/test/send', 'POST', {group_id: group.id, kind, attempt: crypto.randomUUID()}); render(await request('qq/status'));
    });
  };
  el('qqConfirmCancel').onclick = () => el('qqConfirmDialog').close();
  el('qqConfirmAction').onclick = () => work(el('qqConfirmAction'), async () => {await confirmAction(); el('qqConfirmDialog').close();});
  window.openQQTargets = async (id, name) => {
    try {
      const data = await request('courses/' + id + '/qq-targets'); targetCourse = id;
      el('qqTargetsTitle').textContent = name + ' · 通知群'; el('qqTargetChoices').replaceChildren();
      for (const g of data.groups) {
        const label = node('label', undefined, 'check'); const input = node('input'); input.type = 'checkbox'; input.value = g.id; input.checked = data.group_ids.includes(g.id);
        label.append(input, node('span', g.name + (g.enabled ? '' : '（已停用）'))); el('qqTargetChoices').append(label);
      }
      if (!data.groups.length) el('qqTargetChoices').append(node('p', '请先在设置 → 消息通知中绑定 QQ 群。'));
      el('qqTargetsDialog').showModal();
    } catch (e) {notice(e.message);}
  };
  el('qqTargetsCancel').onclick = () => el('qqTargetsDialog').close();
  el('qqTargetsForm').onsubmit = e => {
    e.preventDefault(); work(e.submitter, async () => {
      await request('courses/' + targetCourse + '/qq-targets', 'PUT', {group_ids: [...el('qqTargetChoices').querySelectorAll('input:checked')].map(x => Number(x.value))});
      el('qqTargetsDialog').close(); notice('通知群已保存；仅对新收到的题目生效。');
    });
  };
  poll();
})();
