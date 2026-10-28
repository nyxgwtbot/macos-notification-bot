/**
 * Shared motion-preference helpers (R6 base): under `prefers-reduced-motion:
 * reduce` programmatic scrolls de-grade their 'smooth' behavior to 'auto',
 * while the CSS gate (transitions/animations collapsed ~0 + scroll-behavior
 * auto app-wide) lives in theme/nb-shared.css. Scroll call sites wire these
 * in their own missions — this module is the shared contract.
 * Tests: tests/unit/motion.spec.ts.
 */

/** The media query the helpers consult (exported so specs can pin it). */
export const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

/** Minimal matchMedia surface the helpers rely on (injectable for tests). */
export interface MediaQuerySource {
  matchMedia?(query: string): { matches: boolean };
}

/**
 * True only on a POSITIVE reduced-motion report from the environment.
 * A missing window/matchMedia (jsdom defaults, old webviews) or a throwing
 * call reads as NOT reduced — motion degrades only when the user asked.
 */
export function prefersReducedMotion(
  source: MediaQuerySource | undefined | null = typeof window !== 'undefined' ? window : undefined
): boolean {
  try {
    return source?.matchMedia?.(REDUCED_MOTION_QUERY)?.matches === true;
  } catch {
    return false;
  }
}

/**
 * Behavior for programmatic scrolls: 'auto' under prefers-reduced-motion,
 * 'smooth' otherwise. Call sites route their behavior strings through this
 * (e.g. `scrollTo({ top, behavior: scrollBehavior() })`).
 */
export function scrollBehavior(
  source: MediaQuerySource | undefined | null = typeof window !== 'undefined' ? window : undefined
): ScrollBehavior {
  return prefersReducedMotion(source) ? 'auto' : 'smooth';
}