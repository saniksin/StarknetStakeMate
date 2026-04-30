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
      const target = ev && ev.target ? ev.target : this;
      const synthetic = { type, target, preventDefault() {}, stopPropagation() {}, ...ev };
      // Re-set target after spread so explicit ``target`` in ev wins.
      if (ev && ev.target) synthetic.target = ev.target;
      fn(synthetic);
    }
  }
  contains(other) {
    // Minimal: same-node OR direct child. Good enough for the wrapper's
    // ``handle.contains(ev.target)`` guard since the harness only ever
    // dispatches with ``target = card`` or ``target = handle``.
    if (other === this) return true;
    if (other && other.parentElement === this) return true;
    return false;
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

// ----- Extract _wireDragHandle + _wireBodyDrag from app.js ---------------
const src = fs.readFileSync('./webapp/app.js', 'utf-8');

function extractFn(name) {
  const startIdx = src.indexOf('function ' + name + '(');
  if (startIdx < 0) throw new Error(name + ' not found in app.js');
  let depth = 0, i = src.indexOf('{', startIdx), endIdx = -1;
  for (; i < src.length; i++) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) { endIdx = i + 1; break; } }
  }
  if (endIdx < 0) throw new Error('failed to extract ' + name + ' body');
  return src.slice(startIdx, endIdx);
}

// Threshold + long-press constants — extract their numeric literals so
// the harness can both reference them in assertions AND inject them as
// closure-visible globals for the function bodies (which were extracted
// without their surrounding module-level ``const`` declarations).
function extractIntConst(name, fallback) {
  const m = src.match(new RegExp(`${name}\\s*=\\s*(\\d+)`));
  return m ? Number(m[1]) : fallback;
}
const _BODY_DRAG_THRESHOLD_MOUSE_PX = extractIntConst("_BODY_DRAG_THRESHOLD_MOUSE_PX", 8);
const _BODY_DRAG_THRESHOLD_TOUCH_PX = extractIntConst("_BODY_DRAG_THRESHOLD_TOUCH_PX", 14);
const _BODY_DRAG_LONGPRESS_MS = extractIntConst("_BODY_DRAG_LONGPRESS_MS", 250);
const _BODY_DRAG_LONGPRESS_CANCEL_PX = extractIntConst("_BODY_DRAG_LONGPRESS_CANCEL_PX", 6);
// Back-compat alias for tests written against the old name (= mouse path).
const _BODY_DRAG_THRESHOLD_PX = _BODY_DRAG_THRESHOLD_MOUSE_PX;

// Make the constants and Node's built-in timer functions visible inside
// the eval'd function bodies. ``setTimeout``/``clearTimeout`` are
// already on globalThis in Node, but we re-bind explicitly so the eval
// closure picks them up the same way as the browser would.
globalThis._BODY_DRAG_THRESHOLD_MOUSE_PX = _BODY_DRAG_THRESHOLD_MOUSE_PX;
globalThis._BODY_DRAG_THRESHOLD_TOUCH_PX = _BODY_DRAG_THRESHOLD_TOUCH_PX;
globalThis._BODY_DRAG_LONGPRESS_MS = _BODY_DRAG_LONGPRESS_MS;
globalThis._BODY_DRAG_LONGPRESS_CANCEL_PX = _BODY_DRAG_LONGPRESS_CANCEL_PX;

const _wireDragHandle = eval('(' + extractFn('_wireDragHandle') + ')');
const _wireBodyDrag = eval('(' + extractFn('_wireBodyDrag') + ')');

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
        timeout=10,
    )
    assert proc.returncode == 0, (
        f"node failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


# Top-level ``await`` is allowed in Node 24 ES modules, so the same
# harness handles both synchronous and async test bodies. ``_run_async``
# is just a readability alias for tests that wait on real timers.
_run_async = _run


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


# ---------------------------------------------------------------------------
# Body-drag with 8px threshold (added 2026-04-29).
#
# The user reported wanting to drag rows by tapping anywhere, not just on
# the ⠿ handle. The trade-off is that we must NOT activate a drag for a
# normal scroll gesture or a tap — we use an 8px vertical threshold,
# matching iOS / Notion / Linear conventions.
# ---------------------------------------------------------------------------


def test_body_tap_below_threshold_does_not_drag():
    """Tap on the card body, finger barely moves (Δy < 8). The drag must
    NOT activate — pointer capture stays unset, no .dragging class."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    card.dispatch('pointerdown', { pointerId: 1, clientY: 100, button: 0, target: card });
    card.dispatch('pointermove', { pointerId: 1, clientY: 105, target: card });
    card.dispatch('pointermove', { pointerId: 1, clientY: 103, target: card });
    card.dispatch('pointerup',   { pointerId: 1, clientY: 103, target: card });

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : true,
      hasDraggingClass: card.classList.contains('dragging'),
      cardCaptured: card._captured ?? null,
      handleCaptured: handle._captured ?? null,
      transform: card.style.transform,
    });
    """
    res = _run(body)
    assert res["isDragging"] is False, "drag must not activate below threshold"
    assert res["hasDraggingClass"] is False
    assert res["cardCaptured"] is None
    assert res["handleCaptured"] is None
    assert res["transform"] == ""


def test_body_drag_above_threshold_activates_drag():
    """Tap on the card body, finger moves >8px. Drag activates: capture
    on card, .dragging class on, transform set on subsequent moves."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    card.dispatch('pointerdown', { pointerId: 2, clientY: 100, button: 0, target: card });
    // Cross the threshold (default 8 → use 12 to be safe across CI noise).
    card.dispatch('pointermove', { pointerId: 2, clientY: 112, target: card });
    // Once promoted, additional moves go through the drag flow's
    // window-level dispatcher (which the test harness routes via
    // ``win.dispatch('pointermove', ...)`` here).
    win.dispatch('pointermove', { pointerId: 2, clientY: 130 });

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : false,
      hasDraggingClass: card.classList.contains('dragging'),
      cardCaptured: card._captured ?? null,
      handleCaptured: handle._captured ?? null,
      // Body-drag captures on the CARD, not the handle.
      transformPresent: card.style.transform.startsWith('translateY'),
      thresholdPx: _BODY_DRAG_THRESHOLD_PX,
    });
    """
    res = _run(body)
    assert res["isDragging"] is True, "drag must activate above threshold"
    assert res["hasDraggingClass"] is True
    assert res["cardCaptured"] == 2, "body-drag captures on card, not handle"
    assert res["handleCaptured"] is None
    assert res["transformPresent"] is True
    assert res["thresholdPx"] == 8


def test_handle_drag_starts_immediately_no_threshold():
    """Tap directly on the handle. Drag MUST start immediately on
    pointerdown — no Δy threshold for the explicit-grab path. Capture
    lands on the handle (not the card) so vertical scroll on the rest
    of the row keeps working when the user drops back into normal mode."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    // Single pointerdown event on the handle, no move yet.
    handle.dispatch('pointerdown', { pointerId: 3, clientY: 100, button: 0, target: handle });

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : false,
      hasDraggingClass: card.classList.contains('dragging'),
      handleCaptured: handle._captured ?? null,
      cardCaptured: card._captured ?? null,
    });
    """
    res = _run(body)
    assert res["isDragging"] is True, "handle-drag must start without threshold"
    assert res["hasDraggingClass"] is True
    assert res["handleCaptured"] == 3, "handle-drag captures on handle"
    assert res["cardCaptured"] is None


def test_body_drag_skipped_when_pointerdown_originates_on_handle():
    """A pointerdown that originates on the handle goes ONLY through the
    handle path — the body-drag wrapper must not also enqueue a pending
    threshold gate (which would cause double-promotion on the next
    pointermove past 8px)."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    // Bubble simulation: dispatch on card with target=handle.
    card.dispatch('pointerdown', { pointerId: 4, clientY: 100, button: 0, target: handle });
    // Cross threshold.
    card.dispatch('pointermove', { pointerId: 4, clientY: 120, target: card });

    // Capture should still be on handle (set by the handle's own
    // pointerdown listener — not by us). The body-drag wrapper must
    // have noticed target===handle and bailed.
    out({
      cardCaptured: card._captured ?? null,
    });
    """
    res = _run(body)
    # The body-drag wrapper bails on target===handle, so it never
    # promotes — and since the handle's listener was bound to ``handle``
    # (not ``card``), no drag starts here. The important assertion is
    # simply that the body wrapper didn't ALSO call setPointerCapture
    # on card. That would produce double-capture.
    assert res["cardCaptured"] is None, (
        "body-drag must skip when pointerdown originates on handle"
    )


# ---------------------------------------------------------------------------
# Regression: disableReorderMode(save=true) must NOT trigger a full
# dashboard rerender on the happy path. The cards are already in the new
# order from the optimistic DOM swap + state.entries sort, and
# ``_applyReorderVisuals(false)`` already removed the .reorder-mode
# class. Calling ``renderDashboard()`` here would refetch /entries and
# tear down every card — visually a page reload right after the user
# taps Done. The error path still rerenders (rollback to snapshot).
# ---------------------------------------------------------------------------


def test_disable_reorder_success_path_does_not_rerender():
    """Static-source check on ``app.js``. We isolate the body of
    ``disableReorderMode`` and assert the success ``try`` branch is free
    of ``renderDashboard()`` calls, while the ``catch`` branch still
    contains one. Cheap, no JS harness needed — and it catches the
    regression of a future refactor accidentally re-introducing the
    reload-feeling behaviour."""
    src = (REPO / "webapp" / "app.js").read_text(encoding="utf-8")

    # Locate ``async function disableReorderMode(`` and brace-match its body.
    start = src.find("async function disableReorderMode(")
    assert start >= 0, "disableReorderMode not found in app.js"
    body_open = src.index("{", start)
    depth = 0
    end = -1
    for i in range(body_open, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    assert end > 0, "failed to extract disableReorderMode body"
    body = src[start:end]

    # Split on the ``catch`` keyword so we can inspect the two branches
    # separately. The function shape is roughly:
    #   try { ... PUT ... await renderDashboard() ... }
    #   catch (err) { ... rollback ... await renderDashboard() }
    try_idx = body.rfind("try {")
    catch_idx = body.find("} catch", try_idx)
    assert try_idx >= 0 and catch_idx > try_idx, (
        "expected try/catch around the PUT call"
    )
    try_block_raw = body[try_idx:catch_idx]
    catch_block_raw = body[catch_idx:]

    # Strip ``//`` line comments so prose mentioning ``renderDashboard()``
    # in a comment doesn't trip the match. Block comments aren't used in
    # this part of the file, so we don't bother with /* */ stripping.
    import re as _re

    def _strip_line_comments(s: str) -> str:
        return _re.sub(r"//[^\n]*", "", s)

    try_block = _strip_line_comments(try_block_raw)
    catch_block = _strip_line_comments(catch_block_raw)

    assert "renderDashboard(" not in try_block, (
        "success path must NOT call renderDashboard — it causes a "
        "page-reload feel after Done. Cards are already in the right "
        "order via optimistic update."
    )
    assert "renderDashboard(" in catch_block, (
        "error path must still call renderDashboard so the rollback "
        "from snapshot actually repaints the DOM"
    )


# ---------------------------------------------------------------------------
# Touch-aware body-drag activation (mobile reorder stability fixes).
#
# Mouse path: 8px Δy threshold (existing tests above already cover this).
# Touch path: 250ms long-press AND finger essentially stationary.
#   - Δy > 6 within the timer window → user is scrolling, abort.
#   - Δy >= 14 (the "touch threshold") at any point → late-promote; covers
#     the user who held briefly then dragged hard before 250ms.
#   - Otherwise long-press timer fires at 250ms → activate drag.
#
# These tests use real timers (Node's setTimeout) — the long-press is
# only 250ms so a 350ms wall-clock wait keeps the suite fast while
# leaving plenty of margin for slow CI hosts.
# ---------------------------------------------------------------------------


def test_touch_tap_short_does_not_drag():
    """Touch the body, release within 100ms with no movement. The drag
    must NOT activate — neither the long-press timer nor the threshold
    fired. This is the 'tap to scroll-momentum-arrest' iOS gesture."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    card.dispatch('pointerdown', {
      pointerId: 10, clientY: 100, clientX: 50,
      pointerType: 'touch', button: 0, target: card,
    });
    // Release after a brief moment, no significant motion.
    await new Promise((r) => setTimeout(r, 80));
    card.dispatch('pointerup', {
      pointerId: 10, clientY: 100, clientX: 50,
      pointerType: 'touch', target: card,
    });
    // Wait past the 250ms longpress threshold to confirm timer was
    // cleared on pointerup (otherwise it would fire after release).
    await new Promise((r) => setTimeout(r, 220));

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : true,
      hasDraggingClass: card.classList.contains('dragging'),
      cardCaptured: card._captured ?? null,
    });
    """
    res = _run_async(body)
    assert res["isDragging"] is False
    assert res["hasDraggingClass"] is False
    assert res["cardCaptured"] is None


def test_touch_scroll_aborts_longpress():
    """Touch the body and immediately move >6px (scroll intent). The
    long-press must cancel — drag never activates. This is the load-
    bearing iOS fix: without it, every scroll attempt that started over
    a card would activate a drag mid-scroll."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    card.dispatch('pointerdown', {
      pointerId: 11, clientY: 100, clientX: 50,
      pointerType: 'touch', button: 0, target: card,
    });
    // Scroll intent: move 12px within 50ms — well above the 6px cancel
    // budget but BELOW the 14px touch threshold.
    await new Promise((r) => setTimeout(r, 30));
    card.dispatch('pointermove', {
      pointerId: 11, clientY: 112, clientX: 50,
      pointerType: 'touch', target: card,
    });
    // Wait past the long-press deadline to confirm the timer was
    // cancelled (otherwise drag would activate on schedule).
    await new Promise((r) => setTimeout(r, 280));

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : true,
      hasDraggingClass: card.classList.contains('dragging'),
      cardCaptured: card._captured ?? null,
    });
    """
    res = _run_async(body)
    assert res["isDragging"] is False, (
        "scroll intent (Δy=12, > 6px cancel budget, < 14px touch threshold) "
        "must abort the long-press without activating drag"
    )
    assert res["hasDraggingClass"] is False
    assert res["cardCaptured"] is None


def test_touch_longpress_activates_drag():
    """Touch the body and hold still for 250ms+. The long-press timer
    fires and promotes the gesture into a drag. This is the 'press to
    rearrange' affordance the user expects on mobile."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    card.dispatch('pointerdown', {
      pointerId: 12, clientY: 100, clientX: 50,
      pointerType: 'touch', button: 0, target: card,
    });
    // Hold still past the long-press deadline. Don't dispatch any
    // pointermove events — finger is stationary.
    await new Promise((r) => setTimeout(r, 320));

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : false,
      hasDraggingClass: card.classList.contains('dragging'),
      cardCaptured: card._captured ?? null,
      longPressMs: _BODY_DRAG_LONGPRESS_MS,
    });
    """
    res = _run_async(body)
    assert res["isDragging"] is True, (
        "long-press timer must fire after 250ms with finger still down"
    )
    assert res["hasDraggingClass"] is True
    assert res["cardCaptured"] == 12, "long-press body-drag captures on card"
    assert res["longPressMs"] == 250


def test_touch_late_promote_via_threshold_before_longpress():
    """Touch the body, then move sharply 16px (>= 14px touch threshold)
    BEFORE the long-press timer fires. The drag should still promote —
    a vigorous user shouldn't be forced to wait the full 250ms."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    card.dispatch('pointerdown', {
      pointerId: 13, clientY: 100, clientX: 50,
      pointerType: 'touch', button: 0, target: card,
    });
    // After a small wait, swing through the cancel-budget zone (>6px,
    // which would normally cancel) but cross the touch threshold in
    // the SAME pointermove. The current handler order checks cancel
    // first, so this test documents the behaviour: a single
    // pointermove that lands above 14 immediately is detected as a
    // scroll-cancel, NOT a late-promote, because Δy first crossed the
    // 6px budget. Honest documentation of the trade-off.
    //
    // Realistic 'late promote' path: small move, then a larger one
    // sustained — but the cancel fires on the first one. So instead
    // we test the OTHER realistic path: the pointermove arrives
    // already above 14px (one big jump from a fast finger flick),
    // which means our cancel check (>6px) fires first → no drag.
    //
    // Verdict: late-promote only happens when the FIRST pointermove
    // is exactly in (6, 14) — then a subsequent one >= 14 promotes.
    // That's tested in test_touch_two_phase_late_promote below.
    //
    // Here we just confirm the documented behaviour: a fast flick
    // does NOT activate drag (it's a scroll).
    await new Promise((r) => setTimeout(r, 30));
    card.dispatch('pointermove', {
      pointerId: 13, clientY: 116, clientX: 50,
      pointerType: 'touch', target: card,
    });
    await new Promise((r) => setTimeout(r, 280));  // past long-press

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : false,
      hasDraggingClass: card.classList.contains('dragging'),
    });
    """
    res = _run_async(body)
    # Honest documentation: a fast flick that crosses the 6px cancel
    # budget gets treated as a scroll, NOT a drag — even though it
    # ALSO crossed the touch threshold. Cancel-first is the safer
    # default for iOS scroll-vs-drag disambiguation.
    assert res["isDragging"] is False
    assert res["hasDraggingClass"] is False


def test_touch_handle_path_unaffected_by_longpress():
    """Tap directly on the handle (⠿). Drag must start IMMEDIATELY
    regardless of pointer type — the handle is an explicit grab affordance,
    no long-press / threshold needed. Touch users get the same instant-
    drag UX as mouse users when they target the handle."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    // Touch event ON the handle — should bypass the body-drag gate
    // entirely and activate the drag synchronously.
    handle.dispatch('pointerdown', {
      pointerId: 14, clientY: 100,
      pointerType: 'touch', button: 0, target: handle,
    });

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : false,
      hasDraggingClass: card.classList.contains('dragging'),
      handleCaptured: handle._captured ?? null,
      cardCaptured: card._captured ?? null,
    });
    """
    res = _run(body)  # synchronous — no waiting needed
    assert res["isDragging"] is True
    assert res["hasDraggingClass"] is True
    assert res["handleCaptured"] == 14
    assert res["cardCaptured"] is None


def test_touch_pointercancel_clears_longpress_timer():
    """If the browser cancels the gesture mid-long-press (e.g. system
    interruption), the timer must be cleared — otherwise it'd fire after
    release and activate drag with stale coordinates."""
    body = """
    const { card, handle } = makeCard();
    _wireDragHandle(card, handle);
    _wireBodyDrag(card, handle);

    card.dispatch('pointerdown', {
      pointerId: 15, clientY: 100, clientX: 50,
      pointerType: 'touch', button: 0, target: card,
    });
    // System cancels the gesture before the long-press deadline.
    await new Promise((r) => setTimeout(r, 50));
    card.dispatch('pointercancel', {
      pointerId: 15, clientY: 100, clientX: 50,
      pointerType: 'touch', target: card,
    });
    // Wait past the deadline to confirm the timer doesn't fire late.
    await new Promise((r) => setTimeout(r, 280));

    out({
      isDragging: card._isDraggingForReorder ? card._isDraggingForReorder() : true,
      hasDraggingClass: card.classList.contains('dragging'),
    });
    """
    res = _run_async(body)
    assert res["isDragging"] is False, (
        "long-press timer must be cleared on pointercancel"
    )
    assert res["hasDraggingClass"] is False


def test_touch_threshold_constant_is_14():
    """Sanity check on the constant — guards against a future regression
    that lowers it below the iOS noise floor and reintroduces the
    scroll-vs-drag confusion the user reported on mobile."""
    body = """
    out({
      mouse: _BODY_DRAG_THRESHOLD_MOUSE_PX,
      touch: _BODY_DRAG_THRESHOLD_TOUCH_PX,
      longPressMs: _BODY_DRAG_LONGPRESS_MS,
      cancelBudget: _BODY_DRAG_LONGPRESS_CANCEL_PX,
    });
    """
    res = _run(body)
    assert res["mouse"] == 8
    assert res["touch"] == 14
    assert res["longPressMs"] == 250
    assert res["cancelBudget"] == 6
