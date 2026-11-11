/* Client-side input normaliser for the Start coverage form.
 *
 * Feedback only: the server (coverageflow/sanitize.py) re-checks everything and
 * is authoritative. These functions never throw; each returns
 * { ok, value, error } so the form can show one message per field.
 *
 * Number rules (same as the server):
 *  - spaces / thin spaces are removed, unicode minus becomes "-", "1,234.5" works;
 *  - rate fields: "9.5%" -> 0.095; a bare fraction (|x| <= 1) is kept; a bare
 *    number with 1 < |x| <= 100 is read as a percentage ("9.5" -> 0.095);
 *    anything larger is rejected. A "%" sign is refused on non-rate fields;
 *  - multiple fields: "14x" or "14×" -> 14;
 *  - anything else is rejected with a field-specific error.
 */
(function (root) {
  "use strict";
  const CONTROL = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}]/gu;   // incl. zero-width and bidi marks

  function text(v, { max = 200 } = {}) {
    let s = String(v == null ? "" : v).normalize("NFC");
    s = s.replace(/[\t\n\r\v\f\u2028\u2029\p{Zs}]/gu, " ").replace(CONTROL, "");
    return s.replace(/ +/g, " ").trim().slice(0, max);
  }

  const err = (label, msg) => ({ ok: false, value: null, error: `${label}: ${msg}` });
  const ok = value => ({ ok: true, value, error: null });
  const empty = v => v == null || String(v).trim() === "";

  /* slug for the note type: lower-case, spaces to underscores. */
  function noteType(raw, label = "Note type") {
    if (empty(raw)) return err(label, "is required (e.g. quarterly_update)");
    const s = text(raw, { max: 40 }).toLowerCase().replace(/[ -]+/g, "_");
    return /^[a-z][a-z0-9_]{0,39}$/.test(s) ? ok(s)
      : err(label, "use letters, digits and underscores, starting with a letter");
  }

  /* names (segments): letters, digits and & / . , ( ) ' - space; max 48. */
  function name(raw, label = "Name") {
    if (empty(raw)) return err(label, "is required");
    const s = text(raw, { max: 48 });
    return /^[A-Za-z0-9][A-Za-z0-9 &/.,()'-]{0,47}$/.test(s) ? ok(s)
      : err(label, "use letters, digits and & / . , ( ) ' - only, starting with a letter or digit");
  }

  function ticker(raw, label = "Ticker") {
    if (empty(raw)) return err(label, "is required (e.g. AMD)");
    const s = text(raw, { max: 40 }).toUpperCase();
    return /^[A-Z0-9.-]{1,10}$/.test(s) ? ok(s)
      : err(label, "1-10 characters from A-Z 0-9 . -");
  }

  function tickers(raw, label = "Peers") {
    const parts = String(raw == null ? "" : raw).split(/[\s,;]+/).filter(Boolean);
    const out = [];
    for (const p of parts) {
      const t = ticker(p, label);
      if (!t.ok) return t;
      if (!out.includes(t.value)) out.push(t.value);
    }
    return ok(out);
  }

  function ident(raw, label = "Label") {
    const s = text(raw, { max: 80 });
    return /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(s) && !s.includes("..") ? ok(s)
      : err(label, "use letters, digits . _ - only (max 80)");
  }

  /* kind: "plain" | "rate" | "multiple" | "int". Returns ok(null) for an empty
   * value unless required. */
  function number(raw, { kind = "plain", lo = null, hi = null, label = "Value", required = true } = {}) {
    if (typeof raw === "number") raw = String(raw);
    if (empty(raw)) return required ? err(label, "is required") : ok(null);
    let s = text(raw).replace(/−|–|—/g, "-").replace(/ /g, "");
    const pct = s.endsWith("%");
    const mul = kind === "multiple" && /[xX×]$/.test(s);
    if (pct && kind !== "rate") return err(label, "a % sign is only accepted for rates");
    if (pct || mul) s = s.slice(0, -1);
    if (!/^-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$|^-?\.\d+$/.test(s))
      return err(label, `"${text(raw).slice(0, 20)}" is not a number`);
    let x = parseFloat(s.replace(/,/g, ""));
    if (kind === "int" && !Number.isInteger(x)) return err(label, "must be a whole number");
    if (kind === "rate" && (pct || (Math.abs(x) > 1 && Math.abs(x) <= 100))) x = x / 100;
    if (kind === "rate" && Math.abs(x) > 1)
      return err(label, "too large (use 0.095 or 9.5%)");
    x = Math.round(x * 1e10) / 1e10;
    if ((lo != null && x < lo) || (hi != null && x > hi))
      return err(label, `${x} is outside ${lo != null ? lo : "-inf"} to ${hi != null ? hi : "inf"}`);
    return ok(x);
  }

  const api = { text, noteType, name, ticker, tickers, ident, number };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.CFNorm = api;
})(typeof self !== "undefined" ? self : this);
