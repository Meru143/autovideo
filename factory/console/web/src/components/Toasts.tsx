import * as Toast from "@radix-ui/react-toast";
import { X } from "lucide-react";
import { useSyncExternalStore } from "react";
import type { Lamp as Kind } from "../api";
import { Lamp } from "./Lamp";

type Note = { id: number; title: string; text: string; lamp?: Kind };

// ponytail: one list for the whole app, so any page, or a post that ends after its page has gone, can add to it
let notes: Note[] = [];
let made = 0;
const subs = new Set<() => void>();
const set = (next: Note[]) => {
  notes = next;
  subs.forEach((f) => f());
};
const subscribe = (f: () => void) => {
  subs.add(f);
  return () => void subs.delete(f);
};

/** A note in the corner: what an action did, or the server's own reason it refused. A refusal (the attention lamp)
 * stays until dismissed. */
export const toast = (note: Omit<Note, "id">) => set([...notes, { ...note, id: made++ }]);

/** Where the notes show, mounted once by App. The rest go after 8 s, a timer Radix holds while the window is in the
 * background or the pointer is on them. */
export function Toasts() {
  const list = useSyncExternalStore(subscribe, () => notes);
  return (
    <Toast.Provider duration={8000}>
      {list.map((n) => (
        <Toast.Root
          key={n.id}
          type={n.lamp === "attention" ? "foreground" : "background"}
          duration={n.lamp === "attention" ? Infinity : undefined}
          onOpenChange={(open) => {
            if (!open) set(notes.filter((x) => x.id !== n.id));
          }}
          className="flex items-start gap-3 rounded-lg border border-line bg-raised p-3 text-sm motion-safe:animate-rise"
        >
          {n.lamp && (
            <span className="flex h-5 items-center">
              <Lamp kind={n.lamp} />
            </span>
          )}
          <div className="min-w-0 flex-1">
            <Toast.Title className="font-medium">{n.title}</Toast.Title>
            <Toast.Description className="break-words whitespace-pre-line text-secondary">{n.text}</Toast.Description>
          </div>
          <Toast.Close aria-label="Dismiss" className="grid size-5 shrink-0 place-items-center rounded-sm hover:bg-line">
            <X aria-hidden size={16} strokeWidth={1.5} className="text-muted" />
          </Toast.Close>
        </Toast.Root>
      ))}
      <Toast.Viewport className="fixed right-6 bottom-6 z-10 flex w-96 flex-col gap-2" />
    </Toast.Provider>
  );
}
