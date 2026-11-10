/**
 * R6 shared reduced-motion helpers (src/utils/motion.ts): the positive-
 * preference contract — 'auto' only on an explicit reduce report — plus the
 * exact media query consulted and the safe degrades for missing/throwing
 * matchMedia implementations. The matching CSS gate lives in
 * theme/nb-shared.css (app-wide) and is not re-proven here.
 */

import { describe, expect, test, vi } from 'vitest';

import { REDUCED_MOTION_QUERY, prefersReducedMotion, scrollBehavior } from '../../src/utils/motion';

function mediaSource(matches: boolean) {
  return { matchMedia: vi.fn(() => ({ matches })) };
}

describe('prefersReducedMotion', () => {
  test('a positive reduce report -> true, consulting the exact media query', () => {
    const source = mediaSource(true);
    expect(prefersReducedMotion(source)).toBe(true);
    expect(source.matchMedia).toHaveBeenCalledWith(REDUCED_MOTION_QUERY);
  });

  test('no reduce report -> false (motion stays on by default)', () => {
    const source = mediaSource(false);
    expect(prefersReducedMotion(source)).toBe(false);
    expect(source.matchMedia).toHaveBeenCalledWith(REDUCED_MOTION_QUERY);
  });

  test('missing window/matchMedia degrades to "not reduced" without throwing', () => {
    expect(prefersReducedMotion(undefined)).toBe(false);
    expect(prefersReducedMotion(null)).toBe(false);
    expect(prefersReducedMotion({})).toBe(false);
  });

  test('a throwing matchMedia degrades to "not reduced"', () => {
    const source = { matchMedia: vi.fn(() => { throw new Error('no media'); }) };
    expect(prefersReducedMotion(source)).toBe(false);
  });

  test('the environment default (jsdom) reports no reduce -> not reduced', () => {
    expect(prefersReducedMotion()).toBe(false);
  });
});

describe('scrollBehavior', () => {
  test("de-grades 'smooth' to 'auto' under prefers-reduced-motion", () => {
    expect(scrollBehavior(mediaSource(true))).toBe('auto');
  });

  test("keeps 'smooth' when motion is allowed, unknown, or matchMedia is absent", () => {
    expect(scrollBehavior(mediaSource(false))).toBe('smooth');
    expect(scrollBehavior(undefined)).toBe('smooth'); // jsdom default: no reduce
    expect(scrollBehavior(null)).toBe('smooth');
  });

  test('returns a value assignable to DOM scroll options', () => {
    const behavior: ScrollBehavior = scrollBehavior(mediaSource(true));
    expect(behavior).toBe('auto');
  });
});