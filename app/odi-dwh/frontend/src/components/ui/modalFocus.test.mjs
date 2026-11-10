import assert from 'node:assert/strict';
import test from 'node:test';

import { shouldDismissModal, trapModalFocus } from './modalFocus.js';


function focusable(name) {
  return {
    name,
    disabled: false,
    focus() { globalThis.document.activeElement = this; },
  };
}


test('modal focus wraps forward and backward', () => {
  const first = focusable('first');
  const last = focusable('last');
  const container = {
    querySelectorAll: () => [first, last],
    contains: element => element === first || element === last,
  };
  globalThis.document = { activeElement: last };
  let prevented = false;

  trapModalFocus({ key: 'Tab', shiftKey: false, preventDefault: () => { prevented = true; } }, container);
  assert.equal(globalThis.document.activeElement, first);
  assert.equal(prevented, true);

  globalThis.document.activeElement = first;
  prevented = false;
  trapModalFocus({ key: 'Tab', shiftKey: true, preventDefault: () => { prevented = true; } }, container);
  assert.equal(globalThis.document.activeElement, last);
  assert.equal(prevented, true);
});


test('modal dismiss stays enabled unless closeDisabled is set', () => {
  assert.equal(shouldDismissModal(), true);
  assert.equal(shouldDismissModal(undefined), true);
  assert.equal(shouldDismissModal(false), true);
  assert.equal(shouldDismissModal(true), false);
});
