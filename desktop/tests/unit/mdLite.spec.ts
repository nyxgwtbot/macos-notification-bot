import { describe, expect, test } from 'vitest';

import { __mdInternals, mdLite } from '../../src/utils/mdLite';

describe('mdLite XSS boundary', () => {
  test('raw HTML tags are escaped, never rendered', () => {
    const out = mdLite('<script>alert(1)</script><img src=x onerror=alert(1)>');
    expect(out).toContain('&lt;script&gt;');
    expect(out).toContain('&lt;img');
    expect(out).not.toContain('<script');
    expect(out).not.toContain('<img');
  });

  test('attribute smuggling via quotes is neutralized', () => {
    const out = mdLite('<img onmouseover="x(\'y\') < > &');
    expect(out).not.toContain('<img');
    expect(out).toContain('&quot;');
  });
});

describe('mdLite blocks', () => {
  test('plain lines become paragraphs; blank lines produce nothing', () => {
    expect(mdLite('a\n\n\nb')).toBe('<p>a</p><p>b</p>');
  });

  test('empty and whitespace-only input render empty', () => {
    expect(mdLite('')).toBe('');
    expect(mdLite(' \n \n')).toBe('');
  });

  test('hash-prefixed lines become uniform headings', () => {
    expect(mdLite('## Bot resumo')).toBe('<h4>Bot resumo</h4>');
    expect(mdLite('# Detalhes')).toBe('<h4>Detalhes</h4>');
  });

  test('fully-bold standalone lines become headings', () => {
    expect(mdLite('**Total: R$ 12,34**')).toBe('<h4>Total: R$ 12,34</h4>');
  });

  test('bold lines with surrounding text stay paragraphs with inline strong', () => {
    expect(mdLite('**Total**: R$ 12')).toBe('<p><strong>Total</strong>: R$ 12</p>');
  });

  test('dash bullets group into one list', () => {
    expect(mdLite('intro\n- x\n- y\noutro')).toBe('<p>intro</p><ul><li>x</li><li>y</li></ul><p>outro</p>');
  });

  test('star and dot bullets group with dash bullets', () => {
    expect(mdLite('- x\n* y\n• z')).toBe('<ul><li>x</li><li>y</li><li>z</li></ul>');
    expect(mdLite('1. x\n2. y')).toBe('<ol><li>x</li><li>y</li></ol>');
    expect(mdLite('1) x\n2) y')).toBe('<ol><li>x</li><li>y</li></ol>');
  });

  test('ordered and unordered lists do not merge', () => {
    expect(mdLite('- a\n3. b')).toBe('<ul><li>a</li></ul><ol><li>b</li></ol>');
  });

  test('numbers without list punctuation are not lists', () => {
    expect(mdLite('total foi 12.34 unidades')).toBe('<p>total foi 12.34 unidades</p>');
  });
});

describe('mdLite inline', () => {
  test('bold inside plain text', () => {
    expect(mdLite('margem **positiva** hoje')).toBe('<p>margem <strong>positiva</strong> hoje</p>');
  });

  test('single-asterisk emphasis', () => {
    expect(mdLite('status *ok* fim')).toBe('<p>status <em>ok</em> fim</p>');
  });

  test('code spans are protected from bold parsing and restored last', () => {
    expect(mdLite('use `**comando**` agora')).toBe('<p>use <code>**comando**</code> agora</p>');
  });

  test('code spans escape their content', () => {
    expect(mdLite('a `<b>` tag')).toBe('<p>a <code>&lt;b&gt;</code> tag</p>');
  });

  test('inline transforms inside list items and headings', () => {
    expect(mdLite('- **1** item')).toBe('<ul><li><strong>1</strong> item</li></ul>');
    expect(mdLite('## **resumo**')).toBe('<h4><strong>resumo</strong></h4>');
  });

  test('escapeHtml covers all HTML-significant characters', () => {
    expect(__mdInternals.escapeHtml('<a href="x">&\'t</a>')).toBe(
      '&lt;a href=&quot;x&quot;&gt;&amp;&#39;t&lt;/a&gt;'
    );
  });
});