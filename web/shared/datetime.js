/* Timestamp presentation only; stored source times and business dates stay intact. */
(() => {
  'use strict';
  const formatter = new Intl.DateTimeFormat('zh-CN', {
    timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'
  });
  function format(value, empty='—') {
    if (value === null || value === undefined || value === '') return empty;
    let input = typeof value === 'string' ? value.trim() : value;
    if (typeof input === 'string') {
      if (/^\d{4}-\d{2}-\d{2}$/.test(input)) return input;
      // Legacy timestamps without an offset were recorded in Beijing local time.
      if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(input)) {
        input = input.replace(' ', 'T') + '+08:00';
      }
    }
    const date = new Date(input);
    if (!Number.isFinite(date.getTime())) return empty;
    const parts = Object.fromEntries(formatter.formatToParts(date).map(part=>[part.type,part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
  }
  const api = {format};
  if (typeof window !== 'undefined') window.DashboardTime = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
