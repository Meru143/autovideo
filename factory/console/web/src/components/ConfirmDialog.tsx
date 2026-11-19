import * as Dialog from "@radix-ui/react-dialog";
import { useRef, type ReactNode, type RefObject } from "react";
import { Button } from "./Button";

/** Asks before something that can't be taken back. The title names what it is about; Cancel is where focus starts, so
 * Enter alone never confirms, and it goes back to `returnTo` on close. */
export function ConfirmDialog({ open, onOpenChange, title, children, confirm, onConfirm, returnTo }: {
  returnTo: RefObject<HTMLElement | null>; // what focus goes back to: nothing opens this dialog by a trigger of its own
  open: boolean; onOpenChange: (open: boolean) => void; title: string; children: ReactNode;
  confirm: string; onConfirm: () => void;
}) {
  const cancel = useRef<HTMLButtonElement>(null);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-20 bg-page/80" />
        <Dialog.Content
          onOpenAutoFocus={(e) => {
            e.preventDefault(); // not the first control in the DOM, which a reorder could make the destructive one
            cancel.current?.focus();
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            returnTo.current?.focus();
          }}
          className="fixed top-1/2 left-1/2 z-20 w-96 -translate-x-1/2 -translate-y-1/2 rounded-xl border border-line bg-raised p-5">
          <Dialog.Title className="text-base font-medium">{title}</Dialog.Title>
          <Dialog.Description className="mt-2 text-sm text-secondary">{children}</Dialog.Description>
          <div className="mt-5 flex justify-end gap-2">
            <Dialog.Close asChild>
              <Button ref={cancel}>Cancel</Button>
            </Dialog.Close>
            <Button primary onClick={onConfirm}>{confirm}</Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
