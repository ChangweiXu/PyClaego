import { useState, type ReactNode } from 'react';

interface Tab {
  label: string;
  content: ReactNode;
}

interface TabsProps {
  tabs: Tab[];
}

export function Tabs({ tabs }: TabsProps) {
  const [active, setActive] = useState(0);
  return (
    <div className="p-tabs">
      <div className="p-tabs-header">
        {tabs.map((t, i) => (
          <button
            key={i}
            className={`p-tab-btn ${i === active ? 'active' : ''}`}
            onClick={() => setActive(i)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="p-tabs-body">{tabs[active]?.content}</div>
    </div>
  );
}
