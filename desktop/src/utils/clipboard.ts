/**
 * Clipboard helper shared by the Logs line/copy-all buttons and the Status
 * expanded-row copy: navigator.clipboard when the context is secure, with the
 * hidden-textarea execCommand fallback for the non-secure capacitor scheme.
 * Returns true when the text really left the renderer, so callers can show
 * their transient "Copied" affordance only after success.
 */

/** Hidden-textarea fallback (execCommand) for non-secure contexts. */
function copyViaTextarea(text: string): boolean {
  if (typeof document === 'undefined') return false;
  const helper = document.createElement('textarea');
  helper.value = text;
  helper.setAttribute('readonly', '');
  helper.style.position = 'fixed';
  helper.style.opacity = '0';
  document.body.appendChild(helper);
  helper.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (err) {
    console.debug('execCommand copy failed', err);
    ok = false;
  } finally {
    document.body.removeChild(helper);
  }
  return ok;
}

/** Copy text with the clipboard API + textarea fallback; false when neither path works. */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.debug('clipboard API failed', err);
    }
  }
  return copyViaTextarea(text);
}