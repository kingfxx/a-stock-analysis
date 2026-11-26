const assert = require('node:assert/strict');
require('../web/features/portfolio/returns.js');
const { periodReturns } = globalThis.PortfolioReturns;

const first = '2021-01-01';
const year = '2022-01-01';
const end = '2023-01-01';
const close = (date, total_assets) => ({ date, total_assets });
const flow = (date, deposit, withdrawal = 0) => ({ date, in: deposit, out: withdrawal });
const near = (actual, expected) => {
  assert.notEqual(actual, null);
  assert.ok(Math.abs(actual - expected) < 1e-8, `${actual} != ${expected}`);
};
const investor = {
  opening_date: first,
  opening_assumed_zero: true,
  opening_assets: 0,
  records: [close(year, 170000), close(end, 170000)],
  transfers: [flow(first, 100000), flow(year, 50000)]
};
near(periodReturns(investor, first, [year, end], false).get(year), 0.20);
near(periodReturns(investor, first, [end], false).get(end), 20000 / 125000);
const expectedXirr = (Math.sqrt(70500) - 50) / 200 - 1;
near(periodReturns(investor, first, [end], true).get(end), expectedXirr);
near(periodReturns(investor, first, [year], true).get(year), 0.20);
near(periodReturns(investor, year, [end], false).get(end), 20000 / 125000);
near(periodReturns(investor, '2022-02-01', [end], false).get(end), 0);
near(periodReturns(investor, '2022-02-01', [end], true).get(end), 0);
assert.equal(periodReturns({ ...investor, return_gaps: [year] }, first, [end], true).get(end), null);
assert.equal(periodReturns({ ...investor, opening_assets: null }, first, [end], false).get(end), null);

// -100, +230, -132 has two distinct annual IRRs. The display leaves an ambiguous result blank.
const ambiguous = { opening_assets: 100, records: [close(end, 0)],
  transfers: [flow(year, 0, 230), flow(end, 132)] };
assert.equal(periodReturns(ambiguous, first, [end], true).get(end), null);

const following = '2022-01-02';
const day = { ...investor, records: [close(year, 170000), close(following, 171700)] };
near(periodReturns(day, following, [following], true, false).get(following), 0.01);
console.log('Modified Dietz and XIRR cases passed');
