import { StrictMode, useEffect, useLayoutEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  Clock,
  Edit3,
  Folder,
  FolderOpen,
  Home,
  Menu,
  MoreVertical,
  Minus,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import "./styles.css";

type TaskStatus = "active" | "archived" | "deleted";

type Task = {
  id: number;
  title: string;
  importance: number;
  urgency: number;
  difficulty: number;
  time_estimate_minutes: number;
  deadline_at: string | null;
  start_window_at: string | null;
  dependency_ids: number[];
  category_id: number | null;
  category_snapshot: string | null;
  status: TaskStatus;
  created_at: string;
  archived_at: string | null;
  actual_duration_seconds: number | null;
};

type Category = {
  id: number;
  name: string;
  parent_id: number | null;
  sort_order: number;
  created_at: string;
};

type Session = {
  task: Task;
  started_at: string;
  decline_available_until: string;
};

type TaskDraft = {
  title: string;
  category_id: number | null;
  importance: number;
  difficulty: number;
  deadline_at: string;
  start_window_at: string;
  dependency_ids: number[];
  time_estimate_minutes: string;
};

type TaskSortKey = "importance" | "urgency" | "difficulty" | "time_estimate_minutes";

const emptyDraft: TaskDraft = {
  title: "",
  category_id: null,
  importance: 5,
  difficulty: 5,
  deadline_at: "",
  start_window_at: "",
  dependency_ids: [],
  time_estimate_minutes: "30",
};

const axisTicks = [1, 2, 3, 4, 6, 7, 8, 9, 10];
const API_BASE = "/api";

function errorMessage(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          return String(item.msg);
        }
        return String(item);
      })
      .join("; ");
  }
  return "Request failed";
}

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(errorMessage(body.detail));
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

function formatEstimateDuration(minutes: number): string {
  return `${minutes.toString().padStart(2, "0")}:00`;
}

function formatDateTime(value: string | null): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "numeric",
    day: "numeric",
    year: "2-digit",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function formatDate(value: string | null): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "numeric",
    day: "numeric",
    year: "2-digit",
  }).format(date);
}

function dateStripeKey(value: string | null): string {
  if (!value) {
    return "none";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "none";
  }
  return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
}

function formatMetric(value: number): string {
  return value.toFixed(2);
}

function difficultyColor(difficulty: number): string {
  const hue = 120 - (difficulty / 10) * 120;
  return `hsl(${hue} 82% 45%)`;
}

function matrixPercent(value: number): number {
  return Math.min(96, Math.max(4, value * 10));
}

function parseTimeEstimate(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const minutes = Number(trimmed);
  return Number.isInteger(minutes) && minutes > 0 ? minutes : null;
}

function deadlineInputValue(value: string | null): string {
  if (!value) {
    return "";
  }
  const deadline = new Date(value);
  if (Number.isNaN(deadline.getTime())) {
    return "";
  }
  const offsetMs = deadline.getTimezoneOffset() * 60 * 1000;
  return new Date(deadline.getTime() - offsetMs).toISOString().slice(0, 16);
}

// Mirrors URGENCY_ANCHORS in backend/app/main.py; keep the two in sync.
const urgencyAnchors: Array<[number, number]> = [
  [0, 10],
  [3, 9],
  [12, 8],
  [24, 7],
  [72, 6],
  [168, 5],
  [336, 4],
  [720, 3],
  [1440, 2],
  [2160, 1],
];

function urgencyForDeadline(deadline: string): number {
  const hoursUntilDeadline = (new Date(deadline).getTime() - Date.now()) / 3600000;
  if (Number.isNaN(hoursUntilDeadline)) {
    return 1;
  }
  if (hoursUntilDeadline <= 0) {
    return 10;
  }
  let [previousHours, previousUrgency] = urgencyAnchors[0];
  for (const [anchorHours, anchorUrgency] of urgencyAnchors.slice(1)) {
    if (hoursUntilDeadline <= anchorHours) {
      const position = (hoursUntilDeadline - previousHours) / (anchorHours - previousHours);
      return previousUrgency + (anchorUrgency - previousUrgency) * position;
    }
    [previousHours, previousUrgency] = [anchorHours, anchorUrgency];
  }
  return 1;
}

function taskPayloadFromDraft(draft: TaskDraft) {
  const minutes = parseTimeEstimate(draft.time_estimate_minutes);
  if (minutes === null) {
    throw new Error("Enter a positive whole number of minutes.");
  }
  return {
    ...draft,
    deadline_at: draft.deadline_at ? new Date(draft.deadline_at).toISOString() : null,
    start_window_at: draft.start_window_at ? new Date(draft.start_window_at).toISOString() : null,
    time_estimate_minutes: minutes,
  };
}

function draftFromTask(task: Task): TaskDraft {
  return {
    title: task.title,
    category_id: task.category_id,
    importance: task.importance,
    difficulty: task.difficulty,
    deadline_at: deadlineInputValue(task.deadline_at),
    start_window_at: deadlineInputValue(task.start_window_at),
    dependency_ids: task.dependency_ids,
    time_estimate_minutes: String(task.time_estimate_minutes),
  };
}

function sortedCategories(categories: Category[]): Category[] {
  return [...categories].sort((a, b) => {
    if (a.parent_id === b.parent_id) {
      return a.sort_order - b.sort_order || a.name.localeCompare(b.name);
    }
    return (a.parent_id ?? 0) - (b.parent_id ?? 0);
  });
}

function childrenOf(categories: Category[], parentId: number | null): Category[] {
  return sortedCategories(categories).filter((category) => category.parent_id === parentId);
}

function categoryById(categories: Category[]): Map<number, Category> {
  return new Map(categories.map((category) => [category.id, category]));
}

function categoryPath(categoryId: number | null, categories: Category[]): string {
  if (categoryId === null) return "None";
  const byId = categoryById(categories);
  const names: string[] = [];
  let current = byId.get(categoryId);
  while (current) {
    names.unshift(current.name);
    current = current.parent_id === null ? undefined : byId.get(current.parent_id);
  }
  return names.length > 0 ? names.join(" > ") : "None";
}

function categoryName(categoryId: number | null, categories: Category[], snapshot?: string | null): string {
  if (categoryId === null) {
    return snapshot ?? "None";
  }
  return categoryById(categories).get(categoryId)?.name ?? snapshot ?? "None";
}

function descendantCategoryIds(categoryId: number, categories: Category[]): Set<number> {
  const ids = new Set<number>([categoryId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const category of categories) {
      if (category.parent_id !== null && ids.has(category.parent_id) && !ids.has(category.id)) {
        ids.add(category.id);
        changed = true;
      }
    }
  }
  return ids;
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
        {label} <strong>{formatMetric(value)}</strong>
      </span>
      <input
        type="range"
        min="0"
        max="10"
        step="0.01"
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function UrgencyInput({
  deadline,
  onDeadlineChange,
}: {
  deadline: string;
  onDeadlineChange: (value: string) => void;
}) {
  return (
    <div className="axis-control urgency-control">
      <div className="urgency-control-header">
        <span>
          Urgency <strong>{formatMetric(deadline ? urgencyForDeadline(deadline) : 1)}</strong>
        </span>
        {deadline ? (
          <div className="mode-toggle">
            <button type="button" onClick={() => onDeadlineChange("")}>
              Clear
            </button>
          </div>
        ) : null}
      </div>
      <input
        type="datetime-local"
        value={deadline}
        onChange={(event) => onDeadlineChange(event.target.value)}
      />
    </div>
  );
}

function StartWindowInput({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const [expanded, setExpanded] = useState(Boolean(value));

  useEffect(() => {
    if (value) {
      setExpanded(true);
    }
  }, [value]);

  return (
    <div className="field start-window-field">
      <div className="start-window-header">
        <span>Start Window</span>
        <button
          className="start-window-toggle"
          type="button"
          onClick={() => {
            if (expanded) {
              onChange("");
              setExpanded(false);
            } else {
              setExpanded(true);
            }
          }}
          title={expanded ? "Remove start window" : "Add start window"}
        >
          {expanded ? <Minus size={14} /> : <Plus size={14} />}
        </button>
      </div>
      {expanded && (
        <input
          type="datetime-local"
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </div>
  );
}

function DependencyPicker({
  value,
  tasks,
  currentTaskId,
  onChange,
}: {
  value: number[];
  tasks: Task[];
  currentTaskId?: number;
  onChange: (dependencyIds: number[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(value.length > 0);
  const pickerRef = useRef<HTMLDivElement | null>(null);
  const options = tasks
    .filter((task) => task.status === "active" && task.id !== currentTaskId)
    .sort((a, b) => a.title.localeCompare(b.title));
  const optionIds = new Set(options.map((task) => task.id));
  const selectedTasks = value
    .map((id) => options.find((task) => task.id === id))
    .filter((task): task is Task => task !== undefined);
  const filteredOptions = options.filter((task) =>
    task.title.toLowerCase().includes(query.trim().toLowerCase()),
  );

  useEffect(() => {
    const validValue = value.filter((id) => optionIds.has(id));
    if (validValue.length !== value.length) {
      onChange(validValue);
    }
  }, [tasks, currentTaskId]);

  useEffect(() => {
    if (value.length > 0) {
      setExpanded(true);
    }
  }, [value.length]);

  useEffect(() => {
    if (!open) {
      return;
    }
    function closeOnOutsideClick(event: MouseEvent) {
      if (!pickerRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setQuery("");
      }
    }
    document.addEventListener("mousedown", closeOnOutsideClick);
    return () => document.removeEventListener("mousedown", closeOnOutsideClick);
  }, [open]);

  function toggleDependency(taskId: number) {
    onChange(
      value.includes(taskId)
        ? value.filter((id) => id !== taskId)
        : [...value, taskId],
    );
  }

  return (
    <div className="field dependency-field">
      <div className="dependency-header">
        <span>Depends On</span>
        <button
          className="dependency-toggle"
          type="button"
          onClick={() => {
            if (expanded) {
              onChange([]);
              setQuery("");
              setOpen(false);
              setExpanded(false);
            } else {
              setExpanded(true);
              setOpen(true);
            }
          }}
          title={expanded ? "Remove dependencies" : "Add dependencies"}
        >
          {expanded ? <Minus size={14} /> : <Plus size={14} />}
        </button>
      </div>
      {expanded && (
        <div className="dependency-picker" ref={pickerRef}>
          <div className="dependency-input-shell" onClick={() => setOpen(true)}>
            {selectedTasks.map((task) => (
              <button
                className="dependency-chip"
                key={task.id}
                type="button"
                onClick={() => toggleDependency(task.id)}
                title={`Remove ${task.title}`}
              >
                {task.title}
                <X size={12} />
              </button>
            ))}
            <input
              type="text"
              value={query}
              onFocus={() => setOpen(true)}
              onChange={(event) => {
                setQuery(event.target.value);
                setOpen(true);
              }}
              placeholder={selectedTasks.length === 0 ? "Search tasks..." : ""}
            />
          </div>
          {open && (
            <div className="dependency-popover">
              {filteredOptions.length > 0 ? (
                filteredOptions.map((task) => (
                  <label className="dependency-option" key={task.id}>
                    <input
                      type="checkbox"
                      checked={value.includes(task.id)}
                      onChange={() => toggleDependency(task.id)}
                    />
                    <span>{task.title}</span>
                  </label>
                ))
              ) : (
                <p>No matching tasks.</p>
              )}
              <div className="dependency-actions">
                <button
                  className="secondary"
                  type="button"
                  onClick={() => {
                    setQuery("");
                    onChange([]);
                  }}
                >
                  Clear
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    setQuery("");
                  }}
                >
                  Done
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CategoryPicker({
  value,
  categories,
  onChange,
}: {
  value: number | null;
  categories: Category[];
  onChange: (categoryId: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pendingCategoryId, setPendingCategoryId] = useState<number | null>(value);
  const [browsingParentId, setBrowsingParentId] = useState<number | null>(null);
  const browsingCategory =
    browsingParentId === null ? null : categoryById(categories).get(browsingParentId);

  function openPicker() {
    const selectedCategory = value === null ? undefined : categoryById(categories).get(value);
    setPendingCategoryId(value);
    setBrowsingParentId(selectedCategory?.parent_id ?? null);
    setOpen(true);
  }

  function goUp() {
    if (browsingCategory) {
      setPendingCategoryId(browsingCategory.id);
      setBrowsingParentId(browsingCategory.parent_id);
    }
  }

  return (
    <div className="category-picker">
      <button className="category-picker-trigger" type="button" onClick={openPicker}>
        {categoryPath(open ? pendingCategoryId : value, categories)}
      </button>
      {open && (
        <div className="category-picker-popover">
          <CategoryPickerLevel
            browsingParentId={browsingParentId}
            categories={categories}
            pendingCategoryId={pendingCategoryId}
            onBack={goUp}
            onBrowse={(categoryId) => {
              setPendingCategoryId(categoryId);
              setBrowsingParentId(categoryId);
            }}
            onSelect={setPendingCategoryId}
          />
          <div className="category-picker-actions">
            <button className="secondary" type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
            <button
              type="button"
              onClick={() => {
                onChange(pendingCategoryId);
                setOpen(false);
              }}
            >
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function CategoryPickerLevel({
  browsingParentId,
  categories,
  pendingCategoryId,
  onBack,
  onBrowse,
  onSelect,
}: {
  browsingParentId: number | null;
  categories: Category[];
  pendingCategoryId: number | null;
  onBack: () => void;
  onBrowse: (categoryId: number) => void;
  onSelect: (categoryId: number) => void;
}) {
  const visibleCategories = childrenOf(categories, browsingParentId);

  return (
    <div className="category-picker-level">
      {browsingParentId !== null ? (
        <button className="category-picker-option category-picker-back" type="button" onClick={onBack}>
          <ChevronLeft size={15} />
        </button>
      ) : (
        <div className="category-picker-option category-picker-back category-picker-back-spacer" />
      )}
      {visibleCategories.map((category) => {
        const hasChildren = childrenOf(categories, category.id).length > 0;
        return (
          <div
            className={
              pendingCategoryId === category.id
                ? "category-picker-option selected"
                : "category-picker-option"
            }
            key={category.id}
          >
            <button
              className="category-picker-select"
              type="button"
              onClick={() => onSelect(category.id)}
            >
              <span>{category.name}</span>
            </button>
            {hasChildren && (
              <button
                aria-label={`Open ${category.name}`}
                className="category-picker-arrow"
                type="button"
                onClick={() => onBrowse(category.id)}
              >
                <ChevronRight size={15} />
              </button>
            )}
          </div>
        );
      })}
      {visibleCategories.length === 0 && (
        <p className="category-picker-empty">No categories here.</p>
      )}
    </div>
  );
}

function CategoryFilterPicker({
  value,
  categories,
  onChange,
}: {
  value: number | null;
  categories: Category[];
  onChange: (categoryId: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pendingCategoryId, setPendingCategoryId] = useState<number | null>(value);
  const [browsingParentId, setBrowsingParentId] = useState<number | null>(null);
  const browsingCategory =
    browsingParentId === null ? null : categoryById(categories).get(browsingParentId);

  function openPicker() {
    const selectedCategory = value === null ? undefined : categoryById(categories).get(value);
    setPendingCategoryId(value);
    setBrowsingParentId(selectedCategory?.parent_id ?? null);
    setOpen(true);
  }

  function goUp() {
    if (browsingCategory) {
      setPendingCategoryId(browsingCategory.id);
      setBrowsingParentId(browsingCategory.parent_id);
    }
  }

  return (
    <div className="category-picker category-filter-picker">
      <button className="header-label category-filter-trigger" type="button" onClick={openPicker}>
        Category
      </button>
      {open && (
        <div className="category-picker-popover category-filter-popover">
          <div className="category-filter-breadcrumb">
            {pendingCategoryId === null ? "All categories" : categoryPath(pendingCategoryId, categories)}
          </div>
          <CategoryPickerLevel
            browsingParentId={browsingParentId}
            categories={categories}
            pendingCategoryId={pendingCategoryId}
            onBack={goUp}
            onBrowse={(categoryId) => {
              setPendingCategoryId(categoryId);
              setBrowsingParentId(categoryId);
            }}
            onSelect={setPendingCategoryId}
          />
          <div className="category-picker-actions category-filter-actions">
            <button
              className="secondary"
              type="button"
              onClick={() => {
                onChange(null);
                setOpen(false);
              }}
            >
              Clear
            </button>
            <div className="category-filter-commit-actions">
              <button className="secondary" type="button" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button
                type="button"
                onClick={() => {
                  onChange(pendingCategoryId);
                  setOpen(false);
                }}
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function TaskForm({
  draft,
  categories,
  dependencyOptions,
  currentTaskId,
  submitLabel,
  canDelete,
  onChange,
  onErrorChange,
  onDelete,
  onFinish,
  onStart,
  onSubmit,
  onCancel,
}: {
  draft: TaskDraft;
  categories: Category[];
  dependencyOptions: Task[];
  currentTaskId?: number;
  submitLabel: string;
  canDelete?: boolean;
  onChange: (draft: TaskDraft) => void;
  onErrorChange: (error: string) => void;
  onDelete?: () => void;
  onFinish?: (minutes: number | null) => void;
  onStart?: () => void;
  onSubmit: () => void;
  onCancel?: () => void;
}) {
  const [finishPromptOpen, setFinishPromptOpen] = useState(false);
  const [finishMinutes, setFinishMinutes] = useState("");

  function validateEstimate(): boolean {
    if (parseTimeEstimate(draft.time_estimate_minutes) === null) {
      onErrorChange("Enter a positive whole number of minutes.");
      return false;
    }
    onErrorChange("");
    return true;
  }

  function openFinishPrompt() {
    // Seed with the estimate, which is usually close to the real answer.
    setFinishMinutes(draft.time_estimate_minutes);
    setFinishPromptOpen(true);
  }

  function confirmFinish() {
    const minutes = parseTimeEstimate(finishMinutes);
    setFinishPromptOpen(false);
    onFinish?.(minutes);
  }

  return (
    <form
      className="task-form"
      onSubmit={(event) => {
        event.preventDefault();
      }}
    >
      <label className="field">
        <span>Title</span>
        <input
          value={draft.title}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
            }
          }}
          onChange={(event) => onChange({ ...draft, title: event.target.value })}
          placeholder="Describe the task..."
        />
      </label>
      <div className="field">
        <span>Category</span>
        <CategoryPicker
          value={draft.category_id}
          categories={categories}
          onChange={(categoryId) =>
            onChange({
              ...draft,
              category_id: categoryId,
            })
          }
        />
      </div>
      <div className="axis-grid">
        <AxisInput
          label="Importance"
          value={draft.importance}
          onChange={(importance) => onChange({ ...draft, importance })}
        />
        <UrgencyInput
          deadline={draft.deadline_at}
          onDeadlineChange={(deadline_at) => onChange({ ...draft, deadline_at })}
        />
        <AxisInput
          label="Difficulty"
          value={draft.difficulty}
          onChange={(difficulty) => onChange({ ...draft, difficulty })}
        />
      </div>
      <label className="field compact-field">
        <span>Time Estimate</span>
        <input
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          value={draft.time_estimate_minutes}
          onBlur={validateEstimate}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              validateEstimate();
            }
          }}
          onChange={(event) => {
            const nextValue = event.target.value;
            if (/^\d*$/.test(nextValue)) {
              onErrorChange("");
              onChange({ ...draft, time_estimate_minutes: nextValue });
            }
          }}
        />
        <span>min</span>
      </label>
      <StartWindowInput
        value={draft.start_window_at}
        onChange={(start_window_at) => onChange({ ...draft, start_window_at })}
      />
      <DependencyPicker
        value={draft.dependency_ids}
        tasks={dependencyOptions}
        currentTaskId={currentTaskId}
        onChange={(dependency_ids) => onChange({ ...draft, dependency_ids })}
      />
      <div className="form-actions">
        {canDelete && onDelete && (
          <button className="danger" type="button" onClick={onDelete}>
            Archive
          </button>
        )}
        {onFinish && (
          <button className="secondary" type="button" onClick={openFinishPrompt}>
            Finish
          </button>
        )}
        {onCancel && (
          <button className="secondary" type="button" onClick={onCancel}>
            Cancel
          </button>
        )}
        {onStart && (
          <button
            className="start-task-button"
            type="button"
            onClick={() => {
              if (validateEstimate()) {
                onStart();
              }
            }}
          >
            Start
          </button>
        )}
        <button
          className="outlined"
          type="button"
          onClick={() => {
            if (validateEstimate()) {
              onSubmit();
            }
          }}
        >
          {submitLabel}
        </button>
      </div>
      {finishPromptOpen && (
        <div className="modal-backdrop" role="presentation">
          <section className="modal-panel warning-modal" role="dialog" aria-modal="true">
            <div className="modal-heading">
              <h2>Finish Task</h2>
              <button
                className="icon-button"
                type="button"
                title="Close finish prompt"
                onClick={() => setFinishPromptOpen(false)}
              >
                <X size={16} />
              </button>
            </div>
            <label className="field compact-field">
              <span>Time Taken</span>
              <input
                type="text"
                inputMode="numeric"
                pattern="[0-9]*"
                autoFocus
                value={finishMinutes}
                onChange={(event) => {
                  if (/^\d*$/.test(event.target.value)) {
                    setFinishMinutes(event.target.value);
                  }
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    confirmFinish();
                  }
                }}
              />
              <span>min</span>
            </label>
            <div className="form-actions">
              <button type="button" onClick={confirmFinish}>
                Finish
              </button>
            </div>
          </section>
        </div>
      )}
    </form>
  );
}

function TaskTable({
  tasks,
  categories,
  categoryFilter,
  highlightedTaskId,
  scrollToTaskId,
  onAdd,
  onEdit,
  onHoverTask,
  onCategoryFilterChange,
}: {
  tasks: Task[];
  categories: Category[];
  categoryFilter: number | null;
  highlightedTaskId: number | null;
  scrollToTaskId: number | null;
  onAdd: () => void;
  onEdit: (task: Task) => void;
  onHoverTask: (taskId: number | null) => void;
  onCategoryFilterChange: (categoryId: number | null) => void;
}) {
  const [sortKey, setSortKey] = useState<TaskSortKey>("urgency");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [urgencyDisplay, setUrgencyDisplay] = useState<"value" | "deadline">("deadline");
  const rowRefs = useRef(new Map<number, HTMLTableRowElement>());
  const sorted = [...tasks].sort((a, b) => {
    const leftValue = a[sortKey];
    const rightValue = b[sortKey];
    const delta = leftValue - rightValue;
    return sortDir === "asc" ? delta : -delta;
  });
  const stripeByDate = new Map<string, "date-stripe-a" | "date-stripe-b">();
  for (const task of sorted) {
    const key = dateStripeKey(task.deadline_at);
    if (!stripeByDate.has(key)) {
      stripeByDate.set(key, stripeByDate.size % 2 === 0 ? "date-stripe-a" : "date-stripe-b");
    }
  }

  function toggleSort(key: TaskSortKey) {
    if (key === "urgency") {
      setSortKey("urgency");
      setSortDir("desc");
      setUrgencyDisplay((current) => (current === "value" ? "deadline" : "value"));
      return;
    }
    if (key !== sortKey) {
      setSortKey(key);
      setSortDir("desc");
    } else if (sortDir === "desc") {
      setSortDir("asc");
    } else {
      setSortKey("urgency");
      setSortDir("desc");
    }
  }

  useEffect(() => {
    if (scrollToTaskId === null) {
      return;
    }
    rowRefs.current.get(scrollToTaskId)?.scrollIntoView({
      block: "nearest",
      behavior: "smooth",
    });
  }, [scrollToTaskId, sorted]);

  return (
    <div className="table-stack">
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>
                <span className="header-label">Task</span>
              </th>
              <th>
                <CategoryFilterPicker
                  value={categoryFilter}
                  categories={categories}
                  onChange={onCategoryFilterChange}
                />
              </th>
              <th className="metric-header">
                <button className="sort-button" onClick={() => toggleSort("urgency")}>
                  urgency {sortKey === "urgency" ? "↓" : ""}
                </button>
              </th>
              {(["importance", "difficulty"] as const).map((key) => (
                <th className="metric-header" key={key}>
                  <button className="sort-button" onClick={() => toggleSort(key)}>
                    {key} {sortKey === key ? (sortDir === "asc" ? "↑" : "↓") : ""}
                  </button>
                </th>
              ))}
              <th className="time-header" title="Time estimate" aria-label="Time estimate">
                <button className="sort-button icon-sort-button" onClick={() => toggleSort("time_estimate_minutes")}>
                  <Clock size={14} />
                  {sortKey === "time_estimate_minutes" ? (sortDir === "asc" ? "↑" : "↓") : ""}
                </button>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr className="add-task-row" onClick={onAdd}>
              <td colSpan={6}>
                <button className="add-task-button" type="button">
                  <Plus size={15} /> Add a task...
                </button>
              </td>
            </tr>
            {sorted.map((task) => (
              <tr
                className={[
                  "task-row",
                  stripeByDate.get(dateStripeKey(task.deadline_at)),
                  highlightedTaskId === task.id ? "highlighted" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                key={task.id}
                ref={(node) => {
                  if (node) {
                    rowRefs.current.set(task.id, node);
                  } else {
                    rowRefs.current.delete(task.id);
                  }
                }}
                onClick={() => onEdit(task)}
                onMouseEnter={() => onHoverTask(task.id)}
                onMouseLeave={() => onHoverTask(null)}
              >
                <td>{task.title}</td>
                <td className="category-cell">{categoryName(task.category_id, categories)}</td>
                <td className={urgencyDisplay === "deadline" ? "metric-cell date-cell" : "metric-cell"}>
                  {urgencyDisplay === "deadline" ? formatDate(task.deadline_at) : formatMetric(task.urgency)}
                </td>
                <td className="metric-cell">{formatMetric(task.importance)}</td>
                <td className="metric-cell">
                  <span
                    className="difficulty-dot"
                    style={{ background: difficultyColor(task.difficulty) }}
                  />
                  {formatMetric(task.difficulty)}
                </td>
                <td className="time-cell">{task.time_estimate_minutes}m</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {tasks.length === 0 && <p className="empty">No active tasks.</p>}
    </div>
  );
}

function MatrixView({
  tasks,
  categories,
  highlightedTaskId,
  onHoverTask,
}: {
  tasks: Task[];
  categories: Category[];
  highlightedTaskId: number | null;
  onHoverTask: (taskId: number | null) => void;
}) {
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const [matrixWidth, setMatrixWidth] = useState(0);
  const matrixRef = useRef<HTMLDivElement | null>(null);
  const taskPoints = new Map(
    tasks.map((task) => [
      task.id,
      {
        x: matrixPercent(task.urgency),
        y: matrixPercent(task.importance),
      },
    ]),
  );
  const dependencyArrows = tasks.flatMap((task) => {
    const target = taskPoints.get(task.id);
    if (!target) {
      return [];
    }
    return task.dependency_ids
      .map((dependencyId) => {
        const source = taskPoints.get(dependencyId);
        if (!source || (source.x === target.x && source.y === target.y)) {
          return null;
        }
        const sourceY = 100 - source.y;
        const targetY = 100 - target.y;
        const dx = target.x - source.x;
        const dy = targetY - sourceY;
        const length = Math.hypot(dx, dy);
        const offset = Math.min(2, length / 3);
        return {
          id: `${dependencyId}-${task.id}`,
          x1: source.x + (dx / length) * offset,
          y1: sourceY + (dy / length) * offset,
          x2: target.x - (dx / length) * offset,
          y2: targetY - (dy / length) * offset,
        };
      })
      .filter((arrow): arrow is { id: string; x1: number; y1: number; x2: number; y2: number } => arrow !== null);
  });

  useLayoutEffect(() => {
    if (!matrixRef.current) return;

    const updateWidth = () => setMatrixWidth(matrixRef.current?.clientWidth ?? 0);
    updateWidth();

    const observer = new ResizeObserver(updateWidth);
    observer.observe(matrixRef.current);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="matrix-shell">
      <div className="y-label">Importance</div>
      <div className="matrix" ref={matrixRef}>
        <span className="matrix-origin-tick">0</span>
        {axisTicks.map((tick) => (
          <span
            className={tick === 10 ? "matrix-x-tick matrix-x-tick-max" : "matrix-x-tick"}
            style={{ left: `${tick * 10}%` }}
            key={`x-${tick}`}
          >
            {tick}
          </span>
        ))}
        {axisTicks.map((tick) => (
          <span
            className={tick === 10 ? "matrix-y-tick matrix-y-tick-max" : "matrix-y-tick"}
            style={{ bottom: `${tick * 10}%` }}
            key={`y-${tick}`}
          >
            {tick}
          </span>
        ))}
        {dependencyArrows.length > 0 && (
          <svg className="matrix-dependency-layer" viewBox="0 0 100 100" preserveAspectRatio="none">
            <defs>
              <marker
                id="dependency-arrowhead"
                markerHeight="5"
                markerWidth="5"
                orient="auto"
                refX="4.5"
                refY="2.5"
                viewBox="0 0 5 5"
              >
                <path d="M0,0 L5,2.5 L0,5 Z" />
              </marker>
            </defs>
            {dependencyArrows.map((arrow) => (
              <line
                key={arrow.id}
                x1={arrow.x1}
                y1={arrow.y1}
                x2={arrow.x2}
                y2={arrow.y2}
                markerEnd="url(#dependency-arrowhead)"
              />
            ))}
          </svg>
        )}
        {tasks.map((task) => {
          const isSelected = selectedTaskId === task.id;
          const isHighlighted = highlightedTaskId === task.id;
          const taskColor = difficultyColor(task.difficulty);
          const taskUrgency = task.urgency;
          const point = taskPoints.get(task.id);
          const xPercent = point?.x ?? matrixPercent(taskUrgency);
          const yPercent = point?.y ?? matrixPercent(task.importance);
          const estimatedPopupWidth = Math.min(
            420,
            Math.max(132, task.title.length * 6.2 + 18),
          );
          const rightSpace = matrixWidth * (1 - xPercent / 100) - 18;
          const shouldExpandLeft = matrixWidth > 0 ? rightSpace < estimatedPopupWidth : xPercent > 88;
          const shouldExpandUp = isSelected && yPercent < 34;
          return (
            <button
              className={[
                "matrix-task",
                isSelected ? "selected" : "",
                isHighlighted ? "highlighted" : "",
                shouldExpandLeft ? "expand-left" : "",
                shouldExpandUp ? "expand-metrics-up" : "",
              ]
                .filter(Boolean)
                .join(" ")}
              title={`${task.title} · urgency ${formatMetric(taskUrgency)} · importance ${formatMetric(task.importance)} · difficulty ${formatMetric(task.difficulty)}`}
              aria-label={isSelected ? `Hide details for ${task.title}` : `Show details for ${task.title}`}
              type="button"
              onClick={() => setSelectedTaskId(isSelected ? null : task.id)}
              style={{
                left: `${xPercent}%`,
                bottom: `${yPercent}%`,
              }}
              onMouseEnter={() => onHoverTask(task.id)}
              onMouseLeave={() => {
                onHoverTask(null);
                setSelectedTaskId(null);
              }}
              key={task.id}
            >
              <span
                className="matrix-task-dot"
                style={{ background: taskColor }}
              />
              <span className="matrix-task-popup">
                <span className="matrix-task-title" style={{ background: taskColor }}>
                  {task.title}
                </span>
                {isSelected && (
                  <span className="matrix-task-metrics-popover">
                    <span>Urgency {formatMetric(taskUrgency)}</span>
                    <span>Importance {formatMetric(task.importance)}</span>
                    <span>Difficulty {formatMetric(task.difficulty)}</span>
                    <span>Category {categoryName(task.category_id, categories)}</span>
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>
      <div className="x-label">Urgency</div>
    </div>
  );
}

function ArchiveRowMenu({
  onEdit,
  onRestore,
  onDelete,
}: {
  onEdit: () => void;
  onRestore: () => void;
  onDelete: () => void;
}) {
  // The archive table scrolls, which would clip an absolutely positioned popover, so the
  // menu is fixed-positioned against the trigger and closed whenever the page moves.
  const [menuPosition, setMenuPosition] = useState<{ top: number; right: number } | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const open = menuPosition !== null;

  useEffect(() => {
    if (!open) {
      return;
    }
    function closeOnOutsideClick(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) {
        setMenuPosition(null);
      }
    }
    function close() {
      setMenuPosition(null);
    }
    document.addEventListener("mousedown", closeOnOutsideClick);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  function toggle() {
    if (open) {
      setMenuPosition(null);
      return;
    }
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      setMenuPosition({ top: rect.bottom + 4, right: window.innerWidth - rect.right });
    }
  }

  return (
    <div className="row-menu" ref={containerRef}>
      <button
        ref={triggerRef}
        className="icon-button"
        type="button"
        title="Task actions"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggle}
      >
        <MoreVertical size={16} />
      </button>
      {menuPosition && (
        <div
          className="row-menu-popover"
          role="menu"
          style={{ top: menuPosition.top, right: menuPosition.right }}
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setMenuPosition(null);
              onEdit();
            }}
          >
            Edit
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setMenuPosition(null);
              onRestore();
            }}
          >
            Restore to active
          </button>
          <button
            className="danger"
            type="button"
            role="menuitem"
            onClick={() => {
              setMenuPosition(null);
              onDelete();
            }}
          >
            Delete permanently
          </button>
        </div>
      )}
    </div>
  );
}

function ArchiveList({
  tasks,
  categories,
  onEdit,
  onRestore,
  onPurge,
}: {
  tasks: Task[];
  categories: Category[];
  onEdit: (task: Task) => void;
  onRestore: (task: Task) => void;
  onPurge: (task: Task) => void;
}) {
  const [pendingPurge, setPendingPurge] = useState<Task | null>(null);

  return (
    <div className="table-wrap archive">
      <table>
        <thead>
          <tr>
            <th>Task</th>
            <th>Category</th>
            <th className="metric-header">Urgency</th>
            <th className="metric-header">Importance</th>
            <th className="metric-header">Difficulty</th>
            <th className="time-header">Estimate</th>
            <th>Actual</th>
            <th>Created</th>
            <th>Archived</th>
            <th className="row-menu-header" aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.id}>
              <td>{task.title}</td>
              <td className="category-cell">
                {categoryName(task.category_id, categories, task.category_snapshot)}
              </td>
              <td className="metric-cell">{formatMetric(task.urgency)}</td>
              <td className="metric-cell">{formatMetric(task.importance)}</td>
              <td className="metric-cell">{formatMetric(task.difficulty)}</td>
              <td className="time-cell">{formatEstimateDuration(task.time_estimate_minutes)}</td>
              <td>{task.actual_duration_seconds === null ? "-" : formatElapsed(task.actual_duration_seconds)}</td>
              <td className="date-cell">{formatDateTime(task.created_at)}</td>
              <td className="date-cell">{formatDateTime(task.archived_at)}</td>
              <td className="row-menu-cell">
                <ArchiveRowMenu
                  onEdit={() => onEdit(task)}
                  onRestore={() => onRestore(task)}
                  onDelete={() => setPendingPurge(task)}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {tasks.length === 0 && <p className="empty">No archived tasks.</p>}
      {pendingPurge && (
        <div className="modal-backdrop" role="presentation">
          <section className="modal-panel warning-modal" role="dialog" aria-modal="true">
            <div className="modal-heading">
              <h2>Delete Permanently</h2>
              <button
                className="icon-button"
                title="Close delete warning"
                onClick={() => setPendingPurge(null)}
              >
                <X size={16} />
              </button>
            </div>
            <p className="subtle">
              "{pendingPurge.title}" will be erased for good. This cannot be undone.
            </p>
            <div className="form-actions">
              <button className="secondary" onClick={() => setPendingPurge(null)}>
                Cancel
              </button>
              <button
                className="danger"
                onClick={() => {
                  onPurge(pendingPurge);
                  setPendingPurge(null);
                }}
              >
                Delete
              </button>
            </div>
          </section>
        </div>
      )}
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
  const taskUrgency = session.task.urgency;

  return (
    <main className="focus-layout">
      <section className="focus-panel">
        <h2>{session.task.title}</h2>
        <div className="timer-card">
          <div className="timer">
            <span>{formatElapsed(elapsed)}</span>
          </div>
          <div className="timer-estimate">Estimate: {session.task.time_estimate_minutes}m</div>
        </div>
        <div className="task-metrics">
          <span>Urgency {formatMetric(taskUrgency)}</span>
          <span>Importance {formatMetric(session.task.importance)}</span>
          <span>Difficulty {formatMetric(session.task.difficulty)}</span>
        </div>
        <div className="focus-actions">
          {canDecline && (
            <button className="secondary" onClick={onDecline}>
              Decline/Edit ({formatElapsed(declineSecondsLeft)})
            </button>
          )}
          <button onClick={onFinish}>
            Finish
          </button>
        </div>
      </section>
    </main>
  );
}

function DeclineEditor({
  session,
  categories,
  dependencyOptions,
  onCancel,
  onSaved,
}: {
  session: Session;
  categories: Category[];
  dependencyOptions: Task[];
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
        body: JSON.stringify({ action: "update", task: taskPayloadFromDraft(draft) }),
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
        {error && <p className="error modal-error">{error}</p>}
        <TaskForm
          draft={draft}
          categories={categories}
          dependencyOptions={dependencyOptions}
          currentTaskId={session.task.id}
          submitLabel="Save edit"
          canDelete
          onChange={setDraft}
          onErrorChange={setError}
          onDelete={deleteTask}
          onSubmit={save}
          onCancel={onCancel}
        />
      </section>
    </main>
  );
}

function NavDrawer({
  open,
  page,
  onNavigate,
  onClose,
}: {
  open: boolean;
  page: "tasks" | "archive" | "categories";
  onNavigate: (page: "tasks" | "archive" | "categories") => void;
  onClose: () => void;
}) {
  if (!open) return null;

  return (
    <>
      <button className="nav-backdrop" aria-label="Close navigation" onClick={onClose} />
      <nav className="nav-drawer" aria-label="Main navigation">
        <div className="nav-heading">
          <strong>Odot</strong>
          <button className="icon-button" title="Close navigation" onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <button
          className={page === "tasks" ? "nav-link active" : "nav-link"}
          onClick={() => onNavigate("tasks")}
        >
          <Home size={17} /> Tasks
        </button>
        <button
          className={page === "categories" ? "nav-link active" : "nav-link"}
          onClick={() => onNavigate("categories")}
        >
          <Folder size={17} /> Categories
        </button>
        <button
          className={page === "archive" ? "nav-link active" : "nav-link"}
          onClick={() => onNavigate("archive")}
        >
          <Archive size={17} /> Archived
        </button>
      </nav>
    </>
  );
}

function TaskModal({
  title,
  draft,
  categories,
  dependencyOptions,
  currentTaskId,
  submitLabel,
  error,
  canDelete,
  onChange,
  onErrorChange,
  onDelete,
  onFinish,
  onStart,
  onSubmit,
  onClose,
}: {
  title: string;
  draft: TaskDraft;
  categories: Category[];
  dependencyOptions: Task[];
  currentTaskId?: number;
  submitLabel: string;
  error: string;
  canDelete: boolean;
  onChange: (draft: TaskDraft) => void;
  onErrorChange: (error: string) => void;
  onDelete: () => void;
  onFinish?: (minutes: number | null) => void;
  onStart?: () => void;
  onSubmit: () => void;
  onClose: () => void;
}) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal-panel" role="dialog" aria-modal="true" aria-labelledby="task-modal-title">
        <div className="modal-heading">
          <h2 id="task-modal-title">{title}</h2>
          <button className="icon-button" title="Close task form" onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        {error && <p className="error modal-error">{error}</p>}
        <TaskForm
          draft={draft}
          categories={categories}
          dependencyOptions={dependencyOptions}
          currentTaskId={currentTaskId}
          submitLabel={submitLabel}
          canDelete={canDelete}
          onChange={onChange}
          onErrorChange={onErrorChange}
          onDelete={onDelete}
          onFinish={onFinish}
          onStart={onStart}
          onSubmit={onSubmit}
        />
      </section>
    </div>
  );
}

type DropHint = {
  targetId: number | null;
  position: "before" | "inside" | "after";
} | null;

function CategoriesPage({
  categories,
  tasks,
  onRefresh,
  onError,
}: {
  categories: Category[];
  tasks: Task[];
  onRefresh: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [newName, setNewName] = useState("");
  const [newParentId, setNewParentId] = useState<number | null>(null);
  const [newAfterId, setNewAfterId] = useState<number | null>(null);
  const [renameId, setRenameId] = useState<number | null>(null);
  const [renameName, setRenameName] = useState("");
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const [dropHint, setDropHint] = useState<DropHint>(null);
  const [collapsedIds, setCollapsedIds] = useState<Set<number>>(new Set());
  const [taskListCategoryId, setTaskListCategoryId] = useState<number | null>(null);
  const [deleteWarning, setDeleteWarning] = useState<{
    category: Category;
    activeTaskCount: number;
  } | null>(null);

  useEffect(() => {
    const ids = new Set(categories.map((category) => category.id));
    setCollapsedIds((current) => new Set([...current].filter((id) => ids.has(id))));
    setTaskListCategoryId((current) => (current !== null && ids.has(current) ? current : null));
  }, [categories]);

  async function createCategory(parentId: number | null) {
    const name = newName.trim();
    if (!name) return;
    onError("");
    try {
      await api<Category>("/categories", {
        method: "POST",
        body: JSON.stringify({ name, parent_id: parentId }),
      });
      setNewName("");
      setNewParentId(null);
      setNewAfterId(null);
      await onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not create category");
    }
  }

  async function saveRename(category: Category) {
    const name = renameName.trim();
    if (!name) return;
    onError("");
    try {
      await api<Category>(`/categories/${category.id}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      setRenameId(null);
      setRenameName("");
      await onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not rename category");
    }
  }

  async function requestDelete(category: Category) {
    onError("");
    try {
      const preview = await api<{ active_task_count: number; category_count: number }>(
        `/categories/${category.id}/delete-preview`,
      );
      if (preview.active_task_count > 0) {
        setDeleteWarning({ category, activeTaskCount: preview.active_task_count });
        return;
      }
      await confirmDelete(category);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not delete category");
    }
  }

  async function confirmDelete(category: Category) {
    onError("");
    try {
      await api<void>(`/categories/${category.id}`, { method: "DELETE" });
      setDeleteWarning(null);
      await onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not delete category");
    }
  }

  function canDrop(draggedId: number, targetId: number | null): boolean {
    if (targetId === null) return true;
    return !descendantCategoryIds(draggedId, categories).has(targetId);
  }

  async function moveCategory(
    categoryId: number,
    targetId: number | null,
    position: "before" | "inside" | "after",
  ) {
    if (!canDrop(categoryId, targetId)) return;
    const target = targetId === null ? null : categoryById(categories).get(targetId);
    let parentId: number | null = null;
    let sortOrder = childrenOf(categories, null).length;

    if (target && position === "inside") {
      parentId = target.id;
      sortOrder = childrenOf(categories, target.id).length;
    } else if (target) {
      parentId = target.parent_id;
      const siblings = childrenOf(categories, parentId).filter((category) => category.id !== categoryId);
      const targetIndex = siblings.findIndex((category) => category.id === target.id);
      sortOrder = targetIndex + (position === "after" ? 1 : 0);
    }

    onError("");
    try {
      await api<Category>(`/categories/${categoryId}`, {
        method: "PATCH",
        body: JSON.stringify({ parent_id: parentId, sort_order: sortOrder }),
      });
      await onRefresh();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Could not move category");
    }
  }

  function dropPosition(event: DragEvent<HTMLElement>): "before" | "inside" | "after" {
    const rect = event.currentTarget.getBoundingClientRect();
    const offset = (event.clientY - rect.top) / rect.height;
    if (offset < 0.25) return "before";
    if (offset > 0.75) return "after";
    return "inside";
  }

  function directTaskCount(categoryId: number): number {
    return tasks.filter((task) => task.category_id === categoryId).length;
  }

  function directTasks(categoryId: number): Task[] {
    return tasks.filter((task) => task.category_id === categoryId);
  }

  function expandableCategoryIds(): number[] {
    return categories
      .filter((category) => childrenOf(categories, category.id).length > 0)
      .map((category) => category.id);
  }

  function toggleCategory(categoryId: number) {
    setCollapsedIds((current) => {
      const next = new Set(current);
      if (next.has(categoryId)) {
        next.delete(categoryId);
      } else {
        next.add(categoryId);
      }
      return next;
    });
  }

  function expandAllCategories() {
    setCollapsedIds(new Set());
  }

  function collapseAllCategories() {
    setCollapsedIds(new Set(expandableCategoryIds()));
  }

  function renderAddRow(parentId: number | null, depth: number): ReactNode {
    const isCreatingHere = newParentId === parentId && newAfterId === -1;
    if (isCreatingHere) {
      return (
        <div
          className="category-create-row inline-create-row"
          style={{ marginLeft: `${depth * 18 + 10}px` }}
          key={`create-${parentId ?? "root"}`}
        >
          <input
            maxLength={32}
            value={newName}
            autoFocus
            placeholder="Category name"
            onChange={(event) => setNewName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") createCategory(parentId);
              if (event.key === "Escape") {
                setNewParentId(null);
                setNewAfterId(null);
              }
            }}
          />
          <button
            className="secondary mini-button"
            onClick={() => {
              setNewParentId(null);
              setNewAfterId(null);
            }}
          >
            Cancel
          </button>
          <button className="mini-button" onClick={() => createCategory(parentId)}>
            Add
          </button>
        </div>
      );
    }

    return (
      <button
        className={[
          "category-insert-row",
          categories.length === 0 && parentId === null ? "empty-root-insert" : "",
        ]
          .filter(Boolean)
          .join(" ")}
        style={{ paddingLeft: `${depth * 18 + 10}px` }}
        onClick={() => {
          setNewParentId(parentId);
          setNewAfterId(-1);
          setNewName("");
        }}
        key={`add-${parentId ?? "root"}`}
      >
        <Plus size={14} />
        <span>Add category</span>
      </button>
    );
  }

  function renderRows(parentId: number | null, depth = 0): ReactNode {
    const levelCategories = childrenOf(categories, parentId);
    const showAddRow =
      parentId === null ||
      levelCategories.length > 0 ||
      (newParentId === parentId && newAfterId === -1);
    return (
      <>
        {levelCategories.map((category) => {
          const categoryTasks = directTasks(category.id);
          const taskCount = categoryTasks.length;
          const childCategories = childrenOf(categories, category.id);
          const hasChildren = childCategories.length > 0;
          const isCollapsed = collapsedIds.has(category.id);
          const isRenaming = renameId === category.id;
          const isDropTarget = dropHint?.targetId === category.id;
          const isTaskListOpen = taskListCategoryId === category.id;
          return (
            <div className="category-branch" key={category.id}>
              <div
                className={[
                  "category-row",
                  isDropTarget ? `drop-${dropHint.position}` : "",
                  draggingId === category.id ? "dragging" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                draggable
                onDragStart={() => setDraggingId(category.id)}
                onDragEnd={() => {
                  setDraggingId(null);
                  setDropHint(null);
                }}
                onDragOver={(event) => {
                  if (draggingId === null || draggingId === category.id || !canDrop(draggingId, category.id)) return;
                  event.preventDefault();
                  setDropHint({ targetId: category.id, position: dropPosition(event) });
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  if (draggingId !== null && draggingId !== category.id) {
                    moveCategory(draggingId, category.id, dropPosition(event));
                  }
                  setDraggingId(null);
                  setDropHint(null);
                }}
                onClick={() => {
                  if (hasChildren && !isRenaming) {
                    toggleCategory(category.id);
                  }
                }}
                style={{ paddingLeft: `${depth * 18 + 10}px` }}
              >
                {hasChildren && !isCollapsed ? <FolderOpen size={15} /> : <Folder size={15} />}
                {isRenaming ? (
                  <input
                    className="category-name-input"
                    maxLength={32}
                    value={renameName}
                    autoFocus
                    onClick={(event) => event.stopPropagation()}
                    onChange={(event) => setRenameName(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") saveRename(category);
                      if (event.key === "Escape") setRenameId(null);
                    }}
                  />
                ) : (
                  <span className="category-name">{category.name}</span>
                )}
                <div className="category-meta">
                  <div className="category-actions">
                    {isRenaming ? (
                      <>
                      <button
                        className="secondary mini-button"
                        onClick={(event) => {
                          event.stopPropagation();
                          setRenameId(null);
                        }}
                      >
                        Cancel
                      </button>
                      <button
                        className="mini-button"
                        onClick={(event) => {
                          event.stopPropagation();
                          saveRename(category);
                        }}
                      >
                        Save
                      </button>
                      </>
                    ) : (
                      <>
                      <button
                        className="icon-button category-action-button"
                        title="Rename category"
                        onClick={(event) => {
                          event.stopPropagation();
                          setRenameId(category.id);
                          setRenameName(category.name);
                        }}
                      >
                        <Edit3 size={15} />
                      </button>
                      <button
                        className="icon-button danger category-action-button"
                        title="Delete category"
                        onClick={(event) => {
                          event.stopPropagation();
                          requestDelete(category);
                        }}
                      >
                        <Trash2 size={15} />
                      </button>
                      </>
                    )}
                  </div>
                  <button
                    className="category-count"
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation();
                      setTaskListCategoryId(isTaskListOpen ? null : category.id);
                    }}
                  >
                    {taskCount}
                  </button>
                  {isTaskListOpen && (
                    <div className="category-task-popup" onClick={(event) => event.stopPropagation()}>
                      <div className="category-task-popup-heading">
                        <span>{category.name}</span>
                        <button
                          className="category-task-popup-close"
                          type="button"
                          title="Close task list"
                          onClick={() => setTaskListCategoryId(null)}
                        >
                          <X size={13} />
                        </button>
                      </div>
                      {categoryTasks.length > 0 ? (
                        <ul>
                          {categoryTasks.map((task) => (
                            <li key={task.id}>{task.title}</li>
                          ))}
                        </ul>
                      ) : (
                        <p>No tasks.</p>
                      )}
                    </div>
                  )}
                </div>
              </div>
              {!isCollapsed && renderRows(category.id, depth + 1)}
            </div>
          );
        })}
        {showAddRow && renderAddRow(parentId, depth)}
      </>
    );
  }

  return (
    <section className="main-panel categories-panel">
      <div className="panel-heading">
        <h2>Categories</h2>
        <div className="category-expand-actions">
          <button className="secondary category-expand-button" onClick={expandAllCategories}>
            Expand all
          </button>
          <button className="secondary category-expand-button" onClick={collapseAllCategories}>
            Collapse all
          </button>
        </div>
      </div>

      <div
        className={dropHint?.targetId === null ? "category-root-drop active" : "category-root-drop"}
        onDragOver={(event) => {
          if (draggingId === null) return;
          event.preventDefault();
          setDropHint({ targetId: null, position: "inside" });
        }}
        onDrop={(event) => {
          event.preventDefault();
          if (draggingId !== null) moveCategory(draggingId, null, "inside");
          setDraggingId(null);
          setDropHint(null);
        }}
      >
        Drop here for top level
      </div>

      <div className="category-tree">{renderRows(null)}</div>

      {deleteWarning && (
        <div className="modal-backdrop" role="presentation">
          <section className="modal-panel warning-modal" role="dialog" aria-modal="true">
            <div className="modal-heading">
              <h2>Delete Category</h2>
              <button className="icon-button" title="Close delete warning" onClick={() => setDeleteWarning(null)}>
                <X size={16} />
              </button>
            </div>
            <p className="subtle">
              {deleteWarning.activeTaskCount} active task
              {deleteWarning.activeTaskCount === 1 ? "" : "s"} will move to None.
            </p>
            <div className="form-actions">
              <button className="secondary" onClick={() => setDeleteWarning(null)}>
                Cancel
              </button>
              <button className="danger" onClick={() => confirmDelete(deleteWarning.category)}>
                Delete
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}

function App() {
  const [activeTasks, setActiveTasks] = useState<Task[]>([]);
  const [archivedTasks, setArchivedTasks] = useState<Task[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [draft, setDraft] = useState<TaskDraft>(emptyDraft);
  const [editingTask, setEditingTask] = useState<Task | null>(null);
  const [energyLevel, setEnergyLevel] = useState(5);
  const [page, setPage] = useState<"tasks" | "archive" | "categories">("tasks");
  const [categoryFilter, setCategoryFilter] = useState<number | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [taskModalOpen, setTaskModalOpen] = useState(false);
  const [declineEditing, setDeclineEditing] = useState(false);
  const [error, setError] = useState("");
  const [taskModalError, setTaskModalError] = useState("");
  const [hoveredTaskId, setHoveredTaskId] = useState<number | null>(null);
  const [matrixHoveredTaskId, setMatrixHoveredTaskId] = useState<number | null>(null);

  async function refresh() {
    const [active, archived, currentSession, nextCategories] = await Promise.all([
      api<Task[]>("/tasks?status=active"),
      api<Task[]>("/tasks?status=archived"),
      api<Session | null>("/session"),
      api<Category[]>("/categories"),
    ]);
    setActiveTasks(active);
    setArchivedTasks(archived);
    setSession(currentSession);
    setCategories(nextCategories);
  }

  useEffect(() => {
    refresh().catch((err) => setError(err instanceof Error ? err.message : "Could not load tasks"));
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => {
      refresh().catch((err) => setError(err instanceof Error ? err.message : "Could not refresh tasks"));
    }, 60_000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    if (
      typeof categoryFilter === "number" &&
      !categories.some((category) => category.id === categoryFilter)
    ) {
      setCategoryFilter(null);
    }
  }, [categories, categoryFilter]);

  async function submitTask() {
    setTaskModalError("");
    try {
      if (editingTask) {
        await api<Task>(`/tasks/${editingTask.id}`, {
          method: "PATCH",
          body: JSON.stringify(taskPayloadFromDraft(draft)),
        });
        setEditingTask(null);
      } else {
        await api<Task>("/tasks", {
          method: "POST",
          body: JSON.stringify(taskPayloadFromDraft(draft)),
        });
      }
      setDraft(emptyDraft);
      setTaskModalOpen(false);
      await refresh();
    } catch (err) {
      setTaskModalError(err instanceof Error ? err.message : "Could not save task");
    }
  }

  async function startEditingTask() {
    if (!editingTask) return;
    setTaskModalError("");
    try {
      await api<Task>(`/tasks/${editingTask.id}`, {
        method: "PATCH",
        body: JSON.stringify(taskPayloadFromDraft(draft)),
      });
      const started = await api<Session>(`/tasks/${editingTask.id}/start`, { method: "POST" });
      setSession(started);
      setDeclineEditing(false);
      setTaskModalOpen(false);
      setEditingTask(null);
      setDraft(emptyDraft);
      await refresh();
    } catch (err) {
      setTaskModalError(err instanceof Error ? err.message : "Could not start task");
    }
  }

  function openNewTaskModal() {
    setEditingTask(null);
    setDraft(emptyDraft);
    setTaskModalError("");
    setTaskModalOpen(true);
  }

  function openEditTaskModal(task: Task) {
    setEditingTask(task);
    setDraft(draftFromTask(task));
    setTaskModalError("");
    setTaskModalOpen(true);
  }

  function closeTaskModal() {
    setTaskModalOpen(false);
    setEditingTask(null);
    setDraft(emptyDraft);
    setTaskModalError("");
  }

  async function deleteTask(
    task: Task,
    setTargetError: (message: string) => void = setError,
  ): Promise<boolean> {
    setTargetError("");
    try {
      await api<void>(`/tasks/${task.id}`, { method: "DELETE" });
      await refresh();
      return true;
    } catch (err) {
      setTargetError(err instanceof Error ? err.message : "Could not delete task");
      return false;
    }
  }

  async function finishEditingTask(minutes: number | null) {
    if (!editingTask) return;
    setTaskModalError("");
    try {
      await api<Task>(`/tasks/${editingTask.id}/complete`, {
        method: "POST",
        body: JSON.stringify({ actual_duration_minutes: minutes }),
      });
      closeTaskModal();
      await refresh();
    } catch (err) {
      setTaskModalError(err instanceof Error ? err.message : "Could not finish task");
    }
  }

  async function restoreTask(task: Task) {
    setError("");
    try {
      await api<Task>(`/tasks/${task.id}/restore`, { method: "POST" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not restore task");
    }
  }

  async function purgeTask(task: Task) {
    setError("");
    try {
      await api<void>(`/tasks/${task.id}/purge`, { method: "DELETE" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete task");
    }
  }

  async function deleteEditingTask() {
    if (!editingTask) return;
    const deleted = await deleteTask(editingTask, setTaskModalError);
    if (!deleted) return;
    setTaskModalOpen(false);
    setEditingTask(null);
    setDraft(emptyDraft);
  }

  async function pullTask() {
    setError("");
    try {
      const pulled = await api<Session>("/tasks/pull", {
        method: "POST",
        body: JSON.stringify({ energy_level: energyLevel, category_id: categoryFilter }),
      });
      setSession(pulled);
      setDeclineEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not pull task");
    }
  }

  const filteredActiveTasks = activeTasks.filter((task) => {
    if (categoryFilter === null) return true;
    if (task.category_id === null) return false;
    return descendantCategoryIds(categoryFilter, categories).has(task.category_id);
  });

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
        categories={categories}
        dependencyOptions={activeTasks}
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
      <NavDrawer
        open={navOpen}
        page={page}
        onClose={() => setNavOpen(false)}
        onNavigate={(nextPage) => {
          setPage(nextPage);
          setNavOpen(false);
        }}
      />
      <header className="app-header">
        <div className="header-left">
          <button className="icon-button" title="Open navigation" onClick={() => setNavOpen(true)}>
            <Menu size={18} />
          </button>
          <h1>Odot</h1>
        </div>
        <div className="pull-controls">
          <AxisInput label="Energy" value={energyLevel} onChange={setEnergyLevel} />
          <button onClick={pullTask}>
            <Clock size={16} /> Pull Task
          </button>
        </div>
      </header>

      {error && <p className="error">{error}</p>}

      {page === "tasks" ? (
        <section className="task-page">
          <section className="main-panel list-panel">
            <TaskTable
              tasks={filteredActiveTasks}
              categories={categories}
              categoryFilter={categoryFilter}
              highlightedTaskId={hoveredTaskId}
              scrollToTaskId={matrixHoveredTaskId}
              onAdd={openNewTaskModal}
              onEdit={openEditTaskModal}
              onHoverTask={setHoveredTaskId}
              onCategoryFilterChange={setCategoryFilter}
            />
          </section>

          <section className="main-panel matrix-panel">
            <MatrixView
              tasks={filteredActiveTasks}
              categories={categories}
              highlightedTaskId={hoveredTaskId}
              onHoverTask={(taskId) => {
                setHoveredTaskId(taskId);
                setMatrixHoveredTaskId(taskId);
              }}
            />
          </section>
        </section>
      ) : page === "archive" ? (
        <section className="main-panel">
          <div className="panel-heading">
            <h2>Archived</h2>
          </div>
          <ArchiveList
            tasks={archivedTasks}
            categories={categories}
            onEdit={openEditTaskModal}
            onRestore={restoreTask}
            onPurge={purgeTask}
          />
        </section>
      ) : (
        <CategoriesPage
          categories={categories}
          tasks={activeTasks}
          onRefresh={refresh}
          onError={setError}
        />
      )}

      {taskModalOpen && (
        <TaskModal
          title={editingTask ? "Edit Task" : "New Task"}
          draft={draft}
          categories={categories}
          dependencyOptions={activeTasks}
          currentTaskId={editingTask?.id}
          submitLabel={editingTask ? "Save" : "Add Task"}
          error={taskModalError}
          canDelete={editingTask?.status === "active"}
          onChange={setDraft}
          onErrorChange={setTaskModalError}
          onDelete={deleteEditingTask}
          onFinish={editingTask?.status === "active" ? finishEditingTask : undefined}
          onStart={editingTask?.status === "active" ? startEditingTask : undefined}
          onSubmit={submitTask}
          onClose={closeTaskModal}
        />
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
