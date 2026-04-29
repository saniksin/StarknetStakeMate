"""Regression tests for the Mini-App reorder drag-and-drop cleanup.

The original handler cleared ``transform`` / ``.dragging`` only when
``pointerup`` fired on the ``handle`` element with a matching pointerId.
On iOS WebView the DOM swap during ``pointermove`` causes the browser
to drop pointer capture silently — the canonical ``pointerup`` then
fires on the original element under the finger (not on ``handle``),
the listener never matches, and the card stays "stuck" in its lifted
state. The fix routes terminal events through window-level fallbacks
+ ``lostpointercapture`` + ``touchend``/``touchcancel`` and an
idempotent ``cleanup()``.

These tests boot a tiny synthetic DOM in Node and exercise:
  - happy path: pointerdown → pointermove → pointerup cleans up.
  - lost-capture path: pointerdown → pointermove → lostpointercapture
    cleans up even when no pointerup ever arrives.
  - pointercancel path: same, via the cancel terminal event.
  - touchend fallback: window-level touchend cleans up when neither
    pointerup nor lostpointercapture fire (older Telegram WebViews).
  - cleanup is idempotent: a second terminal event after a successful
    clean-up is a no-op (no double release / no exceptions).
  - transform reset: after cleanup, ``card.style.transform`` is empty
    (the load-bearing visual reset).
  - force-flush: ``card._reorderCleanup()`` works from outside the
    handler (used by ``disableReorderMode`` when the user taps Done
    mid-gesture).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent


def _node_or_skip() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; skipping JS render tests")
    return node


# Tiny synthetic DOM that supplies just enough surface for the drag
# handler. Every assertion the handler makes against the DOM (event
# listener wiring, getBoundingClientRect, classList, dataset, style,
# parent insertBefore, querySelectorAll on the parent) is covered.
# We wire the script as text-replacement inside ``__BODY__`` so the
# per-test invocation can pick which fault-injection to run.
_HARNESS = r"""
import fs from 'node:fs';

// ----- Synthetic DOM nodes ------------------------------------------------
class FakeClassList {
  constructor() { this._set = new Set(); }
  add(...c) { for (const x of c) this._set.add(x); }
  remove(...c) { for (const x of c) this._set.delete(x); }
  contains(c) { return this._set.has(c); }
  toggle(c, on) {
    if (on === true) { this._set.add(c); return true; }
    if (on === false) { this._set.delete(c); return false; }
    if (this._set.has(c)) { this._set.delete(c); return false; }
    this._set.add(c); return true;
  }
  toString() { return Array.from(this._set).join(' '); }
}

class FakeStyle {
  constructor() { this.transform = ''; this.zIndex = ''; }
}

class FakeNode {
  constructor() {
    this.classList = new FakeClassList();
    this.style = new FakeStyle();
    this.dataset = {};
    this.parentElement = null;
    this.children = [];
    this._listeners = {};
  }
  addEventListener(type, fn, opts) {
    (this._listeners[type] ??= []).push({ fn, opts });
  }
  removeEventListener(type, fn) {
    if (!this._listeners[type]) return;
    this._listeners[type] = this._listeners[type].filter((l) => l.fn !== fn);
  }
  dispatch(type, ev) {
    for (const { fn } of (this._listeners[type] ?? [])) {
      fn({ type, target: this, preventDefault() {}, stopPropagation() {}, ...ev });
    }
  }
  setPointerCapture(_id) { this._captured = _id; }
  releasePointerCapture(id) {
    if (this._captured !== id) {
      const e = new Error('InvalidPointerId');
      e.name = 'InvalidStateError';
      throw e;
    }
    this._captured = null;
  }
  getBoundingClientRect() {
    return { top: this._top ?? 0, height: 60, bottom: (this._top ?? 0) + 60 };
  }
  querySelectorAll(_sel) {
    // Card harness keeps siblings in ``this._siblings`` for tests.
    return this._siblings ?? [];
  }
  insertBefore(node, ref) {
    // Naive: just record that it was called — the unit tests don't
    // care about the exact tree, only that the handler doesn't crash
    // and that cleanup runs after a swap.
    this._inserts ??= [];
    this._inserts.push({ node, ref });
  }
  compareDocumentPosition(_other) { return 0; }
}

// Window-level event bus (same shape as DOM nodes — addEventListener,
// dispatch with a target). Captures listeners with their ``capture``
// option so we can model whether the handler chose ``capture: true``.
const win = new FakeNode();

// ----- Globals the production code expects --------------------------------
globalThis.window = win;
globalThis.document = { getElementById: () => null };
globalThis.Node = { DOCUMENT_POSITION_FOLLOWING: 4, DOCUMENT_POSITION_PRECEDING: 2 };
globalThis.tg = null;
const viewEl = new FakeNode();
viewEl.querySelectorAll = () => [];
globalThis.viewEl = viewEl;

// Stub state — only ``reorderMode`` is read by the handler.
const state = { reorderMode: true };
globalThis.state = state;

// Production helpers stub out (we only execute _wireDragHandle below).
globalThis.t = (k, fb) => fb ?? k;
globalThis.toast = () => {};

// ----- Extract _wireDragHandle from app.js --------------------------------
const src = fs.readFileSync('./webapp/app.js', 'utf-8');
const startIdx = src.indexOf('function _wireDragHandle(');
let depth = 0, i = src.indexOf('{', startIdx), endIdx = -1;
for (; i < src.length; i++) {
  const ch = src[i];
  if (ch === '{') depth++;
  else if (ch === '}') { depth--; if (depth === 0) { endIdx = i + 1; break; } }
}
if (endIdx < 0) throw new Error('failed to extract _wireDragHandle body');
const fnSrc = src.slice(startIdx, endIdx);
const _wireDragHandle = eval('(' + fnSrc + ')');

// ----- Build a card + handle pair and run the test ------------------------
function makeCard() {
  const card = new FakeNode();
  card.dataset.kind = 'validator';
  card.dataset.address = '0x' + 'a'.repeat(63);
  card._top = 100;
  const handle = new FakeNode();
  handle.parentElement = card;
  card.parentElement = viewEl;
  return { card, handle };
}

const out = (obj) => process.stdout.write(JSON.stringify(obj));

/* __BODY__ */
__BODY__
"""


def _run(body: str) -> dict:
    script = _HARNESS.replace("__BODY__", body)
    node = _node_or_skip()
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env={**os.environ},
    )
    assert proc.returncode == 0, (
        f"node failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# 1. Happy path — pointerdown → pointermove → pointerup cleans up.
# ---------------------------------------------------------------------------

def test_happy_path_cleanup_on_pointerup():
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);

    handle.dispatch('pointerdown', { pointerId: 1, clientY: 100, button: 0 });
    handle.dispatch('pointermove', { pointerId: 1, clientY: 130 });
    handle.dispatch('pointerup',   { pointerId: 1, clientY: 130 });

    out({
      transform: card.style.transform,
      zIndex: card.style.zIndex,
      hasDragging: card.classList.contains('dragging'),
      pointerCaptured: handle._captured,
    });
    """
    res = _run(body)
    assert res["transform"] == ""
    assert res["zIndex"] == ""
    assert res["hasDragging"] is False
    assert res["pointerCaptured"] is None


# ---------------------------------------------------------------------------
# 2. The actual stuck-card bug: pointerup never arrives on handle, only
#    ``lostpointercapture`` does (DOM mutation invalidates iOS capture).
#    The original code left the card stuck. The fix must clean up.
# ---------------------------------------------------------------------------

def test_lost_pointer_capture_cleans_up():
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);

    handle.dispatch('pointerdown', { pointerId: 1, clientY: 100, button: 0 });
    handle.dispatch('pointermove', { pointerId: 1, clientY: 130 });
    // No pointerup. Browser drops capture silently due to DOM swap.
    handle.dispatch('lostpointercapture', { pointerId: 1 });

    out({
      transform: card.style.transform,
      hasDragging: card.classList.contains('dragging'),
    });
    """
    res = _run(body)
    assert res["transform"] == "", (
        "transform should reset on lostpointercapture (this is the bug fix)"
    )
    assert res["hasDragging"] is False


# ---------------------------------------------------------------------------
# 3. pointercancel terminal event also cleans up.
# ---------------------------------------------------------------------------

def test_pointercancel_cleans_up():
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);

    handle.dispatch('pointerdown',   { pointerId: 1, clientY: 100, button: 0 });
    handle.dispatch('pointermove',   { pointerId: 1, clientY: 130 });
    handle.dispatch('pointercancel', { pointerId: 1, clientY: 130 });

    out({
      transform: card.style.transform,
      hasDragging: card.classList.contains('dragging'),
    });
    """
    res = _run(body)
    assert res["transform"] == ""
    assert res["hasDragging"] is False


# ---------------------------------------------------------------------------
# 4. Window-level pointerup fallback. Older Telegram WebViews route the
#    event to ``window`` after capture is lost; the handle's own
#    listener never sees it.
# ---------------------------------------------------------------------------

def test_window_pointerup_fallback_cleans_up():
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);

    handle.dispatch('pointerdown', { pointerId: 1, clientY: 100, button: 0 });
    handle.dispatch('pointermove', { pointerId: 1, clientY: 130 });
    // Terminal event arrives at window, NOT at handle — this is the
    // observed iOS WebView path post-DOM-mutation.
    win.dispatch('pointerup', { pointerId: 1, clientY: 130 });

    out({
      transform: card.style.transform,
      hasDragging: card.classList.contains('dragging'),
    });
    """
    res = _run(body)
    assert res["transform"] == ""
    assert res["hasDragging"] is False


# ---------------------------------------------------------------------------
# 5. Touch fallback — old Android WebViews emit only touch events, no
#    pointer events. ``touchend`` on window must still flush the drag.
# ---------------------------------------------------------------------------

def test_window_touchend_fallback_cleans_up():
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);

    handle.dispatch('pointerdown', { pointerId: 1, clientY: 100, button: 0 });
    handle.dispatch('pointermove', { pointerId: 1, clientY: 130 });
    // No pointerup, no lostpointercapture. Only touchend is emitted.
    win.dispatch('touchend', { changedTouches: [{ clientY: 130 }] });

    out({
      transform: card.style.transform,
      hasDragging: card.classList.contains('dragging'),
    });
    """
    res = _run(body)
    assert res["transform"] == ""
    assert res["hasDragging"] is False


# ---------------------------------------------------------------------------
# 6. cleanup() is idempotent — a redundant terminal event after a
#    successful pointerup is a no-op (no double release, no throws).
# ---------------------------------------------------------------------------

def test_cleanup_is_idempotent():
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);

    handle.dispatch('pointerdown', { pointerId: 1, clientY: 100, button: 0 });
    handle.dispatch('pointerup',   { pointerId: 1, clientY: 100 });
    // After a clean exit, a stray lostpointercapture (which can happen
    // when the browser belatedly notices the DOM mutation) must be a
    // no-op. The release call would throw without the swallow.
    let err = null;
    try {
      handle.dispatch('lostpointercapture', { pointerId: 1 });
      win.dispatch('pointerup', { pointerId: 1 });
    } catch (e) { err = String(e); }

    out({
      transform: card.style.transform,
      hasDragging: card.classList.contains('dragging'),
      err,
    });
    """
    res = _run(body)
    assert res["err"] is None
    assert res["transform"] == ""
    assert res["hasDragging"] is False


# ---------------------------------------------------------------------------
# 7. Force-flush from outside (disableReorderMode → card._reorderCleanup).
#    User taps Done while still holding a card — the controller calls
#    the per-card cleanup directly to flush state synchronously before
#    the rerender. This must work even mid-drag.
# ---------------------------------------------------------------------------

def test_force_flush_from_disable_reorder():
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);

    handle.dispatch('pointerdown', { pointerId: 1, clientY: 100, button: 0 });
    handle.dispatch('pointermove', { pointerId: 1, clientY: 130 });
    // Mid-drag, controller invokes the exposed hook (this is what
    // ``disableReorderMode`` does for every .row-card before rerender).
    card._reorderCleanup();

    out({
      transform: card.style.transform,
      hasDragging: card.classList.contains('dragging'),
      hookExposed: typeof card._reorderCleanup === 'function',
    });
    """
    res = _run(body)
    assert res["hookExposed"] is True
    assert res["transform"] == ""
    assert res["hasDragging"] is False


# ---------------------------------------------------------------------------
# 8. Window-level listeners are removed after cleanup so they don't
#    leak across drags. A future drag's pointerup must not be matched by
#    a stale listener from a previous drag.
# ---------------------------------------------------------------------------

def test_window_listeners_detached_after_cleanup():
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);

    handle.dispatch('pointerdown', { pointerId: 1, clientY: 100, button: 0 });
    const duringDrag = (win._listeners['pointerup'] ?? []).length;
    handle.dispatch('pointerup',   { pointerId: 1, clientY: 100 });
    const afterCleanup = (win._listeners['pointerup'] ?? []).length;

    out({ duringDrag, afterCleanup });
    """
    res = _run(body)
    assert res["duringDrag"] >= 1, "should bind window pointerup during drag"
    assert res["afterCleanup"] == 0, "must detach window listeners after cleanup"
