import * as DialogPrimitive from "@radix-ui/react-dialog";
import type { ReactNode } from "react";
import { X } from "lucide-react";

import { cn } from "../../lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

function Overlay() {
  return <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-slate-950/30 backdrop-blur-[2px]" />;
}

function CloseButton() {
  return (
    <DialogPrimitive.Close
      aria-label="关闭"
      className="absolute right-3 top-3 grid size-8 place-items-center rounded-lg text-muted-ink hover:bg-subtle hover:text-ink"
    >
      <X size={18} />
    </DialogPrimitive.Close>
  );
}

export function DialogContent({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <DialogPrimitive.Portal>
      <Overlay />
      <DialogPrimitive.Content
        className={cn(
          "fixed left-1/2 top-1/2 z-50 max-h-[88vh] w-[min(640px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-2xl border bg-panel p-6 shadow-[var(--shadow-pop)] focus:outline-none",
          className,
        )}
      >
        {children}
        <CloseButton />
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

/** A panel that slides in from the side; used for source passages and the mobile menu. */
export function SheetContent({
  children,
  className,
  side = "right",
  label,
}: {
  children: ReactNode;
  className?: string;
  side?: "left" | "right";
  label?: string;
}) {
  return (
    <DialogPrimitive.Portal>
      <Overlay />
      <DialogPrimitive.Content
        aria-describedby={undefined}
        aria-label={label}
        className={cn(
          "fixed inset-y-0 z-50 flex w-[min(520px,100vw)] flex-col overflow-y-auto bg-panel shadow-[var(--shadow-pop)] focus:outline-none",
          side === "right" ? "right-0 border-l" : "left-0 border-r",
          className,
        )}
      >
        {children}
        <CloseButton />
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function DialogTitle({ children, className }: { children: ReactNode; className?: string }) {
  return <DialogPrimitive.Title className={cn("pr-10 text-lg font-semibold leading-7 text-ink", className)}>{children}</DialogPrimitive.Title>;
}

export function DialogDescription({ children }: { children: ReactNode }) {
  return <DialogPrimitive.Description className="mt-1 text-sm leading-6 text-muted-ink">{children}</DialogPrimitive.Description>;
}
