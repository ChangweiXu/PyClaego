import { useEffect, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { ReactQueryDevtools } from '@tanstack/react-query-devtools';
import { queryClient } from './queries/client';
import { api, type PSSummary } from './api';
import { bridge } from './ws/bridge';
import { tasksWS } from './ws/tasksWS';
import { useUIStore } from './store/ui';
import DashboardPage from './pages/Dashboard';
import TasksPage from './pages/TasksPage';
import Tasks2Page from './pages/Tasks2Page';
import TasksDrawer from './components/TasksDrawer';
import NotesPage from './pages/NotesPage/NotesPage';
import LogsPage from './pages/LogsPage/LogsPage';
import { StandaloneChatPage } from './pages/StandaloneChat/StandaloneChatPage';

export default function App() {
  const [pses, setPSes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const tasksOpen = useUIStore((s) => s.tasksOpen);
  const openTasks = useUIStore((s) => s.openTasks);
  const closeTasks = useUIStore((s) => s.closeTasks);

  useEffect(() => {
    bridge.start();
    tasksWS.start();
  }, []);

  useEffect(() => {
    api
      .listPersonalSpaces()
      .then((r) => setPSes(r.personal_spaces))
      .catch((e) => console.error(e))
      .finally(() => setLoading(false));
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
    <div className="app-shell">
      <header className="app-topbar">
        <h1>🧩 PyClaego Dashboard</h1>
        <div className="ps-tabs">
          {pses.map((id) => (
            <NavLink
              key={id}
              to={`/ps/${id}`}
              className={({ isActive }) => 'tab' + (isActive ? ' active' : '')}
            >
              {id}
            </NavLink>
          ))}
          <CreatePSButton onCreated={(id) => setPSes((s) => Array.from(new Set([...s, id])))} />
        </div>
        <NavLink
          to="/tasks"
          className={({ isActive }) => 'tab' + (isActive ? ' active' : '')}
        >
          📋 Tasks
        </NavLink>
        <NavLink
          to="/tasks2"
          className={({ isActive }) => 'tab' + (isActive ? ' active' : '')}
        >
          📊 Tasks v2
        </NavLink>
        <NavLink
          to="/logs"
          className={({ isActive }) => 'tab' + (isActive ? ' active' : '')}
        >
          🗂 Logs
        </NavLink>
        <button
          className={`tasks-topbar-btn${tasksOpen ? ' tasks-topbar-btn--active' : ''}`}
          onClick={() => (tasksOpen ? closeTasks() : openTasks())}
          aria-label="Toggle tasks drawer"
        >
          📋 Drawer
        </button>
      </header>
      <main className="app-body">
        {loading ? (
          <div>Loading…</div>
        ) : (
          <Routes>
            <Route path="/" element={pses.length ? <Navigate to={`/ps/${pses[0]}`} replace /> : <EmptyState />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/tasks2" element={<Tasks2Page />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/ps/:psId" element={<DashboardPage />} />
            <Route path="/ps/:psId/notes/:widgetId" element={<NotesPage />} />
            <Route path="/chat/:psId/:widgetId" element={<StandaloneChatPage />} />
          </Routes>
        )}
      </main>
    </div>
    {tasksOpen && <TasksDrawer onClose={closeTasks} />}
    <ReactQueryDevtools initialIsOpen={false} />
    </QueryClientProvider>
  );
}

function CreatePSButton({ onCreated }: { onCreated: (id: string) => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      className="tab"
      disabled={busy}
      onClick={async () => {
        const id = prompt('PS id (letters/digits/_-)');
        if (!id) return;
        const reserved = new Set(['tasks', 'tasks2', 'ps']);
        if (reserved.has(id)) {
          alert(`"${id}" is a reserved name and cannot be used as a PS id.`);
          return;
        }
        setBusy(true);
        try {
          await api.createOrGetPS(id);
          onCreated(id);
        } catch (e) {
          alert(String(e));
        } finally {
          setBusy(false);
        }
      }}
    >
      ＋ New PS
    </button>
  );
}

function EmptyState() {
  return (
    <div>
      <p>No PersonalSpaces yet. Click <b>＋ New PS</b> in the topbar to create one.</p>
      <p>
        Or visit <Link to="/alice">/alice</Link> — opening any PS creates it on disk.
      </p>
    </div>
  );
}
