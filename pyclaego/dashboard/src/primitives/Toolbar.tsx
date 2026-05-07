import type { ToolbarSchema, ButtonSpec } from '../schema/types';

interface ToolbarProps extends ToolbarSchema {
  onCommand: (command: string, args?: Record<string, unknown>) => void;
}

export function Toolbar({ buttons, onCommand }: ToolbarProps) {
  return (
    <div className="p-toolbar">
      {buttons.map((btn, i) => (
        <ToolbarButton key={i} spec={btn} onCommand={onCommand} />
      ))}
    </div>
  );
}

function ToolbarButton({ spec, onCommand }: { spec: ButtonSpec; onCommand: ToolbarProps['onCommand'] }) {
  const cls = `p-toolbar-btn ${spec.variant ?? 'ghost'}`;
  return (
    <button
      className={cls}
      disabled={spec.disabled ?? false}
      onClick={() => onCommand(spec.command, spec.args)}
    >
      {spec.label}
    </button>
  );
}
