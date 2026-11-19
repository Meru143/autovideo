import { useSyncExternalStore, type AnchorHTMLAttributes } from "react";

// ponytail: six routes need no router library, just pushState, popstate, and a link that uses them

const subscribe = (fn: () => void) => {
  window.addEventListener("popstate", fn);
  return () => window.removeEventListener("popstate", fn);
};

const go = (to: string) => {
  if (to !== window.location.pathname + window.location.search) window.history.pushState(null, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
};

// ponytail: one guard at a time (the page with unsaved work); the browser's Back button isn't covered
let guard: ((proceed: () => void) => void) | null = null;

/** While set, a move to another page is handed to `fn` with a `proceed` that makes it, instead of happening. Returns
 * the function that clears it. */
export function guardNavigation(fn: (proceed: () => void) => void) {
  guard = fn;
  return () => {
    if (guard === fn) guard = null;
  };
}

export function navigate(to: string) {
  if (guard) guard(() => go(to));
  else go(to);
}

export const usePath = () => useSyncExternalStore(subscribe, () => window.location.pathname);

/** An <a> that moves inside the app; a middle or modified click, or one on a link to another tab (target="_blank"),
 * is left to the browser, as a link's is */
export function Link({ to, ...rest }: { to: string } & AnchorHTMLAttributes<HTMLAnchorElement>) {
  return (
    <a
      {...rest}
      href={to}
      onClick={(e) => {
        rest.onClick?.(e);
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        if (rest.target && rest.target !== "_self") return;
        e.preventDefault();
        navigate(to);
      }}
    />
  );
}
