import type { ReactNode } from "react";

const VARIANT = {
  primary: "bg-primary font-semibold text-on-primary hover:bg-primary-hover",
  secondary: "border border-border-strong bg-selected hover:bg-bubble",
  ghost: "text-subtle hover:bg-line hover:text-foreground",
  quiet: "text-muted-foreground hover:bg-line hover:text-foreground",
  danger: "text-muted-foreground hover:bg-destructive/10 hover:text-destructive",
};

// The key that does the same, like y for Accept
export function KeyHint({ k, onPrimary = false }: { k: string; onPrimary?: boolean }) {
  return (
    <span
      className={`rounded px-[5px] font-mono text-[11px] ${
        onPrimary ? "bg-black/20 font-semibold" : "bg-background text-muted-foreground"
      }`}
    >
      {k}
    </span>
  );
}

// A button in a card's row of choices, with its key
export function ActionButton({
  variant,
  hint,
  title,
  onClick,
  disabled = false,
  className = "",
  children,
}: {
  variant: keyof typeof VARIANT;
  hint?: string;
  title?: string;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      disabled={disabled}
      className={`flex items-center gap-2 rounded-lg px-3 py-1.5 text-[13px] disabled:pointer-events-none disabled:opacity-50 ${VARIANT[variant]} ${className}`}
    >
      {children}
      {hint && <KeyHint k={hint} onPrimary={variant === "primary"} />}
    </button>
  );
}
