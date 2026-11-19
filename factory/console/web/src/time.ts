// Times as the console shows them: in the laptop's own zone (IST), named as it is there (the plan's Display rules)

const hm = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const day = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });
const zone = new Intl.DateTimeFormat("en-IN", { timeZoneName: "short" }); // en-IN names Asia/Kolkata "IST"

/** "18:14 IST" today, "Mon 9 Nov 18:14 IST" on any other day */
export function at(d: Date, now = new Date()): string {
  const name = zone.formatToParts(d).find((p) => p.type === "timeZoneName")?.value;
  return `${day.format(d) === day.format(now) ? "" : `${day.format(d)} `}${hm.format(d)} ${name}`;
}

/** "Mon 9 Nov · 18:20 IST" */
export const clock = (now: Date) => `${day.format(now)} · ${at(now, now)}`;
