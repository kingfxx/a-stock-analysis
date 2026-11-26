/* Account returns from dated external cash flows and close valuations. */
(() => {
  'use strict';
  const dayNumber = (date) => Date.parse(date) / 86400000;
  const logRates = [-32, -24, -16, -12, -8, -6, -4, -3, -2, -1.5, -1, -.75,
    ...Array.from({ length: 41 }, (_, index) => (index - 20) / 40),
    .75, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 64, 128, 256, 512];

  function xirrLog(cashFlows) {
    const terms = [...cashFlows].filter(([, value]) => Math.abs(value) >= .005);
    if (terms.length < 2) return null;
    const origin = terms[0][0];
    const values = terms.map(([day, value]) => [(day - origin) / 365, value]);
    const lastTime = values.at(-1)[0];
    const scale = values.reduce((sum, [, value]) => sum + Math.abs(value), 0);
    function npv(logRate) {
      const shift = logRate < 0 ? lastTime : 0;
      return values.reduce((sum, [time, value]) =>
        sum + value / scale * Math.exp(-(time - shift) * logRate), 0);
    }
    const signChanges = values.slice(1).filter(([, value], index) =>
      Math.sign(value) !== Math.sign(values[index][1])).length;
    if (!signChanges) return null;
    const grid = signChanges === 1 ? [logRates[0], logRates.at(-1)] : logRates;
    let left = grid[0];
    let previous = npv(left);
    let bracket;
    for (const right of grid.slice(1)) {
      const next = npv(right);
      if (Math.abs(previous) < 1e-12) {
        if (bracket) return null;
        bracket = [left, left];
      } else if (Math.abs(next) >= 1e-12 && Math.sign(previous) !== Math.sign(next)) {
        if (bracket) return null;
        bracket = [left, right];
      }
      left = right;
      previous = next;
    }
    if (Math.abs(previous) < 1e-12) {
      if (bracket) return null;
      return left;
    }
    if (!bracket) return null;
    let [low, high] = bracket;
    const positive = npv(low) > 0;
    for (let step = 0; low !== high && step < 55; step++) {
      const middle = (low + high) / 2;
      if ((npv(middle) > 0) === positive) low = middle;
      else high = middle;
    }
    return (low + high) / 2;
  }

  function periodReturns(report, start, dates, annualXirr, annualize = true) {
    const preceding = report.records.findLast((row) => row.date < start);
    const initial = preceding ? preceding.total_assets : report.opening_assets;
    const openingDate = preceding?.date ?? report.opening_date ?? start;
    const opening = dayNumber(openingDate);
    const assets = new Map(report.records.map((row) => [row.date, row.total_assets]));
    const includeOpeningDay = !preceding && (report.opening_assumed_zero || openingDate === start);
    const afterOpening = (date) => date > openingDate || (includeOpeningDay && date === openingDate);
    const flows = report.transfers.filter(({date}) => afterOpening(date))
      .map(({date, in: deposit, out: withdrawal}) => [dayNumber(date), Number(deposit) - Number(withdrawal)])
      .filter(([, amount]) => Math.abs(amount) >= .005);
    const firstGap = (report.return_gaps ?? []).find(afterOpening);
    const results = new Map();
    for (const date of dates) {
      const closing = assets.get(date);
      const end = dayNumber(date);
      const duration = end - opening;
      let value = null;
      if (initial != null && Number(initial) >= 0 && closing != null && duration > 0
          && (!firstGap || date < firstGap)) {
        const movements = flows.filter(([day]) => day <= end);
        if (annualXirr) {
          const cashFlows = new Map([[opening, -Number(initial)]]);
          for (const [day, amount] of movements) cashFlows.set(day, (cashFlows.get(day) ?? 0) - amount);
          cashFlows.set(end, (cashFlows.get(end) ?? 0) + Number(closing));
          const logRate = xirrLog(cashFlows);
          if (logRate != null) value = Math.expm1(annualize ? logRate : logRate * duration / 365);
        } else {
          const net = movements.reduce((sum, [, amount]) => sum + amount, 0);
          const capital = movements.reduce((sum, [day, amount]) =>
            sum + amount * (end - day) / duration, Number(initial));
          if (capital > .005) value = (Number(closing) - Number(initial) - net) / capital;
        }
      }
      results.set(date, value);
    }
    return results;
  }

  globalThis.PortfolioReturns = { periodReturns };
})();
