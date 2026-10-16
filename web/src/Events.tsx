import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { EventItem, EventsResponse, Rsvp, cancelEvent, fetchEvents, rsvpEvent } from "./api";
import { colorFor, parseDay, shiftDay } from "./format";
import { IconChevron, IconClock, IconExternal, IconPin, IconTrash } from "./icons";
import { haptic, openLink } from "./telegram";
import { Avatar, Sheet, SheetHandle, ToastState, plural } from "./ui";

/** Раздел «Встречи»: неделя полосой и день по часам — как календарь в Outlook, только в стиле доски. */

const WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const WD_LONG = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"];
const HOUR_PX = 64;
const DEFAULT_MIN = 60; // длительность встречи не знаем — рисуем час

const weekday = (iso: string) => (parseDay(iso).getDay() + 6) % 7; // 0 — понедельник
const mondayOf = (iso: string) => shiftDay(iso, -weekday(iso));
/** Минуты от полуночи по Москве: время берём прямо из ISO с «+03:00», чтобы не зависеть от пояса телефона. */
const minutesOf = (iso: string) => Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16));
const hhmm = (min: number) => `${String(Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;
const dayLong = (iso: string) => `${WD_LONG[weekday(iso)]}, ${Number(iso.slice(8, 10))} ${MONTHS_GEN[Number(iso.slice(5, 7)) - 1]}`;

function whenLabel(e: EventItem): string {
  if (e.starts_at) return `${dayLong(e.day ?? e.starts_at.slice(0, 10))} · ${e.starts_at.slice(11, 16)}`;
  if (e.day) return `${dayLong(e.day)} · время не назначено`;
  return e.when_text ? `«${e.when_text}»` : "Когда — пока не решили";
}

const going = (e: EventItem) => e.attendees.filter((a) => a.status === "yes");

function Faces({ e, max = 4 }: { e: EventItem; max?: number }) {
  const g = going(e);
  return (
    <span className="faces">
      {g.slice(0, max).map((a) => <Avatar key={a.user_id} name={a.name} size="xs" className="faces__one" />)}
      {g.length > max && <span className="faces__more">+{g.length - max}</span>}
    </span>
  );
}

/** Раскладка пересекающихся встреч по колонкам, чтобы блоки не лежали друг на друге. */
function layout(items: EventItem[]) {
  const sorted = [...items].sort((a, b) => minutesOf(a.starts_at!) - minutesOf(b.starts_at!));
  const out: { e: EventItem; col: number; cols: number }[] = [];
  let cluster: { e: EventItem; col: number }[] = [];
  let clusterEnd = -1;
  const flush = () => {
    const cols = Math.max(1, ...cluster.map((c) => c.col + 1));
    cluster.forEach((c) => out.push({ ...c, cols }));
    cluster = [];
  };
  for (const e of sorted) {
    const start = minutesOf(e.starts_at!);
    if (start >= clusterEnd) { flush(); clusterEnd = -1; }
    const busy = new Set(cluster.filter((c) => minutesOf(c.e.starts_at!) + DEFAULT_MIN > start).map((c) => c.col));
    let col = 0;
    while (busy.has(col)) col++;
    cluster.push({ e, col });
    clusterEnd = Math.max(clusterEnd, start + DEFAULT_MIN);
  }
  flush();
  return out;
}

type Props = { onToast: (t: ToastState) => void; tick: number };
export type EventsHandle = { closeSheet: () => boolean };

const Events = forwardRef<EventsHandle, Props>(function Events({ onToast, tick }, ref) {
  const [data, setData] = useState<EventsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [week, setWeek] = useState<string | null>(null);
  const [openedId, setOpenedId] = useState<number | null>(null);
  const sheet = useRef<SheetHandle>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const touch = useRef<number | null>(null);
  useImperativeHandle(ref, () => ({ closeSheet: () => { if (openedId) { sheet.current?.close(); return true; } return false; } }));

  const load = useCallback(() => {
    fetchEvents().then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message));
  }, []);
  useEffect(() => { load(); const id = window.setInterval(load, 15000); return () => window.clearInterval(id); }, [load]);
  useEffect(() => { if (tick) load(); }, [tick, load]);  // «Обновить» в шапке

  const today = data?.today ?? null;
  useEffect(() => {
    if (!today || selected) return;
    // открываем сегодня, а если сегодня пусто — ближайший день со встречей на этой неделе
    const firstSoon = data!.events.map((e) => e.day).filter((d): d is string => !!d && d >= today).sort()[0];
    const start = firstSoon && firstSoon <= shiftDay(mondayOf(today), 6) ? firstSoon : today;
    setSelected(start);
    setWeek(mondayOf(start));
  }, [today, data, selected]);

  const byDay = useMemo(() => {
    const m = new Map<string, EventItem[]>();
    for (const e of data?.events ?? []) if (e.day) m.set(e.day, [...(m.get(e.day) ?? []), e]);
    return m;
  }, [data]);

  const dayEvents = selected ? byDay.get(selected) ?? [] : [];
  const timed = dayEvents.filter((e) => e.starts_at);
  const untimed = dayEvents.filter((e) => !e.starts_at);
  const undated = (data?.events ?? []).filter((e) => !e.day);
  const nowMin = data ? minutesOf(data.now) : 0;
  const isToday = selected === today;
  const fromH = Math.min(8, ...timed.map((e) => Math.floor(minutesOf(e.starts_at!) / 60)), isToday ? Math.floor(nowMin / 60) : 24);
  const toH = Math.max(22, ...timed.map((e) => Math.ceil((minutesOf(e.starts_at!) + DEFAULT_MIN) / 60)));

  // при смене дня — прокрутить шкалу к первой встрече или к «сейчас»
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const target = timed.length ? Math.min(...timed.map((e) => minutesOf(e.starts_at!))) : isToday ? nowMin : 9 * 60;
    el.scrollTo({ top: Math.max(0, (target / 60 - fromH) * HOUR_PX - 24), behavior: "smooth" });
  }, [selected, timed.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const thisWeek = data ? data.events.filter((e) => e.day && week && e.day >= week && e.day <= shiftDay(week, 6)).length : 0;
  const opened = data?.events.find((e) => e.id === openedId) ?? null;

  const moveWeek = (dir: number) => {
    if (!week) return;
    haptic.select();
    const w = shiftDay(week, dir * 7);
    setWeek(w);
    setSelected(today && today >= w && today <= shiftDay(w, 6) ? today : w);
  };

  const answer = async (e: EventItem, status: Rsvp) => {
    haptic.success();
    const me = data?.me;
    if (me) {  // сразу показываем свой ответ, не дожидаясь сервера
      setData((d) => d && { ...d, events: d.events.map((x) => x.id !== e.id ? x : {
        ...x, status: "planned", attendees: [...x.attendees.filter((a) => a.user_id !== me.id), { user_id: me.id, name: me.name, status }],
      }) });
    }
    try { await rsvpEvent(e.id, status); onToast({ id: Date.now(), text: status === "yes" ? "Записал: будете" : status === "maybe" ? "Записал: может быть" : "Записал: не сможете" }); }
    catch (err) { haptic.warn(); onToast({ id: Date.now(), text: (err as Error).message }); }
    load();
  };

  const cancel = async (e: EventItem) => {
    haptic.warn();
    sheet.current?.close();
    setData((d) => d && { ...d, events: d.events.filter((x) => x.id !== e.id) });
    try { await cancelEvent(e.id); onToast({ id: Date.now(), text: `Встреча «${e.title}» удалена` }); }
    catch (err) { onToast({ id: Date.now(), text: (err as Error).message }); load(); }
  };

  if (!data || !selected || !week) {
    return (
      <>
        <div className="skel skel--hero" />
        <div className="skel" style={{ height: 86, borderRadius: 22, marginBottom: 16 }} />
        {[0, 1, 2].map((i) => <div key={i} className="skel skel--card" style={{ animationDelay: `${i * 120}ms` }} />)}
        {error && <p className="err">{error}</p>}
      </>
    );
  }

  const days = Array.from({ length: 7 }, (_, i) => shiftDay(week, i));
  const month = MONTHS[Number(days[3].slice(5, 7)) - 1];

  return (
    <>
      <h1 className="hero">Встречи. <span className="hero__dim">{thisWeek ? `${thisWeek} ${plural(thisWeek, "встреча", "встречи", "встреч")} на неделе` : "Неделя свободна"}</span></h1>

      <div className="ev-week" onTouchStart={(e) => { touch.current = e.touches[0].clientX; }}
           onTouchEnd={(e) => { if (touch.current === null) return; const dx = e.changedTouches[0].clientX - touch.current; touch.current = null; if (Math.abs(dx) > 50) moveWeek(dx < 0 ? 1 : -1); }}>
        <div className="ev-week__head">
          <button className="ev-week__arrow press" onClick={() => moveWeek(-1)} aria-label="Прошлая неделя"><IconChevron dir="left" size={18} /></button>
          <span className="ev-week__month">{month[0].toUpperCase() + month.slice(1)} {days[3].slice(0, 4)}</span>
          <button className="ev-week__arrow press" onClick={() => moveWeek(1)} aria-label="Следующая неделя"><IconChevron dir="right" size={18} /></button>
        </div>
        <div className="ev-week__days">
          {days.map((d, i) => {
            const n = byDay.get(d)?.length ?? 0;
            return (
              <button key={d} className={`ev-day press ${d === selected ? "ev-day--on" : ""} ${d === today ? "ev-day--today" : ""} ${d < data.today ? "ev-day--past" : ""}`}
                      onClick={() => { haptic.select(); setSelected(d); }}>
                <span className="ev-day__wd">{WD[i]}</span>
                <span className="ev-day__num">{Number(d.slice(8, 10))}</span>
                <span className="ev-day__dots">{Array.from({ length: Math.min(n, 3) }, (_, k) => <i key={k} />)}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="section"><span className={`section__dot ${isToday ? "section__dot--soon" : ""}`} />{isToday ? "Сегодня" : dayLong(selected)} · {dayEvents.length ? `${dayEvents.length} ${plural(dayEvents.length, "встреча", "встречи", "встреч")}` : "встреч нет"}</div>

      {untimed.map((e) => (
        <button key={e.id} className={`ev-allday press ${e.status === "proposed" ? "ev-allday--proposed" : ""}`} onClick={() => { haptic.tap(); setOpenedId(e.id); }}>
          <span className="ev-allday__title">{e.title}</span>
          <span className="ev-allday__meta">время не назначено <Faces e={e} max={3} /></span>
        </button>
      ))}

      {dayEvents.length === 0 ? (
        <div className="empty" style={{ padding: "26px 20px" }}>
          <div className="empty__icon">🗓</div>
          <div className="empty__title">В этот день свободно</div>
          Напишите в чате «кто придёт на созвон завтра в 19?» — встреча появится здесь
        </div>
      ) : timed.length > 0 && (
        <div className="ev-scroll" ref={scroller}>
          <div className="ev-grid" style={{ height: (toH - fromH) * HOUR_PX }}>
            {Array.from({ length: toH - fromH }, (_, i) => (
              <div key={i} className="ev-hour" style={{ top: i * HOUR_PX }}><span>{hhmm((fromH + i) * 60)}</span></div>
            ))}
            {isToday && nowMin >= fromH * 60 && nowMin <= toH * 60 && (
              <div className="ev-now" style={{ top: (nowMin / 60 - fromH) * HOUR_PX }}><span>{hhmm(nowMin)}</span></div>
            )}
            {layout(timed).map(({ e, col, cols }, i) => {
              const start = minutesOf(e.starts_at!);
              const past = isToday ? start + DEFAULT_MIN < nowMin : selected < data.today;
              return (
                <button key={e.id} className={`ev-block press ${e.status === "proposed" ? "ev-block--proposed" : ""} ${past ? "ev-block--past" : ""}`}
                        style={{ top: (start / 60 - fromH) * HOUR_PX + 2, height: HOUR_PX - 6, left: `calc(56px + (100% - 60px) * ${col / cols})`,
                                 width: `calc((100% - 60px) / ${cols} - 4px)`, ["--c" as string]: colorFor(e.title), ["--i" as string]: i }}
                        onClick={() => { haptic.tap(); setOpenedId(e.id); }}>
                  <span className="ev-block__title">{e.title}</span>
                  <span className="ev-block__meta"><span className="ev-block__time">{e.starts_at!.slice(11, 16)}</span><Faces e={e} max={3} /></span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {undated.length > 0 && (
        <>
          <div className="section"><span className="section__dot" />Без даты · {undated.length}</div>
          {undated.map((e) => (
            <button key={e.id} className="ev-allday ev-allday--proposed press" onClick={() => { haptic.tap(); setOpenedId(e.id); }}>
              <span className="ev-allday__title">{e.title}</span>
              <span className="ev-allday__meta">{e.when_text ? `«${e.when_text}»` : "когда — пока не решили"} <Faces e={e} max={3} /></span>
            </button>
          ))}
        </>
      )}

      {opened && <EventSheet ref={sheet} e={opened} me={data.me} onClose={() => setOpenedId(null)} onAnswer={answer} onCancel={cancel} />}
    </>
  );
});

export default Events;

const EventSheet = forwardRef<SheetHandle, {
  e: EventItem; me: EventsResponse["me"]; onClose: () => void; onAnswer: (e: EventItem, s: Rsvp) => void; onCancel: (e: EventItem) => void;
}>(function EventSheet({ e, me, onClose, onAnswer, onCancel }, ref) {
  const sheet = useRef<SheetHandle>(null);
  useImperativeHandle(ref, () => ({ close: () => sheet.current?.close() }));
  const [arming, setArming] = useState(false);
  useEffect(() => { if (!arming) return; const id = window.setTimeout(() => setArming(false), 3000); return () => window.clearTimeout(id); }, [arming]);
  const mine = me ? e.attendees.find((a) => a.user_id === me.id)?.status : undefined;
  const groups: [Rsvp, string][] = [["yes", "Будут"], ["maybe", "Может быть"], ["no", "Не смогут"]];

  const bar = me ? (
    <div className="rsvp">
      {([["yes", "✅ Буду"], ["maybe", "🤔 Может"], ["no", "✖️ Не смогу"]] as [Rsvp, string][]).map(([s, label]) => (
        <button key={s} className={`rsvp__btn press ${mine === s ? `rsvp__btn--on rsvp__btn--${s}` : ""}`} onClick={() => onAnswer(e, s)}>{label}</button>
      ))}
    </div>
  ) : <div className="rsvp__hint">Отметиться можно кнопкой в карточке встречи в чате или из мини-аппа в Telegram</div>;

  return (
    <Sheet ref={sheet} onClose={onClose} bar={bar} label={e.title}>
      <div className="sheet__top">
        <button className="icon-btn press" onClick={() => { haptic.tap(); sheet.current?.close(); }} aria-label="Назад"><IconChevron dir="left" size={18} /></button>
        <button className={`icon-btn press ${arming ? "icon-btn--danger" : ""}`} aria-label="Удалить встречу"
                onClick={() => { if (arming) onCancel(e); else { haptic.tap(); setArming(true); } }}>
          <IconTrash size={17} />{arming && <span className="icon-btn__label">Удалить?</span>}
        </button>
      </div>
      <div className="ev-hero" style={{ ["--c" as string]: colorFor(e.title) }}>
        <span className={`chip ${e.status === "planned" ? "chip--ok" : "chip--soon"}`}>{e.status === "planned" ? "Запланирована" : "Ждёт ответов"}</span>
        <h2 className="d-title">{e.title}</h2>
        <div className="ev-hero__when"><IconClock size={16} />{whenLabel(e)}</div>
        {e.place && <div className="ev-hero__when"><IconPin size={16} />{e.place}</div>}
      </div>

      {groups.map(([s, title]) => {
        const people = e.attendees.filter((a) => a.status === s);
        if (!people.length) return null;
        return (
          <div key={s}>
            <div className="d-h">{title} · {people.length}</div>
            <div className="ev-people">
              {people.map((a, i) => (
                <div key={a.user_id} className="ev-person" style={{ ["--i" as string]: i }}>
                  <Avatar name={a.name} size="sm" /><span>{a.name}</span>
                  {a.name === e.created_by && <span className="ev-person__tag">предложил(а)</span>}
                </div>
              ))}
            </div>
          </div>
        );
      })}

      <div className="d-h">Из чата</div>
      <div className="bubble-row">
        <Avatar name={e.created_by} size="sm" />
        <div className="bubble bubble--main">
          <div className="bubble__author" style={{ color: colorFor(e.created_by) }}>{e.created_by}</div>
          <div className="bubble__text">{e.quote}</div>
        </div>
      </div>
      {e.tg_link && (
        <a className="tg-link press" href={e.tg_link} target="_blank" rel="noreferrer"
           onClick={(ev) => { haptic.tap(); if (openLink(e.tg_link!)) ev.preventDefault(); }}>
          Открыть в Telegram <IconExternal size={15} />
        </a>
      )}
    </Sheet>
  );
});
