import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Board, OpenItem, Task, TaskPatch, TgAuthResult, claimOpenItem, deleteTask, fetchBoard, hasToken, patchTask, setToken, tgAuth } from "./api";
import { cap, daysBetween, fmtDay, norm } from "./format";
import { IconCalendar, IconChart, IconCheck, IconCopy, IconFlame, IconGrid, IconHand, IconMessage, IconSearch, IconTasks, IconUser, IconX } from "./icons";
import Events, { EventsHandle } from "./Events";
import TaskDetails from "./TaskDetails";
import { haptic, initTelegram, openLink, tg, tgUser } from "./telegram";
import { Avatar, Sheet, SheetHandle, Toast, ToastState, plural } from "./ui";

type Tab = "tasks" | "free" | "report";
type Seg = "active" | "hot" | "done";

const threadOf = (t: Task) => [...t.context, t.quote, ...t.context_after];

function deadlineChip(t: Task, today: string): { text: string; tone: string } {
  if (!t.deadline) return { text: t.deadline_text ? `«${t.deadline_text}»` : "без срока", tone: "" };
  const n = daysBetween(today, t.deadline);
  const at = t.deadline_time ? ` ${t.deadline_time}` : "";  // «сегодня 18:00»
  if (t.status === "done") return { text: fmtDay(t.deadline), tone: "" };
  if (n < 0) return { text: `просрочено · ${fmtDay(t.deadline)}`, tone: "late" };
  if (n === 0) return { text: `сегодня${at}`, tone: t.urgent ? "late" : "soon" };
  if (n === 1) return { text: `завтра${at}`, tone: "soon" };
  return { text: `${fmtDay(t.deadline)}${at}`, tone: n <= 3 ? "accent" : "" };
}

/** Менять задачу может исполнитель, предложивший или автор; вход по ссылке из браузера — только просмотр. */
export const canEdit = (b: Board, t: Task) => !!b.me && (t.managers.length === 0 || t.managers.includes(b.me.id));

function Highlight({ text, q }: { text: string; q: string }) {
  const i = q ? norm(text).indexOf(norm(q)) : -1;
  if (i < 0) return <>{text}</>;
  return <>{text.slice(0, i)}<mark>{text.slice(i, i + q.length)}</mark>{text.slice(i + q.length)}</>;
}

function TaskCard({ t, today, q, i, checked, leaving, onOpen, onCheck }: {
  t: Task; today: string; q: string; i: number; checked: boolean; leaving: boolean;
  onOpen: (t: Task) => void; onCheck: (t: Task) => void;
}) {
  const dl = deadlineChip(t, today);
  const thread = threadOf(t);
  const hit = q && !norm(t.title).includes(norm(q)) ? thread.find((m) => norm(m.text).includes(norm(q))) : undefined;
  const isDone = t.status === "done" || checked;
  return (
    <div className={`tcard ${leaving ? "tcard--leaving" : ""} ${t.status === "done" ? "tcard--done" : ""}`}
         style={{ ["--i" as string]: Math.min(i, 8) }} role="button" tabIndex={0}
         onClick={() => { haptic.tap(); onOpen(t); }} onKeyDown={(e) => e.key === "Enter" && onOpen(t)}>
      <div className="tcard__body">
        <div className="tcard__title"><Highlight text={cap(t.title)} q={q} /></div>
        <div className="tcard__meta">
          <span className="tcard__who"><Avatar name={t.assignee} size="xs" />{t.assignee}</span>
          {t.urgent && t.status !== "done" && <span className="chip chip--late"><IconFlame size={12} />срочно</span>}
          <span className={`chip ${dl.tone ? `chip--${dl.tone}` : ""}`}>{dl.tone === "late" && !t.urgent && <IconFlame size={12} />}{dl.text}</span>
          {thread.length > 1 && <span className="tcard__who"><IconMessage size={14} />{thread.length}</span>}
        </div>
        {hit && <div className="tcard__hit">{hit.author}: «<Highlight text={hit.text} q={q} />»</div>}
      </div>
      <button className={`check ${isDone ? "check--on" : ""}`} aria-label={t.status === "done" ? "Вернуть в работу" : "Готово"}
              onClick={(e) => { e.stopPropagation(); onCheck(t); }}>
        <IconCheck size={22} />
      </button>
    </div>
  );
}

function Segmented({ value, onChange, items }: { value: Seg; onChange: (v: Seg) => void; items: { key: Seg; label: string; count: number }[] }) {
  const idx = items.findIndex((x) => x.key === value);
  return (
    <div className="seg">
      <span className="seg__pill" style={{ width: `calc((100% - 8px) / ${items.length})`, transform: `translateX(${idx * 100}%)` }} />
      {items.map((x) => (
        <button key={x.key} className={`seg__btn ${x.key === value ? "seg__btn--on" : ""}`}
                onClick={() => { if (x.key !== value) { haptic.select(); onChange(x.key); } }}>
          {x.label}{x.count > 0 && <span className="seg__count">{x.count}</span>}
        </button>
      ))}
    </div>
  );
}

function EditSheet({ t, members, onClose, onSave }: {
  t: Task; members: string[]; onClose: () => void; onSave: (t: Task, patch: TaskPatch) => Promise<void>;
}) {
  const ref = useRef<SheetHandle>(null);
  const [title, setTitle] = useState(t.title);
  const [assignee, setAssignee] = useState(t.assignee);
  const [deadline, setDeadline] = useState(t.deadline ?? "");
  const names = members.includes(t.assignee) ? members : [t.assignee, ...members];
  const submit = async () => {
    const patch: TaskPatch = {};
    if (title.trim() && title.trim() !== t.title) patch.title = title.trim();
    if (assignee !== t.assignee) patch.assignee = assignee;
    if (deadline !== (t.deadline ?? "")) { if (deadline) patch.deadline = deadline; else patch.clear_deadline = true; }
    if (Object.keys(patch).length) { haptic.success(); await onSave(t, patch); }
    ref.current?.close();
  };
  return (
    <Sheet ref={ref} onClose={onClose} label="Изменить задачу" bar={<>
      <button className="btn btn--ghost press" onClick={() => ref.current?.close()}>Отмена</button>
      <button className="btn press" onClick={submit}>Сохранить</button>
    </>}>
      <h2 className="d-title" style={{ marginTop: 8 }}>Изменить задачу</h2>
      <label className="field"><span className="field__label">Что сделать</span>
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label className="field"><span className="field__label">Ответственный</span>
        <select className="input" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
          {names.map((n) => <option key={n} value={n}>{n}</option>)}
        </select></label>
      <label className="field"><span className="field__label">Срок</span>
        <input className="input" type="date" value={deadline} onChange={(e) => setDeadline(e.target.value)} /></label>
      <button className={`pickchip press ${deadline === "" ? "pickchip--on" : ""}`} onClick={() => setDeadline("")}>Без срока</button>
    </Sheet>
  );
}

function FreeTab({ items, canClaim, onClaim }: { items: OpenItem[]; canClaim: boolean; onClaim: (it: OpenItem) => void }) {
  if (!items.length) {
    return <div className="empty"><div className="empty__icon">🙌</div><div className="empty__title">Всё разобрано</div>
      Когда в чате спросят «кто сделает?», задачи появятся здесь.</div>;
  }
  return (
    <>
      <div className="section">Ждут исполнителя · {items.length}</div>
      {items.map((it, i) => (
        <div key={it.key} className="fcard" style={{ ["--i" as string]: i }}>
          <div className="fcard__top">
            <div className="fcard__title">{cap(it.task)}</div>
            <span className={`chip ${it.deadline ? "chip--accent" : ""}`}>{it.deadline ? fmtDay(it.deadline) : it.deadline_text ? `«${it.deadline_text}»` : "без срока"}</span>
          </div>
          <div className="fcard__quote">«{it.quote}»</div>
          <div className="fcard__foot">
            <span className="tcard__who"><Avatar name={it.requested_by} size="xs" />просит {it.requested_by}</span>
            {canClaim ? (
              <button className="btn btn--accent btn--sm press" onClick={() => onClaim(it)}>
                <IconHand size={16} /> Беру
              </button>
            ) : it.tg_link && (
              <button className="btn btn--accent btn--sm press" onClick={() => { haptic.tap(); if (!openLink(it.tg_link!)) window.open(it.tg_link!, "_blank"); }}>
                <IconHand size={16} /> Взять в чате
              </button>
            )}
          </div>
        </div>
      ))}
    </>
  );
}

function reportText(b: Board): string {
  const line = (t: Task) => `• ${t.assignee} — ${t.title}${t.deadline ? ` (до ${fmtDay(t.deadline)})` : ""}`;
  const done = b.tasks.filter((t) => t.status === "done");
  const late = b.tasks.filter((t) => t.status === "open" && t.deadline && t.deadline < b.today);
  const open = b.tasks.filter((t) => t.status === "open" && !(t.deadline && t.deadline < b.today));
  const cp = b.chat.checkpoint ? ` к чекпоинту ${fmtDay(b.chat.checkpoint)}` : "";
  const block = (title: string, xs: Task[]) => [`${title} (${xs.length})`, ...(xs.length ? xs.map(line) : ["• —"])].join("\n");
  return [`Отчёт команды «${b.chat.title}»${cp}`, block("✅ Сделано", done), block("❗ Просрочено", late), block("⏳ В работе", open)].join("\n\n");
}

async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* веб-вью без clipboard API */ }
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select();
  const ok = document.execCommand("copy"); ta.remove(); return ok;
}

function ReportTab({ board, onCopied, onOpen }: { board: Board; onCopied: () => void; onOpen: (t: Task) => void }) {
  const done = board.tasks.filter((t) => t.status === "done");
  const late = board.tasks.filter((t) => t.status === "open" && t.deadline && t.deadline < board.today);
  const open = board.tasks.filter((t) => t.status === "open" && !(t.deadline && t.deadline < board.today));
  const block = (title: string, dot: string, xs: Task[]) => xs.length > 0 && (
    <>
      <div className="section"><span className={`section__dot ${dot}`} />{title} · {xs.length}</div>
      <div className="facts">
        {xs.map((t) => (
          <button key={t.id} className="rline press" style={{ width: "100%", textAlign: "left", padding: "12px 16px" }} onClick={() => onOpen(t)}>
            <Avatar name={t.assignee} size="xs" /><span className="rline__title">{cap(t.title)}</span>
            <span className="chip">{t.deadline ? fmtDay(t.deadline) : "—"}</span>
          </button>
        ))}
      </div>
    </>
  );
  return (
    <>
      <div className="stats">
        <div className="stat"><div className="stat__num" style={{ color: "var(--ok)" }}>{done.length}</div><div className="stat__label">сделано</div></div>
        <div className="stat" style={{ animationDelay: "60ms" }}><div className="stat__num" style={{ color: late.length ? "var(--late)" : undefined }}>{late.length}</div><div className="stat__label">просрочено</div></div>
        <div className="stat" style={{ animationDelay: "120ms" }}><div className="stat__num">{open.length}</div><div className="stat__label">в работе</div></div>
      </div>
      {block("Просрочено", "section__dot--late", late)}
      {block("В работе", "", open)}
      {block("Сделано", "section__dot--ok", done)}
      <button className="btn btn--block press" style={{ marginTop: 22 }}
              onClick={async () => { if (await copyText(reportText(board))) { haptic.success(); onCopied(); } }}>
        <IconCopy size={18} /> Скопировать отчёт к чекпоинту
      </button>
    </>
  );
}

export default function App() {
  const [board, setBoard] = useState<Board | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [auth, setAuth] = useState<"ok" | "loading" | "pick" | "none">(hasToken() ? "ok" : tg ? "loading" : "none");
  const [chats, setChats] = useState<NonNullable<TgAuthResult["chats"]>>([]);
  // ?tab=free / ?tab=report — открыть вкладку ссылкой
  const [tab, setTab] = useState<Tab>(() => (["free", "report"].includes(new URLSearchParams(window.location.search).get("tab") ?? "") ? new URLSearchParams(window.location.search).get("tab") : "tasks") as Tab);
  const [seg, setSeg] = useState<Seg>("active");
  const [person, setPerson] = useState<string>("all");
  const [q, setQ] = useState("");
  const [openedId, setOpenedId] = useState<number | null>(() => Number(new URLSearchParams(window.location.search).get("task")) || null);
  const [editing, setEditing] = useState<Task | null>(null);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [leaving, setLeaving] = useState<Set<number>>(new Set());
  const [toast, setToast] = useState<ToastState>(null);
  // «Встречи» — отдельный экран (?view=events), на доске задач их нет
  const [view, setView] = useState<"board" | "events">(() => (new URLSearchParams(window.location.search).get("view") === "events" ? "events" : "board"));
  const [eventsTick, setEventsTick] = useState(0);
  const eventsRef = useRef<EventsHandle>(null);
  const detailsRef = useRef<SheetHandle>(null);
  const clearToast = useCallback(() => setToast(null), []);

  useEffect(() => {
    initTelegram();
    if (auth !== "loading" || !tg) return;
    tgAuth(tg.initData)
      .then((r) => { if (r.token) { setToken(r.token); setAuth("ok"); } else { setChats(r.chats ?? []); setAuth("pick"); } })
      .catch((e: Error) => { setError(e.message); setAuth("none"); });
  }, [auth]);

  const load = useCallback((manual = false) => {
    if (manual) { setLoading(true); haptic.tap(); }
    fetchBoard()
      .then((b) => { setBoard(b); setError(null); })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (auth !== "ok") return;
    load();
    const id = window.setInterval(() => load(), 8000);
    const onVis = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onVis);
    return () => { window.clearInterval(id); document.removeEventListener("visibilitychange", onVis); };
  }, [load, auth]);

  // «Назад» в Telegram: закрыть правку → экран задачи → вернуться на вкладку задач
  useEffect(() => {
    if (!tg) return;
    const back = () => {
      haptic.tap();
      if (editing) setEditing(null);
      else if (openedId) detailsRef.current?.close();
      else if (view === "events") { if (!eventsRef.current?.closeSheet()) setView("board"); }
      else setTab("tasks");
    };
    if (editing || openedId || tab !== "tasks" || view === "events") {
      tg.BackButton.show();
      tg.BackButton.onClick(back);
      return () => tg?.BackButton.offClick(back);
    }
    tg.BackButton.hide();
  }, [editing, openedId, tab, view]);

  const save = useCallback(async (t: Task, patch: TaskPatch) => {
    if (patch.status) {
      const status = patch.status as Task["status"];
      setBoard((b) => b && { ...b, tasks: b.tasks.map((x) => (x.id === t.id ? { ...x, status } : x)) });
    }
    try { await patchTask(t.id, patch); } catch (e) { setError((e as Error).message); haptic.warn(); }
    load();
  }, [load]);

  /** Готово с «ощущением»: галочка рисуется → карточка уезжает → тост с отменой. */
  const complete = useCallback((t: Task) => {
    if (board && !canEdit(board, t)) {
      haptic.warn();
      setToast({ id: Date.now(), text: board.me ? "Отметить может исполнитель или автор задачи" : "Менять задачи можно в мини-аппе Telegram" });
      return;
    }
    if (t.status === "done") {
      haptic.tap();
      save(t, { status: "open" });
      setToast({ id: Date.now(), text: "Вернул в работу" });
      return;
    }
    haptic.success();
    setChecked((s) => new Set(s).add(t.id));
    window.setTimeout(() => setLeaving((s) => new Set(s).add(t.id)), 380);
    window.setTimeout(() => {
      save(t, { status: "done" });
      setChecked((s) => { const n = new Set(s); n.delete(t.id); return n; });
      setLeaving((s) => { const n = new Set(s); n.delete(t.id); return n; });
    }, 760);
    setToast({ id: Date.now(), text: `Готово: ${t.title}`, undo: () => { haptic.tap(); save(t, { status: "open" }); } });
  }, [save, board]);

  /** «🙋 Беру» на доске: свободная задача — на меня; в чате бот отметит это в карточке. */
  const claim = useCallback(async (it: OpenItem) => {
    haptic.success();
    setBoard((b) => b && { ...b, open_items: b.open_items.filter((x) => x.key !== it.key) });
    try { await claimOpenItem(it.key); setToast({ id: Date.now(), text: `Теперь ваша: ${it.task}` }); }
    catch (e) { haptic.warn(); setToast({ id: Date.now(), text: (e as Error).message }); }
    load();
  }, [load]);

  /** Удалить: карточка исчезает сразу, в тосте — «Отменить» (задача удаляется мягко и возвращается). */
  const remove = useCallback(async (t: Task) => {
    detailsRef.current?.close();
    setBoard((b) => b && { ...b, tasks: b.tasks.filter((x) => x.id !== t.id) });
    try { await deleteTask(t.id); } catch (e) { setError((e as Error).message); haptic.warn(); load(); return; }
    setToast({ id: Date.now(), text: `Удалено: ${t.title}`, undo: () => { haptic.tap(); save(t, { status: t.status === "done" ? "done" : "open" }); } });
  }, [load, save]);

  const myNames = useMemo(() => new Set(board?.tasks.filter((t) => tgUser && t.assignee_user_id === tgUser.id).map((t) => t.assignee) ?? []), [board]);
  const people = useMemo(() => [...new Set(board?.tasks.map((t) => t.assignee) ?? [])].filter((p) => !myNames.has(p))
    .sort((a, b) => a.localeCompare(b, "ru")), [board, myNames]);

  const visible = useMemo(() => {
    if (!board) return [];
    const nq = norm(q.trim());
    return board.tasks.filter((t) =>
      (person === "all" || (person === "me" ? t.assignee_user_id === tgUser?.id : t.assignee === person)) &&
      (!nq || [t.title, t.assignee, ...threadOf(t).map((m) => m.text)].some((s) => norm(s).includes(nq))));
  }, [board, q, person]);

  // ---------- экраны входа ----------
  if (auth === "loading") return <main className="screen-center"><div className="empty__icon">🤝</div><div className="empty__title">Входим через Telegram…</div></main>;
  if (auth === "pick") {
    return (
      <main className="screen-center">
        <div className="empty__icon">🤝</div>
        <div className="empty__title">Ваши команды</div>
        {chats.length ? (
          <div className="pick">
            {chats.map((c) => <button key={c.id} className="btn btn--ghost press" onClick={() => { haptic.tap(); setToken(c.token); setAuth("ok"); }}>{c.title || "Чат"}</button>)}
          </div>
        ) : <p style={{ color: "var(--muted)" }}>Добавьте бота в рабочий чат команды и напишите там что-нибудь.</p>}
      </main>
    );
  }
  if (auth === "none") {
    return (
      <main className="screen-center">
        <div className="empty__icon">🤝</div>
        <div className="empty__title">Договорились</div>
        {error && <p className="err">{error}</p>}
        <p style={{ color: "var(--muted)", maxWidth: 300 }}>Откройте доску из чата команды: <b>/board</b> → «Открыть доску».</p>
      </main>
    );
  }

  // ---------- загрузка ----------
  if (!board) {
    return (
      <div className="app">
        <div className="top"><div className="who"><div className="skel" style={{ width: 40, height: 40, borderRadius: "50%" }} /><div className="skel" style={{ width: 140, height: 16 }} /></div></div>
        <div className="skel skel--hero" />
        <div className="skel" style={{ height: 50, borderRadius: 999, marginBottom: 20 }} />
        {[0, 1, 2, 3].map((i) => <div key={i} className="skel skel--card" style={{ animationDelay: `${i * 120}ms` }} />)}
        {error && <p className="err">{error}</p>}
      </div>
    );
  }

  const today = board.today;
  const isLate = (t: Task) => t.status === "open" && !!t.deadline && t.deadline < today;
  const isHot = (t: Task) => t.status === "open" && (t.urgent || (!!t.deadline && daysBetween(today, t.deadline) <= 1));
  const active = visible.filter((t) => t.status === "open");
  const hot = visible.filter(isHot);
  const done = visible.filter((t) => t.status === "done").sort((a, b) => (b.done_at ?? "").localeCompare(a.done_at ?? ""));
  const allOpen = board.tasks.filter((t) => t.status === "open");
  const hotCount = board.tasks.filter(isHot).length;
  const cpDays = board.chat.checkpoint ? daysBetween(today, board.chat.checkpoint) : null;

  const groups: { key: string; title: string; dot: string; items: Task[] }[] =
    seg === "done" ? [{ key: "done", title: "Сделано", dot: "section__dot--ok", items: done }]
    : seg === "hot" ? [
        { key: "late", title: "Просрочено", dot: "section__dot--late", items: hot.filter(isLate) },
        { key: "soon", title: "Сегодня и завтра", dot: "section__dot--soon", items: hot.filter((t) => !isLate(t)) },
      ]
    : [
        { key: "late", title: "Просрочено", dot: "section__dot--late", items: active.filter(isLate) },
        { key: "soon", title: "Сегодня и завтра", dot: "section__dot--soon", items: active.filter((t) => isHot(t) && !isLate(t)) },
        { key: "week", title: "На неделе", dot: "", items: active.filter((t) => t.deadline && daysBetween(today, t.deadline) >= 2 && daysBetween(today, t.deadline) <= 7) },
        { key: "later", title: "Позже", dot: "", items: active.filter((t) => t.deadline && daysBetween(today, t.deadline) > 7) },
        { key: "none", title: "Без срока", dot: "", items: active.filter((t) => !t.deadline) },
      ];
  const shown = groups.filter((g) => g.items.length);
  const opened = board.tasks.find((t) => t.id === openedId) ?? null;
  const me = tgUser?.first_name || board.chat.title;
  let order = 0;

  return (
    <div className="app">
      <header className="top">
        <div className="who">
          <Avatar name={me} />
          <div className="who__text">
            <div className="who__label">{tgUser ? `Привет, ${tgUser.first_name}` : "Команда"}</div>
            <div className="who__name">{board.chat.title || "Команда"}</div>
          </div>
        </div>
        <div className="top__actions">
        <button className={`pill-btn press ${view === "events" ? "pill-btn--on" : ""}`} aria-label="Встречи"
                onClick={() => { haptic.select(); setView(view === "events" ? "board" : "events"); window.scrollTo({ top: 0, behavior: "smooth" }); }}>
          <IconCalendar size={17} /><span>Встречи</span>
        </button>
        <button className={`icon-btn press ${loading ? "icon-btn--spin" : ""}`} onClick={() => { load(true); setEventsTick((n) => n + 1); }} aria-label="Обновить">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7" /><path d="M20 4v7h-7" /></svg>
        </button>
        </div>
      </header>

      {!board.me && <div className="readonly">Только просмотр · менять задачи и отмечаться — в мини-аппе Telegram</div>}
      {view === "events" && <Events ref={eventsRef} tick={eventsTick} onToast={setToast} />}

      {view === "board" && tab === "tasks" && (
        <>
          <h1 className="hero">
            {cpDays !== null && cpDays >= 0 ? `До КТ ${cpDays} ${plural(cpDays, "день", "дня", "дней")}.` : `${allOpen.length} в работе.`}{" "}
            <span className="hero__dim">
              {cpDays !== null && cpDays >= 0 ? `${allOpen.length} в работе` : ""}
              {hotCount ? `${cpDays !== null && cpDays >= 0 ? " · " : ""}${hotCount} горит` : cpDays !== null && cpDays >= 0 ? "" : "Всё спокойно."}
            </span>
          </h1>

          <label className="search">
            <IconSearch size={18} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Найти задачу или кто что писал" />
            {q && <button className="press" onClick={() => setQ("")} aria-label="Очистить"><IconX size={16} /></button>}
          </label>

          <div className="people">
            <button className={`person ${person === "all" ? "person--on" : ""}`} onClick={() => { haptic.select(); setPerson("all"); }}>
              <span className="person__ring"><IconGrid size={20} /></span><span className="person__label">Все</span><span className="person__dot" />
            </button>
            {tgUser && (
              <button className={`person ${person === "me" ? "person--on" : ""}`} onClick={() => { haptic.select(); setPerson("me"); }}>
                <span className="person__ring">{myNames.size ? <Avatar name={[...myNames][0]} size="lg" /> : <IconUser size={20} />}</span><span className="person__label">Мои</span><span className="person__dot" />
              </button>
            )}
            {people.map((p) => (
              <button key={p} className={`person ${person === p ? "person--on" : ""}`} onClick={() => { haptic.select(); setPerson(p); }}>
                <span className="person__ring"><Avatar name={p} size="lg" /></span><span className="person__label">{p}</span><span className="person__dot" />
              </button>
            ))}
          </div>

          <Segmented value={seg} onChange={setSeg} items={[
            { key: "active", label: "Активные", count: active.length },
            { key: "hot", label: "Горит", count: hot.length },
            { key: "done", label: "Сделано", count: done.length },
          ]} />

          {shown.length === 0 && (
            <div className="empty">
              <div className="empty__icon">{seg === "done" ? "🌱" : q ? "🔍" : "🎉"}</div>
              <div className="empty__title">{q ? "Ничего не нашлось" : seg === "done" ? "Пока ничего не сделано" : seg === "hot" ? "Ничего не горит" : "Все задачи сделаны"}</div>
              {q ? "Попробуйте другое слово" : seg === "active" ? "Новые появятся, когда кто-то возьмёт задачу в чате" : ""}
            </div>
          )}
          {shown.map((g) => (
            <section key={`${seg}-${g.key}`}>
              <div className="section"><span className={`section__dot ${g.dot}`} />{g.title} · {g.items.length}</div>
              {g.items.map((t) => (
                <TaskCard key={t.id} t={t} today={today} q={q.trim()} i={order++} checked={checked.has(t.id)} leaving={leaving.has(t.id)}
                          onOpen={(x) => setOpenedId(x.id)} onCheck={complete} />
              ))}
            </section>
          ))}
        </>
      )}

      {view === "board" && tab === "free" && (<><h1 className="hero">Свободные. <span className="hero__dim">Кто возьмёт?</span></h1><FreeTab items={board.open_items} canClaim={!!board.me} onClaim={claim} /></>)}
      {view === "board" && tab === "report" && (
        <>
          <h1 className="hero">Отчёт. <span className="hero__dim">{board.chat.checkpoint ? `К КТ ${fmtDay(board.chat.checkpoint)}` : "За всё время"}</span></h1>
          <ReportTab board={board} onCopied={() => setToast({ id: Date.now(), text: "Отчёт скопирован — вставьте в чат" })} onOpen={(t) => setOpenedId(t.id)} />
        </>
      )}

      {view === "events" ? (
        <nav className="dock">
          <button className="dock__btn dock__btn--on press" onClick={() => { haptic.select(); setView("board"); window.scrollTo({ top: 0, behavior: "smooth" }); }}>
            <IconTasks size={20} /><span className="dock__label">Задачи</span>
          </button>
        </nav>
      ) : (
      <nav className="dock">
        {([
          ["tasks", "Задачи", <IconTasks size={20} />, 0],
          ["free", "Свободные", <IconHand size={20} />, board.open_items.length],
          ["report", "Отчёт", <IconChart size={20} />, 0],
        ] as [Tab, string, JSX.Element, number][]).map(([key, label, icon, badge]) => (
          <button key={key} className={`dock__btn press ${tab === key ? "dock__btn--on" : ""}`}
                  onClick={() => { if (tab !== key) { haptic.select(); setTab(key); window.scrollTo({ top: 0, behavior: "smooth" }); } }}>
            {icon}<span className="dock__label">{label}</span>
            {badge > 0 && <span className="dock__badge">{badge}</span>}
          </button>
        ))}
      </nav>
      )}

      {opened && !editing && (
        <TaskDetails ref={detailsRef} t={opened} board={board} onClose={() => setOpenedId(null)} onSave={save} onEdit={setEditing} onDelete={remove} canEdit={canEdit(board, opened)}
                     onDone={(t) => { detailsRef.current?.close(); window.setTimeout(() => complete(t), 280); }} />
      )}
      {editing && <EditSheet t={editing} members={board.members.map((m) => m.name)} onClose={() => setEditing(null)} onSave={save} />}
      <Toast toast={toast} onDone={clearToast} />
      {error && board && <div className="toast toast--plain" style={{ background: "var(--late)", color: "#fff" }}>{error}</div>}
    </div>
  );
}

