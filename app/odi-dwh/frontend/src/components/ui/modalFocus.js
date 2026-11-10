const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export function modalFocusableElements(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
    element => !element.disabled && element.getAttribute?.('aria-hidden') !== 'true',
  );
}

/** Omitted or false keeps Escape, the backdrop, and the close button active. */
export function shouldDismissModal(closeDisabled = false) {
  return closeDisabled !== true;
}

export function trapModalFocus(event, container) {
  if (event.key !== 'Tab' || !container) return false;
  const elements = modalFocusableElements(container);
  if (elements.length === 0) {
    event.preventDefault();
    container.focus?.();
    return true;
  }

  const first = elements[0];
  const last = elements[elements.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !container.contains(active))) {
    event.preventDefault();
    last.focus();
    return true;
  }
  if (!event.shiftKey && (active === last || !container.contains(active))) {
    event.preventDefault();
    first.focus();
    return true;
  }
  return false;
}
