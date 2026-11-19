import type { Lamp as Kind } from "../api";

const LOOK: Record<Kind, string> = {
  making: "bg-lamp-making",
  ready: "bg-lamp-ready",
  live: "bg-lamp-live",
  attention: "border-2 border-lamp-attention", // hollow, unlike the lit lamps, so it reads without its colour
};

/** The tally lamp: dim while being made, green ready, red on air, a hollow amber circle when it needs you. The words
 * beside it always say the same, so screen readers skip it. */
export function Lamp({ kind }: { kind: Kind }) {
  return <span aria-hidden data-lamp={kind} className={`inline-block size-2.5 shrink-0 rounded-full ${LOOK[kind]}`} />;
}
