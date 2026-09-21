/* Independent page lease: unaffected by model settings or classroom rendering errors. */
(() => {
  const label = document.getElementById('appLifecycle');
  const button = document.getElementById('exitApplication');
  const dialog = document.getElementById('exitApplicationDialog');
  const confirm = document.getElementById('confirmExitApplication');
  const cancel = document.getElementById('cancelExitApplication');
  const pageId = crypto.randomUUID();
  let current = null, socket = null, retry = null, heartbeat = null, stopped = false, leaving = false;

  function render(data) {
    current = data;
    if (data.shutting_down) {
      stopped = true; cleanup();
      label.textContent = '程序正在退出，可以关闭此页面';
      button.disabled = true;
      return;
    }
    label.textContent = data.mode === 'desktop'
      ? '桌面模式 · 最后一个页面关闭后 3 分钟退出'
      : '长期后台模式 · 关闭网页不停止服务';
    label.title = `已连接 ${data.page_count} 个看板页面。后台标签仍会保活。`;
    button.disabled = !data.can_exit;
  }
  function cleanup() {
    clearTimeout(retry); clearInterval(heartbeat);
    retry = null; heartbeat = null;
  }
  function connect() {
    if (stopped || leaving || socket && socket.readyState < WebSocket.CLOSING) return;
    cleanup();
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/app/presence?page_id=${pageId}`);
    socket = ws;
    ws.onmessage = e => {try {render(JSON.parse(e.data));} catch {}};
    ws.onopen = () => {
      // Timers are supplementary. The WS connection itself keeps the page present.
      ws.send('heartbeat');
      heartbeat = setInterval(() => {if (ws.readyState === WebSocket.OPEN) ws.send('heartbeat');}, 20000);
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null; cleanup();
      if (!stopped && !leaving) {
        label.textContent = '服务未连接；如已退出，请双击桌面快捷方式重新打开';
        button.disabled = true;
        retry = setTimeout(connect, 3000);
      }
    };
    ws.onerror = () => {}; // onclose handles reconnection; no duplicate retry loop.
  }
  window.addEventListener('pagehide', () => {
    leaving = true; cleanup();
    const ws = socket; socket = null;
    if (ws) ws.close(1000, 'page closed');
  });
  window.addEventListener('pageshow', () => {leaving = false; connect();});
  window.addEventListener('online', connect);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !stopped) {
      if (socket?.readyState === WebSocket.OPEN) socket.send('heartbeat');
      else connect();
    }
  });
  button.onclick = () => {if (current?.can_exit) dialog.showModal();};
  cancel.onclick = () => dialog.close();
  confirm.onclick = async () => {
    if (!current?.instance_id) return;
    confirm.disabled = true;
    try {
      const response = await fetch('/api/app/exit', {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Yktmon': 'local'},
        body: JSON.stringify({instance_id: current.instance_id}),
      });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || '退出失败');
      }
      stopped = true; cleanup();
      label.textContent = '正在停止监听与 QQ 连接，可以关闭此页面';
      button.disabled = true; dialog.close();
      if (socket) socket.close();
    } catch (e) {
      document.getElementById('exitApplicationError').textContent = e.message;
    } finally {confirm.disabled = false;}
  };
  connect();
})();
