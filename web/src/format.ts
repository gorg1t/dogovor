const WD = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
const WD_FULL = ["воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"];
const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const DAY = 86_400_000;

const pad = (n: number) => String(n).padStart(2, "0");

export const parseDay = (iso: string) => new Date(`${iso.slice(0, 10)}T00:00:00`);
export const daysBetween = (a: string, b: string) => Math.round((parseDay(b).getTime() - parseDay(a).getTime()) / DAY);

/** «пт 16.10» */
export const fmtDay = (iso: string) => {
  const d = parseDay(iso);
  return `${WD[d.getDay()]} ${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
};

/** «пятница, 16 октября» */
export const fmtDayLong = (iso: string) => {
  const d = parseDay(iso);
  return `${WD_FULL[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
};

/** «16.10 14:02» */
export const fmtTs = (iso: string) => {
  const d = new Date(iso);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

/** «14:02» */
export const fmtTime = (iso: string) => {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

/** «вт, 14:03» */
export const fmtWeekdayTime = (iso: string) => {
  const d = new Date(iso);
  return `${WD[d.getDay()]}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

/** «вт, 29 сентября» */
export const fmtWeekdayDate = (iso: string) => {
  const d = new Date(iso);
  return `${WD[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
};

export const norm = (s: string) => s.toLowerCase().replace(/ё/g, "е");

const PALETTE = ["#4f6bed", "#0e9f6e", "#d9480f", "#9c36b5", "#c2255c", "#1098ad", "#5c940d", "#e67700"];
export const colorFor = (name: string) => PALETTE[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % PALETTE.length];
export const initials = (name: string) => name.split(/\s+/).map((p) => p[0]).join("").slice(0, 2).toUpperCase();

/** Дата в формате YYYY-MM-DD относительно today (для быстрых кнопок переноса). */
export const shiftDay = (today: string, days: number) => {
  const d = parseDay(today);
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

export const nextWeekday = (today: string, wd: number) => {
  const d = parseDay(today);
  const delta = (wd - d.getDay() + 7) % 7 || 7;
  return shiftDay(today, delta);
};

/** «сделать экспорт» → «Сделать экспорт»: задачи из чата часто с маленькой буквы. */
export const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
