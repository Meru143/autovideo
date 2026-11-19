import type { ComponentProps } from "react";

/** The design's two buttons: the off-white primary, for the one action a place is for, and the outlined secondary.
 * Off is aria-disabled, never the native attribute, so a button that turns off under the keyboard keeps its focus (a
 * natively disabled one drops it to the page); a click on it does nothing. `disabled` dims it; `busy` ("Posting…")
 * keeps it at full strength, since its label is the news. */
export function Button({ primary = false, disabled = false, busy = false, className = "", onClick, ...rest }:
  { primary?: boolean; busy?: boolean } & ComponentProps<"button">) {
  const off = disabled || busy;
  return (
    <button
      type="button"
      {...rest}
      aria-disabled={off || undefined}
      onClick={off ? undefined : onClick}
      className={`inline-flex h-8 shrink-0 items-center rounded-md px-3 text-sm font-medium transition-colors ${
        primary ? "bg-primary text-page" : "border border-muted text-primary"} ${
        off ? "cursor-default" : primary ? "hover:bg-primary/85" : "hover:bg-line"} ${disabled ? "opacity-50" : ""} ${
        className}`}
    />
  );
}
