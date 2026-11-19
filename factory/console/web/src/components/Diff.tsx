/** One diff, its added and removed lines tinted */
export function Diff({ text }: { text: string }) {
  return (
    <pre data-testid="diff" className="mt-2 overflow-x-auto rounded-sm border border-line bg-page py-2 font-mono text-xs leading-5">
      {text.split("\n").map((line, i) => (
        <span
          key={i}
          className={`block px-3 ${
            line.startsWith("+") && !line.startsWith("+++") ? "bg-lamp-ready/15 text-primary"
            : line.startsWith("-") && !line.startsWith("---") ? "bg-lamp-live/15 text-primary"
            : "text-secondary"
          }`}
        >
          {line || " "}
        </span>
      ))}
    </pre>
  );
}
