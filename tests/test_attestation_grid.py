"""Render-level tests for ``renderAttestationDetails`` in the Mini App.

The function lives in plain JS (``webapp/app.js``); rather than port it
to Python or pull in JSDOM, we boot a headless Node and call the
function with a few synthetic ``att`` / ``timeline`` shapes. Locale
bundles are loaded the same way the Mini App loads them at runtime, so
the plural "блок / блока / блоков" case actually exercises ``tN``.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
APP_JS = REPO / "webapp" / "app.js"
LOCALES = REPO / "locales"


def _node_or_skip() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; skipping JS render tests")
    return node


def _run_node(script: str) -> str:
    node = _node_or_skip()
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node exited {proc.returncode}\n"
            f"---- stdout ----\n{proc.stdout}\n"
            f"---- stderr ----\n{proc.stderr}\n"
        )
    return proc.stdout


# We can't import ``app.js`` directly — it expects ``window`` /
# ``document`` / Telegram globals. Instead, the Node script below
# extracts the exact source of ``renderAttestationDetails`` (and the
# helpers it depends on) by ``eval``-ing them in a sandbox where the
# DOM/network globals are stubbed.
#
# Pulling out only the renderer keeps the test fast and avoids running
# the boot sequence that triggers the initial fetch.
_HARNESS = r"""
import fs from 'node:fs';

// Locale bundle for the test (defaults to en, override via env).
const lang = process.env.STAKEMATE_TEST_LANG || 'en';
const bundle = JSON.parse(fs.readFileSync(`./locales/${lang}.json`, 'utf-8'));

// Minimal stubs the extracted helpers need.
const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
));
const fmtBlock = (n) => Number(n).toLocaleString('en-US').replace(/,/g, '_');
const t = (key, fallback, vars) => {
  let v = bundle[key];
  if (v === undefined || v === null || v === '') v = fallback ?? key;
  if (vars) for (const [k, val] of Object.entries(vars)) v = v.replaceAll(`{${k}}`, String(val));
  return v;
};
const SUPPORTED = ['en','ru','ua','de','es','zh','ko','pl'];
const currentLang = () => lang;

// Plural rules: replicate the same Intl.PluralRules path used by the
// Mini App's ``pluralCategory`` so the test matches production behaviour.
const pluralCategory = (n, locale) => {
  try {
    return new Intl.PluralRules(locale).select(n);
  } catch (_) {
    return n === 1 ? 'one' : 'other';
  }
};
const tN = (keyBase, count, locale, fallback, extraVars) => {
  const cat = pluralCategory(count, locale);
  const v = bundle[`${keyBase}_${cat}`] ?? bundle[`${keyBase}_other`] ?? bundle[keyBase] ?? fallback ?? `${keyBase}: ${count}`;
  let out = String(v).replaceAll('{n}', String(count)).replaceAll('{count}', String(count));
  if (extraVars) for (const [k, val] of Object.entries(extraVars)) out = out.replaceAll(`{${k}}`, String(val));
  return out;
};

// Extract ``function renderAttestationDetails`` source from app.js by
// matching the brace-balanced body. Then eval the source so it picks up
// the helpers we just defined as closures.
const src = fs.readFileSync('./webapp/app.js', 'utf-8');
const startIdx = src.indexOf('function renderAttestationDetails(');
if (startIdx < 0) throw new Error('renderAttestationDetails not found');
let depth = 0, i = src.indexOf('{', startIdx), endIdx = -1;
for (; i < src.length; i++) {
  const ch = src[i];
  if (ch === '{') depth++;
  else if (ch === '}') { depth--; if (depth === 0) { endIdx = i + 1; break; } }
}
if (endIdx < 0) throw new Error('failed to extract renderAttestationDetails body');
const fnSrc = src.slice(startIdx, endIdx);
const renderAttestationDetails = eval('(' + fnSrc.replace(/^function /, 'function ') + ')');

/* __INVOCATION_BELOW__ */
__INVOCATION__
"""


def _harness(invocation: str, lang: str = "en") -> str:
    return _HARNESS.replace("__INVOCATION__", invocation), {"STAKEMATE_TEST_LANG": lang}


def _run_render(att: dict, timeline: dict | None, lang: str = "en") -> str:
    inv = (
        f"const att = {json.dumps(att)};\n"
        f"const timeline = {json.dumps(timeline)};\n"
        "process.stdout.write(renderAttestationDetails(att, timeline));\n"
    )
    script, env = _harness(inv, lang)
    node = _node_or_skip()
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env={**os.environ, **env},
    )
    assert proc.returncode == 0, (
        f"node failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return proc.stdout


# ---------------------------------------------------------------------------


def test_until_target_row_when_target_in_future():
    """Happy case: ``current=9_291_987`` and ``target=9_292_130`` puts
    the user 143 blocks before the target. The new row should render
    with the right number + a parenthetical minute estimate."""
    html = _run_render(
        {
            "current_block": 9_291_987,
            "target_block": 9_292_130,
            "attestation_window_blocks": 60,
        },
        {"next_epoch": 9597, "blocks_left_in_epoch": 293, "seconds_left_in_epoch": 760},
        lang="en",
    )
    assert "Time to target" in html
    # 143 blocks @ ~2.6s/block ≈ 372s ≈ 6 minutes
    assert "143 blocks" in html
    # The minute estimate is an integer, format-tolerant — match either
    # "6 min" or "6 minutes" depending on locale.
    assert "6 min" in html or "~6" in html


def test_until_target_row_hidden_when_target_already_passed():
    """When ``current >= target`` the user is already inside the sign
    window. The new row should be omitted entirely (no negative numbers
    or ``0 blocks`` artefacts)."""
    html = _run_render(
        {
            "current_block": 9_292_180,
            "target_block": 9_292_130,
            "attestation_window_blocks": 60,
        },
        None,
        lang="en",
    )
    assert "Time to target" not in html
    # Other rows should still render.
    assert "Sign window" in html
    assert "Until window close" in html


def test_until_target_row_hidden_at_exact_target():
    """At the boundary (``current == target``) we also hide it — saying
    "0 blocks (~0 sec)" is just clutter when the next row already shows
    the same information from the close-of-window angle."""
    html = _run_render(
        {
            "current_block": 9_292_130,
            "target_block": 9_292_130,
            "attestation_window_blocks": 60,
        },
        None,
        lang="en",
    )
    assert "Time to target" not in html


def test_target_label_carries_info_button():
    """The target-block label cell must include a ``button.info-icon``
    with the right ``data-info-key`` so the runtime tap-handler can
    surface the popup. Aria label uses the localized ``_aria`` key."""
    html = _run_render(
        {
            "current_block": 9_291_987,
            "target_block": 9_292_130,
            "attestation_window_blocks": 60,
        },
        None,
        lang="en",
    )
    assert 'class="info-icon"' in html
    assert 'data-info-key="webapp_attestation_target_help"' in html
    assert 'aria-label="What does this mean?"' in html
    # The button must NOT show up next to the *current* block label —
    # info icons are limited to the target row by design.
    current_idx = html.index("Current block")
    target_idx = html.index("Assigned block")
    info_idx = html.index("info-icon")
    # Sanity: the info icon must appear after "Current block" in source
    # order, AND between current and the value column of target. The
    # tightest assertion we can make without parsing HTML is that it's
    # not BEFORE "Current block" (would mean it's on the wrong label).
    assert info_idx > current_idx
    assert info_idx > target_idx - 200  # within the same row's label cell


def test_until_target_row_pluralisation_ru():
    """RU has 3 plural categories (one/few/many). With the test cases
    1 / 3 / 5 the renderer should pick "1 блок", "3 блока", "5 блоков".
    Catches a regression where the new row used the wrong plural key."""
    cases = [(1, "1 блок"), (3, "3 блока"), (5, "5 блоков")]
    for delta, expected in cases:
        html = _run_render(
            {
                "current_block": 9_292_130 - delta,
                "target_block": 9_292_130,
                "attestation_window_blocks": 60,
            },
            None,
            lang="ru",
        )
        assert "До целевого блока" in html, (
            f"delta={delta}: 'До целевого блока' label missing in:\n{html}"
        )
        assert expected in html, (
            f"delta={delta}: expected {expected!r} in render output\n{html}"
        )


def test_until_target_row_does_not_break_old_rows():
    """Insert one row → all the existing rows must still be present and
    in the right relative order. Keeps the rest of the grid stable."""
    html = _run_render(
        {
            "current_block": 9_291_987,
            "target_block": 9_292_130,
            "attestation_window_blocks": 60,
        },
        {"next_epoch": 9597, "blocks_left_in_epoch": 293, "seconds_left_in_epoch": 760},
        lang="en",
    )
    # Source-order check: current → target → until-target → window → close → next-epoch
    expected = [
        "Current block",
        "Assigned block",
        "Time to target",
        "Sign window",
        "Until window close",
        "Next epoch 9597",
    ]
    last = -1
    for label in expected:
        idx = html.find(label)
        assert idx > last, f"{label!r} out of order or missing in:\n{html}"
        last = idx
