import { ReactNode, useEffect, useImperativeHandle, useRef, useState, forwardRef } from "react";
import { colorFor, initials } from "./format";

export function Avatar({ name, size = "", className = "" }: { name: string; size?: "" | "xs" | "sm" | "lg"; className?: string }) {
  const bg = colorFor(name);
  return (
    <span className={`avatar ${size ? `avatar--${size}` : ""} ${className}`}
          style={{ backgroundColor: bg, backgroundImage: "linear-gradient(145deg, rgba(255,255,255,.22), rgba(0,0,0,.28))" }}>
      {initials(name)}
    </span>
  );
}

export type SheetHandle = { close: () => void };

/** Шторка снизу: появляется пружиной, закрывается плавно (по тапу на фон, «Назад» или кнопке). */
export const Sheet = forwardRef<SheetHandle, { onClose: () => void; children: ReactNode; bar?: ReactNode; label: string }>(
  function Sheet({ onClose, children, bar, label }, ref) {
    const [closing, setClosing] = useState(false);
    const close = () => {
      if (closing) return;
      setClosing(true);
      window.setTimeout(onClose, 260);
    };
    useImperativeHandle(ref, () => ({ close }));
    useEffect(() => {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => { document.body.style.overflow = prev; };
    }, []);
    return (
      <>
        <div className={`sheet-back ${closing ? "sheet-back--closing" : ""}`} onClick={close} />
        <section className={`sheet ${closing ? "sheet--closing" : ""}`} role="dialog" aria-label={label}>
          <div className="sheet__handle" />
          <div className="sheet__scroll">{children}</div>
          {bar && <div className="sheet__bar">{bar}</div>}
        </section>
      </>
    );
  },
);

export type ToastState = { id: number; text: string; undo?: () => void } | null;

export function Toast({ toast, onDone }: { toast: ToastState; onDone: () => void }) {
  const timer = useRef<number>();
  useEffect(() => {
    if (!toast) return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(onDone, toast.undo ? 4500 : 2200);
    return () => window.clearTimeout(timer.current);
  }, [toast, onDone]);
  if (!toast) return null;
  return (
    <div key={toast.id} className={`toast ${toast.undo ? "" : "toast--plain"}`} role="status">
      <span className="toast__text">{toast.text}</span>
      {toast.undo && (
        <button className="toast__btn press" onClick={() => { toast.undo?.(); onDone(); }}>Отменить</button>
      )}
    </div>
  );
}

export const plural = (n: number, one: string, few: string, many: string) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
};
