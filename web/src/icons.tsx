/** Набор линейных иконок 24×24 (без внешних библиотек). */
type P = { size?: number; className?: string };

const svg = (d: JSX.Element, { size = 20, className }: P) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
       strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">{d}</svg>
);

export const IconTasks = (p: P) => svg(<><rect x="4" y="4" width="16" height="16" rx="4" /><path d="m8.5 12 2.5 2.5 4.5-5" /></>, p);
export const IconHand = (p: P) => svg(<><path d="M8 13V6.5a1.5 1.5 0 0 1 3 0V12" /><path d="M11 11V5a1.5 1.5 0 0 1 3 0v6" /><path d="M14 11V6.5a1.5 1.5 0 0 1 3 0V14a6 6 0 0 1-6 6h-.6a6 6 0 0 1-4.6-2.2L4 15.5a1.6 1.6 0 0 1 2.4-2.1L8 15" /></>, p);
export const IconChart = (p: P) => svg(<><path d="M4 20V10" /><path d="M10 20V4" /><path d="M16 20v-7" /><path d="M22 20H2" /></>, p);
export const IconSearch = (p: P) => svg(<><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>, p);
export const IconCheck = (p: P) => svg(<path d="m5 12.5 4.5 4.5L19 7.5" />, p);
export const IconBack = (p: P) => svg(<path d="m15 5-7 7 7 7" />, p);
export const IconEdit = (p: P) => svg(<><path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4Z" /><path d="m13.5 6.5 4 4" /></>, p);
export const IconCalendar = (p: P) => svg(<><rect x="3.5" y="5" width="17" height="15" rx="3" /><path d="M3.5 10h17M8 3v4M16 3v4" /></>, p);
export const IconClock = (p: P) => svg(<><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>, p);
export const IconExternal = (p: P) => svg(<><path d="M14 4h6v6" /><path d="M20 4 11 13" /><path d="M19 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h4" /></>, p);
export const IconCopy = (p: P) => svg(<><rect x="8" y="8" width="12" height="12" rx="3" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></>, p);
export const IconFlame = (p: P) => svg(<path d="M12 21a6.5 6.5 0 0 0 6.5-6.5c0-3.5-2.6-5.6-3.5-8.5-1.2 2-2.3 2.7-3.5 3C11 6 10 4.2 9.5 3 7 6 5.5 9.4 5.5 14.5A6.5 6.5 0 0 0 12 21Z" />, p);
export const IconGrid = (p: P) => svg(<><rect x="4" y="4" width="6.5" height="6.5" rx="2" /><rect x="13.5" y="4" width="6.5" height="6.5" rx="2" /><rect x="4" y="13.5" width="6.5" height="6.5" rx="2" /><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="2" /></>, p);
export const IconUser = (p: P) => svg(<><circle cx="12" cy="8.5" r="3.8" /><path d="M4.5 20a7.5 7.5 0 0 1 15 0" /></>, p);
export const IconX = (p: P) => svg(<path d="M6 6l12 12M18 6 6 18" />, p);
export const IconMessage = (p: P) => svg(<path d="M5 18.5 4 21l3.2-1.4A8.5 8.5 0 1 0 5 18.5Z" />, p);
export const IconUndo = (p: P) => svg(<><path d="M9 14 4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" /></>, p);
export const IconTrash = (p: P) => svg(<><path d="M4 7h16" /><path d="M10 11v6M14 11v6" /><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12" /><path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7" /></>, p);
export const IconPin = (p: P) => svg(<><path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11Z" /><circle cx="12" cy="10" r="2.4" /></>, p);
export const IconChevron = (p: P & { dir?: "left" | "right" }) => svg(<path d={p.dir === "left" ? "m15 5-7 7 7 7" : "m9 5 7 7-7 7"} />, p);
