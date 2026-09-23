const assert = require('node:assert/strict');
const {day} = require('../static/business-time.js');
assert.equal(day(new Date('2026-09-20T15:59:59Z')), '2026-09-20');
assert.equal(day(new Date('2026-09-20T16:00:00Z')), '2026-09-21');
assert.equal(day(new Date('2026-12-31T16:00:00Z')), '2027-01-01');
assert.equal(day(new Date('2028-02-28T16:00:00Z')), '2028-02-29');
console.log('Beijing business date boundaries passed');
