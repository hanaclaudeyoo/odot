import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Check, Clock, Edit3, LayoutGrid, List, Trash2, X } from "lucide-react";
import "./styles.css";

type TaskStatus = "active" | "archived";

type Task = {
  id: number;
  title: string;
  importance: number;
  urgency: number;
  difficulty: number;
  time_estimate_minutes: number;
  status: TaskStatus;
  created_at: string;
  archived_at: string | null;
  actual_duration_seconds: number | null;
};

type Session = {
  task: Task;
  started_at: string;
  decline_available_until: string;
};

type TaskDraft = {
  title: string;
  importance: number;
  urgency: number;
  difficulty: number;
  time_estimate_minutes: number;
};

const emptyDraft: TaskDraft = {
  title: "",
  importance: 4,
  urgency: 4,
  difficulty: 4,
  time_estimate_minutes: 30,
};

const axisValues = [1, 2, 3, 4, 5, 6, 7];
const API_BASE = "/api";

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(body.detail ?? "Request failed");
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json();
}

function formatElapsed(seconds: number): string {
  const safe = Math.max(0, seconds);
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

function difficultyColor(difficulty: number): string {
  const hue = 120 - ((difficulty - 1) / 6) * 120;
  return `hsl(${hue} 82% 45%)`;
}

function draftFromTask(task: Task): TaskDraft {
  return {
    title: task.title,
    importance: task.importance,
    urgency: task.urgency,
    difficulty: task.difficulty,
    time_estimate_minutes: task.time_estimate_minutes,
  };
}

function AxisInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="axis-control">
      <span>
        {label} <strong>{value}</strong>
      </span>
      <input
        type="range"
        min="1"
        max="7"
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function TaskForm({
  draft,
  submitLabel,
  onChange,
  onSubmit,
  onCancel,
}: {
  draft: TaskDraft;
  submitLabel: string;
  onChange: (draft: TaskDraft) => void;
  onSubmit: () => void;
  onCancel?: () => void;
}) {
  return (
    <form
      className="task-form"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label className="field">
        <span>Title</span>
        <input
          value={draft.title}
          onChange={(event) => onChange({ ...draft, title: event.target.value })}
          placeholder="Write the proposal intro"
        />
      </label>
      <div className="axis-grid">
        <AxisInput
          label="Importance"
          value={draft.importance}
          onChange={(importance) => onChange({ ...draft, importance })}
        />
        <AxisInput
          label="Urgency"
          value={draft.urgency}
          onChange={(urgency) => onChange({ ...draft, urgency })}
        />
        <AxisInput
          label="Difficulty"
          value={draft.difficulty}
          onChange={(difficulty) => onChange({ ...draft, difficulty })}
        />
      </div>
      <label className="field compact-field">
        <span>Estimate</span>
        <input
          type="number"
          min="1"
          value={draft.time_estimate_minutes}
          onChange={(event) =>
            onChange({ ...draft, time_estimate_minutes: Number(event.target.value) })
          }
        />
        <span>min</span>
      </label>
      <div className="form-actions">
        {onCancel && (
          <button className="secondary" type="button" onClick={onCancel}>
            <X size={16} /> Cancel
          </button>
        )}
        <button type="submit">
          <Check size={16} /> {submitLabel}
        </button>
      </div>
    </form>
  );
}

function TaskTable({
  tasks,
  onEdit,
  onDelete,
}: {
  tasks: Task[];
  onEdit: (task: Task) => void;
  onDelete: (task: Task) => void;
}) {
  const [sortKey, setSortKey] = useState<"importance" | "urgency" | "difficulty">("urgency");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const sorted = [...tasks].sort((a, b) => {
    const delta = a[sortKey] - b[sortKey];
    return sortDir === "asc" ? delta : -delta;
  });

  function toggleSort(key: typeof sortKey) {
    if (key === sortKey) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            {(["importance", "urgency", "difficulty"] as const).map((key) => (
              <th key={key}>
                <button className="sort-button" onClick={() => toggleSort(key)}>
                  {key} {sortKey === key ? (sortDir === "asc" ? "↑" : "↓") : ""}
                </button>
              </th>
            ))}
            <th>Estimate</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((task) => (
            <tr key={task.id}>
              <td>{task.title}</td>
              <td>{task.importance}</td>
              <td>{task.urgency}</td>
              <td>
                <span
                  className="difficulty-dot"
                  style={{ background: difficultyColor(task.difficulty) }}
                />
                {task.difficulty}
              </td>
              <td>{task.time_estimate_minutes}m</td>
              <td className="row-actions">
                <button className="icon-button" title="Edit task" onClick={() => onEdit(task)}>
                  <Edit3 size={16} />
                </button>
                <button className="icon-button danger" title="Delete task" onClick={() => onDelete(task)}>
                  <Trash2 size={16} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {tasks.length === 0 && <p className="empty">No active tasks.</p>}
    </div>
  );
}

function MatrixView({ tasks }: { tasks: Task[] }) {
  const rows = [...axisValues].reverse();
  return (
    <div className="matrix-shell">
      <div className="y-label">Importance</div>
      <div className="matrix">
        {rows.map((importance) =>
          axisValues.map((urgency) => {
            const cellTasks = tasks.filter(
              (task) => task.importance === importance && task.urgency === urgency,
            );
            return (
              <div className="matrix-cell" key={`${importance}-${urgency}`}>
                <span className="cell-index">
                  {urgency},{importance}
                </span>
                <div className="cell-tasks">
                  {cellTasks.map((task) => (
                    <span
                      className="matrix-task"
                      title={`${task.title} · difficulty ${task.difficulty}`}
                      style={{ background: difficultyColor(task.difficulty) }}
                      key={task.id}
                    >
                      {task.title}
                    </span>
                  ))}
                </div>
              </div>
            );
          }),
        )}
      </div>
      <div className="x-label">Urgency</div>
    </div>
  );
}

function ArchiveList({ tasks }: { tasks: Task[] }) {
  return (
    <div className="table-wrap archive">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            <th>Importance</th>
            <th>Urgency</th>
            <th>Difficulty</th>
            <th>Estimate</th>
            <th>Actual</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.id}>
              <td>{task.title}</td>
              <td>{task.importance}</td>
              <td>{task.urgency}</td>
              <td>{task.difficulty}</td>
              <td>{task.time_estimate_minutes}m</td>
              <td>{formatElapsed(task.actual_duration_seconds ?? 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {tasks.length === 0 && <p className="empty">No archived tasks.</p>}
    </div>
  );
}

function PulledTask({
  session,
  onFinish,
  onDecline,
}: {
  session: Session;
  onFinish: () => void;
  onDecline: () => void;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, []);

  const elapsed = Math.floor((now - new Date(session.started_at).getTime()) / 1000);
  const declineSecondsLeft = Math.floor(
    (new Date(session.decline_available_until).getTime() - now) / 1000,
  );
  const canDecline = declineSecondsLeft > 0;

  return (
    <main className="focus-layout">
      <section className="focus-panel">
        <div className="brand-row">
          <h1>Odot</h1>
          <span className="pill">Pulled task</span>
        </div>
        <h2>{session.task.title}</h2>
        <div className="timer">
          <Clock size={22} />
          <span>{formatElapsed(elapsed)}</span>
        </div>
        <div className="task-metrics">
          <span>Importance {session.task.importance}</span>
          <span>Urgency {session.task.urgency}</span>
          <span>Difficulty {session.task.difficulty}</span>
          <span>Estimate {session.task.time_estimate_minutes}m</span>
        </div>
        <div className="focus-actions">
          {canDecline && (
            <button className="secondary" onClick={onDecline}>
              <Edit3 size={16} /> Decline/Edit
            </button>
          )}
          <button onClick={onFinish}>
            <Check size={16} /> Finished
          </button>
        </div>
        {canDecline ? (
          <p className="subtle">Decline window: {formatElapsed(declineSecondsLeft)}</p>
        ) : (
          <p className="subtle">Decline window closed.</p>
        )}
      </section>
    </main>
  );
}

function DeclineEditor({
  session,
  onCancel,
  onSaved,
}: {
  session: Session;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<TaskDraft>(draftFromTask(session.task));
  const [error, setError] = useState("");

  async function save() {
    setError("");
    try {
      await api<Task | null>("/session/decline-edit", {
        method: "POST",
        body: JSON.stringify({ action: "update", task: draft }),
      });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save edit");
    }
  }

  async function deleteTask() {
    setError("");
    try {
      await api<Task | null>("/session/decline-edit", {
        method: "POST",
        body: JSON.stringify({ action: "delete" }),
      });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete task");
    }
  }

  return (
    <main className="focus-layout">
      <section className="focus-panel edit-panel">
        <div className="brand-row">
          <h1>Odot</h1>
          <span className="pill">Decline edit</span>
        </div>
        <TaskForm draft={draft} submitLabel="Save edit" onChange={setDraft} onSubmit={save} onCancel={onCancel} />
        <button className="danger full-width" onClick={deleteTask}>
          <Trash2 size={16} /> Delete task
        </button>
        {error && <p className="error">{error}</p>}
      </section>
    </main>
  );
}

function App() {
  const [activeTasks, setActiveTasks] = useState<Task[]>([]);
  const [archivedTasks, setArchivedTasks] = useState<Task[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [draft, setDraft] = useState<TaskDraft>(emptyDraft);
  const [editingTask, setEditingTask] = useState<Task | null>(null);
  const [energyLevel, setEnergyLevel] = useState(4);
  const [view, setView] = useState<"list" | "matrix">("list");
  const [declineEditing, setDeclineEditing] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    const [active, archived, currentSession] = await Promise.all([
      api<Task[]>("/tasks?status=active"),
      api<Task[]>("/tasks?status=archived"),
      api<Session | null>("/session"),
    ]);
    setActiveTasks(active);
    setArchivedTasks(archived);
    setSession(currentSession);
  }

  useEffect(() => {
    refresh().catch((err) => setError(err instanceof Error ? err.message : "Could not load tasks"));
  }, []);

  async function submitTask() {
    setError("");
    try {
      if (editingTask) {
        await api<Task>(`/tasks/${editingTask.id}`, {
          method: "PATCH",
          body: JSON.stringify(draft),
        });
        setEditingTask(null);
      } else {
        await api<Task>("/tasks", { method: "POST", body: JSON.stringify(draft) });
      }
      setDraft(emptyDraft);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save task");
    }
  }

  async function deleteTask(task: Task) {
    setError("");
    try {
      await api<void>(`/tasks/${task.id}`, { method: "DELETE" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete task");
    }
  }

  async function pullTask() {
    setError("");
    try {
      const pulled = await api<Session>("/tasks/pull", {
        method: "POST",
        body: JSON.stringify({ energy_level: energyLevel }),
      });
      setSession(pulled);
      setDeclineEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not pull task");
    }
  }

  async function finishTask() {
    if (!session) return;
    setError("");
    try {
      await api<Task>(`/tasks/${session.task.id}/finish`, { method: "POST" });
      setSession(null);
      setDeclineEditing(false);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not finish task");
    }
  }

  if (session && declineEditing) {
    return (
      <DeclineEditor
        session={session}
        onCancel={() => setDeclineEditing(false)}
        onSaved={async () => {
          setDeclineEditing(false);
          setSession(null);
          await refresh();
        }}
      />
    );
  }

  if (session) {
    return <PulledTask session={session} onDecline={() => setDeclineEditing(true)} onFinish={finishTask} />;
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <h1>Odot</h1>
        <div className="pull-controls">
          <AxisInput label="Energy" value={energyLevel} onChange={setEnergyLevel} />
          <button onClick={pullTask}>
            <Clock size={16} /> Pull Task
          </button>
        </div>
      </header>

      {error && <p className="error">{error}</p>}

      <section className="workspace">
        <aside className="side-panel">
          <h2>{editingTask ? "Edit Task" : "New Task"}</h2>
          <TaskForm
            draft={draft}
            submitLabel={editingTask ? "Save Task" : "Add Task"}
            onChange={setDraft}
            onSubmit={submitTask}
            onCancel={
              editingTask
                ? () => {
                    setEditingTask(null);
                    setDraft(emptyDraft);
                  }
                : undefined
            }
          />
        </aside>

        <section className="main-panel">
          <div className="panel-heading">
            <h2>Active Tasks</h2>
            <div className="tabs">
              <button className={view === "list" ? "active" : ""} onClick={() => setView("list")}>
                <List size={16} /> List
              </button>
              <button className={view === "matrix" ? "active" : ""} onClick={() => setView("matrix")}>
                <LayoutGrid size={16} /> Matrix
              </button>
            </div>
          </div>
          {view === "list" ? (
            <TaskTable
              tasks={activeTasks}
              onEdit={(task) => {
                setEditingTask(task);
                setDraft(draftFromTask(task));
              }}
              onDelete={deleteTask}
            />
          ) : (
            <MatrixView tasks={activeTasks} />
          )}
        </section>
      </section>

      <section className="archive-section">
        <h2>Archive</h2>
        <ArchiveList tasks={archivedTasks} />
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
