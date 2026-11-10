/**
 * mdLite — presentation-only markdown renderer for assistant chat bubbles
 * (ChatView finding C6: uniform paragraph rhythm).
 *
 * Safety model: the raw reply is HTML-ESCAPED FIRST, so no markup coming from
 * the model/API/UI text can reach the DOM. The block/inline passes afterwards
 * only inject structural tags with no attributes, and no links are generated
 * (URLs stay plain text). View-layer concern only — the chat view-model state
 * machine stays untouched, and the iframe sandbox/allowlist are unrelated.
 *
 * Block grammar (line-based; blank lines are dropped — the .nb-md margin rules
 * supply the rhythm):
 *   - `#`-prefixed lines (up to four hashes) and fully-bold standalone lines
 *     ("**Title**") become headings (rendered at body size / w600 by .nb-md,
 *     never oversized);
 *   - consecutive "- "/"* "/"• " lines group into <ul>;
 *   - consecutive "1. "/"1) " lines group into <ol>;
 *   - every other non-empty line becomes a paragraph.
 * Inline grammar: `code` → <code>, **bold** → <strong>, *em* → <em>.
 */

const HEADING_RE = /^#{1,4}\s+(.*)$/;
const STANDALONE_BOLD_RE = /^\*\*([^*]+)\*\*$/;
const DASH_BULLET_RE = /^\s*[-•]\s+(.*)$/;
const STAR_BULLET_RE = /^\s*\*\s+(.*)$/;
const ORDERED_RE = /^\s*\d+[.)]\s+(.*)$/

/** Escape every HTML-significant char BEFORE anything else — this is the XSS
 * boundary of the renderer: only <code>/<strong>/<em>/<p>/<h4>/<li>/<ul>/<ol>
 * built below ever reach the output. */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function mdLite(text: string): string {
  const escaped = text ? escapeHtml(text) : '';

  // Inline pipeline runs per line. Code spans are parked behind placeholders
  // first so their content never gets re-parsed by bold/emphasis, then
  // restored at the end of each inline pass.
  const codes: string[] = [];
  const parked = escaped.replace(/`([^`\n]+)`/g, (_match, code: string) => {
    codes.push(`<code>${code}</code>`);
    return `\ue000CODE${codes.length - 1}\ue001`;
  });

  const inline = (line: string): string =>
    line
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      // single-asterisk emphasis only when not adjacent to another asterisk
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/[\ue000]CODE(\d+)[\ue001]/g, (_match, index: string) => codes[Number(index)] ?? '');

  const lines = parked.split(/\r?\n/);
  const blocks: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flushList = (): void => {
    if (!list) return;
    const items = list.items.map((item) => `<li>${inline(item)}</li>`).join('');
    blocks.push(list.ordered ? `<ol>${items}</ol>` : `<ul>${items}</ul>`);
    list = null;
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();

    // blank lines produce nothing; the .nb-md CSS supplies block rhythm
    if (line === '') {
      flushList();
      continue;
    }

    const dash = DASH_BULLET_RE.exec(line);
    const star = STAR_BULLET_RE.exec(line);
    const ordered = ORDERED_RE.exec(line);

    if (dash) {
      // unordered markers join the open unordered list; only an open
      // *ordered* list breaks it
      if (list && list.ordered) flushList();
      if (!list) list = { ordered: false, items: [] };
      list.items.push(inline(dash[1]));
      continue;
    }
    if (star) {
      if (list && list.ordered) flushList();
      if (!list) list = { ordered: false, items: [] };
      list.items.push(inline(star[1]));
      continue;
    }
    if (ordered) {
      if (list && !list.ordered) flushList();
      if (!list) list = { ordered: true, items: [] };
      list.items.push(inline(ordered[1]));
      continue;
    }
    flushList();

    const heading = HEADING_RE.exec(line);
    if (heading) {
      blocks.push(`<h4>${inline(heading[1])}</h4>`);
      continue;
    }
    const boldSolo = STANDALONE_BOLD_RE.exec(line);
    if (boldSolo) {
      blocks.push(`<h4>${inline(boldSolo[1])}</h4>`);
      continue;
    }
    blocks.push(`<p>${inline(line)}</p>`);
  }
  flushList();

  return blocks.join('');
}

/** Exposed for unit specs only. */
export const __mdInternals = { escapeHtml };