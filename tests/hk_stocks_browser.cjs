const assert = require('node:assert/strict');

(async () => {
  const [port, base] = process.argv.slice(2);
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  try {
    let index = 0;
    const pending = new Map();
    const starts = [];
    socket.onmessage = ({data}) => {
      const message = JSON.parse(data);
      if (message.method === 'Network.requestWillBeSent') {
        const request = message.params.request;
        if (request.url.endsWith('/api/stock-collection') && request.method === 'POST') starts.push(request);
      }
      const callback = pending.get(message.id);
      if (callback) {
        pending.delete(message.id);
        if (message.error) callback.reject(Error(message.error.message));
        else callback.resolve(message.result);
      }
    };
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++index;
      pending.set(id, {resolve, reject});
      socket.send(JSON.stringify({id, method, params}));
    });
    const evaluate = async expression => {
      const result = await send('Runtime.evaluate', {expression, returnByValue: true});
      if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const wait = async expression => {
      for (let attempt = 0; attempt < 160; attempt++) {
        if (await evaluate(expression)) return;
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      throw Error('Timed out: ' + expression);
    };
    await send('Network.enable');
    await send('Page.navigate', {url: base + '/?code=01919'});
    await wait('document.getElementById("stock-market-caption")?.textContent.startsWith("港股")');
    assert.equal(await evaluate('document.getElementById("chips-heading").hidden'), true);
    await wait('document.getElementById("hk-latest-prices")?.textContent.includes("未复权收盘价 14.06") && document.getElementById("hk-latest-prices")?.textContent.includes("前复权收盘价 14.06")');
    await wait('document.getElementById("hk-dividend-events")?.textContent.includes("0.50414")');
    await evaluate('document.getElementById("business-tab").click()');
    await wait('document.getElementById("bs-company")?.textContent.includes("CNY")');
    await evaluate('document.getElementById("statements-tab").click()');
    await wait('document.getElementById("fs-company")?.textContent.includes("CNY")');
    await evaluate('document.getElementById("stock-picker-summary").click()');
    await evaluate('(()=>{const search=document.getElementById("cached-search");search.value="中远海控";search.dispatchEvent(new Event("input"));})()');
    await wait('(()=>{const text=document.querySelector("#cached-picker")?.textContent;return text?.includes("01919") && text?.includes("601919");})()');
    assert.equal(starts.length, 0);
  } finally {
    socket.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
