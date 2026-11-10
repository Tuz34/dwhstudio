import { useEffect, useId, useRef } from 'react';
import { FaTimes } from 'react-icons/fa';
import { modalFocusableElements, shouldDismissModal, trapModalFocus } from './modalFocus';

export function Modal({ open, onClose, title, children, size = 'md', closeDisabled = false }) {
  const dialogRef = useRef(null);
  const previousFocusRef = useRef(null);
  const titleId = useId();
  const closeDisabledRef = useRef(closeDisabled);
  const allowDismiss = shouldDismissModal(closeDisabled);

  useEffect(() => {
    closeDisabledRef.current = closeDisabled;
  }, [closeDisabled]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement;
    const dialog = dialogRef.current;
    const firstFocusable = modalFocusableElements(dialog)[0];
    (firstFocusable || dialog)?.focus();

    const handler = (e) => {
      if (e.key === 'Escape' && shouldDismissModal(closeDisabledRef.current)) onClose();
      if (e.key === 'Tab') trapModalFocus(e, dialogRef.current);
    };
    window.addEventListener('keydown', handler);
    return () => {
      window.removeEventListener('keydown', handler);
      previousFocusRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  const sizeClass = {
    sm: 'max-w-md',
    md: 'max-w-2xl',
    lg: 'max-w-4xl',
    xl: 'max-w-6xl',
  }[size] || 'max-w-2xl';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/30 backdrop-blur-sm animate-fade-in"
      onClick={(e) => {
        if (e.target !== e.currentTarget || !allowDismiss) return;
        onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={`glass-card w-full ${sizeClass} max-h-[90vh] flex flex-col overflow-hidden`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200/60">
          <h2 id={titleId} className="text-base font-semibold text-slate-800">{title}</h2>
          <button
            type="button"
            aria-label="Close dialog"
            disabled={!allowDismiss}
            onClick={() => {
              if (!allowDismiss) return;
              onClose();
            }}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors disabled:opacity-40 disabled:pointer-events-none"
          >
            <FaTimes aria-hidden="true" />
          </button>
        </div>
        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {children}
        </div>
      </div>
    </div>
  );
}
