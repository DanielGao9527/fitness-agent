"use strict";
(() => {
  const day = (now = new Date()) => {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    }).formatToParts(now);
    const value = (type) => parts.find((part) => part.type === type).value;
    return `${value("year")}-${value("month")}-${value("day")}`;
  };
  const exported = { day };
  if (typeof module !== "undefined") module.exports = exported;
  if (typeof window !== "undefined") window.BusinessTime = exported;
})();
