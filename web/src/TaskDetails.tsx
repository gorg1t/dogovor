import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Board, Quote, Task, TaskPatch } from "./api";
import { cap, colorFor, daysBetween, fmtDay, fmtDayLong, fmtTime, fmtTs, fmtWeekdayDate, fmtWeekdayTime, nextWeekday, shiftDay } from "./format";
import { IconBack, IconCalendar, IconCheck, IconClock, IconEdit, IconExternal, IconFlame, IconTrash, IconUser } from "./icons";
import { haptic, openLink } from "./telegram";
import { Avatar, Sheet, SheetHandle } from "./ui";

function Bubble({ q, main, i }: { q: Quote; main?: boolean; i: number }) {
  return (
    <div className="bubble-row" style={{ ["--i" as string]: i }}>
      <Avatar name={q.author} size="sm" />
      <div className={`bubble ${main ? "bubble--main" : ""}`}>
        <div className="bubble__author" style={{ color: colorFor(q.author) }}>{q.author}</div>
        <div className="bubble__text">{q.text}</div>
        <div className="bubble__time">{fmtTime(q.ts)}</div>
      </div>
    </div>
  );
}

export function statusOf(t: Task, today: string): { label: string; tone: "ok" | "late" | "soon" | "accent" } {
  if (t.status === "done") return { label: "Сделано", tone: "ok" };
  if (t.deadline && t.deadline < today) return { label: "Просрочено", tone: "late" };
  if (t.deadline && daysBetween(today, t.deadline) <= 1) return { label: daysBetween(today, t.deadline) === 0 ? "Сегодня" : "Завтра", tone: "soon" };
  return { label: "В работе", tone: "accent" };
}

type Props = {
  t: Task;
  board: Board;
  onClose: () => void;
  onSave: (t: Task, patch: TaskPatch) => Promise<void>;
  onDone: (t: Task) => void;
  onEdit: (t: Task) => void;
  onDelete: (t: Task) => void;
  canEdit: boolean;  // исполнитель, предложивший или автор; остальным — только просмотр
};

const TaskDetails = forwardRef<SheetHandle, Props>(function TaskDetails({ t, board, onClose, onSave, onDone, onEdit, onDelete, canEdit }, ref) {
  const sheet = useRef<SheetHandle>(null);
  useImperativeHandle(ref, () => ({ close: () => sheet.current?.close() }));
  const [moving, setMoving] = useState(false);
  const [date, setDate] = useState(t.deadline ?? "");
  // удаление — вторым нажатием: первое превращает корзину в красное «Удалить?» на 3 секунды
  const [arming, setArming] = useState(false);
  useEffect(() => {
    if (!arming) return;
    const id = window.setTimeout(() => setArming(false), 3000);
    return () => window.clearTimeout(id);
  }, [arming]);
  const today = board.today;
  const st = statusOf(t, today);
  const thread = [...t.context, t.quote, ...t.context_after];
  const quick: [string, string][] = [
    ["Сегодня", today], ["Завтра", shiftDay(today, 1)], ["Пятница", nextWeekday(today, 5)], ["Через неделю", shiftDay(today, 7)],
  ];
  const saveDate = async () => {
    haptic.success();
    await onSave(t, date ? { deadline: date } : { clear_deadline: true });
    setMoving(false);
  };

  const bar = !canEdit ? (
    <div className="rsvp__hint">{board.me ? `Менять задачу может ${[t.assignee, t.requested_by].filter(Boolean).join(" или ")}` : "Менять задачи можно в мини-аппе Telegram"}</div>
  ) : (
    <>
      <button className="btn btn--ghost press" onClick={() => { haptic.tap(); setMoving((v) => !v); }}>
        <IconCalendar size={18} /> Перенести
      </button>
      <button className="btn press" onClick={() => onDone(t)}>
        <IconCheck size={18} /> {t.status === "done" ? "Вернуть" : "Готово"}
      </button>
    </>
  );

  return (
    <Sheet ref={sheet} onClose={onClose} bar={bar} label={t.title}>
      <div className="sheet__top">
        <button className="icon-btn press" onClick={() => { haptic.tap(); sheet.current?.close(); }} aria-label="Назад">
          <IconBack size={18} />
        </button>
        {canEdit && <div style={{ display: "flex", gap: 8 }}>
          <button className={`icon-btn press ${arming ? "icon-btn--danger" : ""}`} aria-label="Удалить задачу"
                  onClick={() => { if (arming) { haptic.warn(); onDelete(t); } else { haptic.tap(); setArming(true); } }}>
            <IconTrash size={17} />{arming && <span className="icon-btn__label">Удалить?</span>}
          </button>
          <button className="icon-btn press" onClick={() => { haptic.tap(); onEdit(t); }} aria-label="Изменить">
            <IconEdit size={17} />
          </button>
        </div>}
      </div>

      <h2 className="d-title">{cap(t.title)}</h2>
      <div className="d-sub">
        <Avatar name={t.assignee} size="sm" /> <b>{t.assignee}</b>
        <span>· {t.deadline ? `до ${fmtDay(t.deadline)}` : t.deadline_text ? `«${t.deadline_text}»` : "без срока"}</span>
      </div>
      <div className="d-chips">
        <span className={`chip chip--${st.tone}`}>{st.tone === "late" && <IconFlame size={13} />}{st.label}</span>
        {t.kind === "claimed" && <span className="chip">взята кнопкой</span>}
        {t.edited && <span className="chip">исправлена</span>}
      </div>

      {moving && (
        <div className="move">
          <div className="move__chips">
            {quick.map(([label, d]) => (
              <button key={label} className={`pickchip press ${date === d ? "pickchip--on" : ""}`}
                      onClick={() => { haptic.select(); setDate(d); }}>
                {label} · {fmtDay(d)}
              </button>
            ))}
            <button className={`pickchip press ${date === "" ? "pickchip--on" : ""}`} onClick={() => { haptic.select(); setDate(""); }}>
              Без срока
            </button>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <input className="input" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
            <button className="btn btn--accent btn--sm press" style={{ height: 50 }} onClick={saveDate}>Сохранить</button>
          </div>
        </div>
      )}

      <div className="d-h">О задаче</div>
      <div className="facts">
        <div className="fact">
          <span className="fact__k"><IconCalendar size={17} />Срок</span>
          <span className="fact__v">{t.deadline ? fmtDayLong(t.deadline) : "без срока"}</span>
        </div>
        <div className="fact">
          <span className="fact__k"><IconFlame size={17} />Чекпоинт</span>
          <span className="fact__v">{board.chat.checkpoint ? fmtDayLong(board.chat.checkpoint) : "не задан"}</span>
        </div>
        {t.requested_by && (
          <div className="fact">
            <span className="fact__k"><IconUser size={17} />Предложил</span>
            <span className="fact__v">{t.requested_by}</span>
          </div>
        )}
        <div className="fact">
          <span className="fact__k"><IconUser size={17} />Подтвердил</span>
          <span className="fact__v">{t.confirmed_by}</span>
        </div>
        <div className="fact">
          <span className="fact__k"><IconClock size={17} />Создана</span>
          <span className="fact__v">{fmtTs(t.created_at)}</span>
        </div>
      </div>

      <div className="d-h">Из чата · {fmtWeekdayDate(t.quote.ts)}</div>
      <div className="thread">
        {thread.map((q, i) => <Bubble key={i} q={q} i={i} main={q === t.quote} />)}
      </div>
      {t.tg_link && (
        <a className="tg-link press" href={t.tg_link} target="_blank" rel="noreferrer"
           onClick={(e) => { haptic.tap(); if (openLink(t.tg_link!)) e.preventDefault(); }}>
          Открыть в Telegram <IconExternal size={15} />
        </a>
      )}

      {t.history.length > 0 && (
        <>
          <div className="d-h">История</div>
          <div className="timeline">
            {[...t.history].reverse().map((h, i) => (
              <div key={i} className="tl">
                <div className="tl__ts">{fmtWeekdayTime(h.ts)}</div>
                <div>{h.text}</div>
              </div>
            ))}
          </div>
        </>
      )}
    </Sheet>
  );
});

export default TaskDetails;
