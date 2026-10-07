import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { IconUpload, IconX } from "../components/Icons";
import { Card, ErrorBanner } from "../components/ui";
import { usePolling } from "../hooks";

const LANGUAGES = [
  { value: "", label: "Авто (как в настройках)" },
  { value: "ru", label: "Русский" },
  { value: "en", label: "English" },
  { value: "de", label: "Deutsch" },
  { value: "fr", label: "Français" },
  { value: "es", label: "Español" },
];

function fmtBytes(bytes: number): string {
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(2)} ГБ`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} МБ`;
  return `${Math.max(1, Math.round(bytes / 1024))} КБ`;
}

export function NewJobPage() {
  const { data: engines } = usePolling(() => api.engines(), 60000);
  const [files, setFiles] = useState<File[]>([]);
  const [engine, setEngine] = useState("");
  const [language, setLanguage] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState<{ done: number; total: number } | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (engines && !engine) setEngine(engines.default);
  }, [engines, engine]);

  const addFiles = (selected: File[]) => {
    if (selected.length === 0) return;
    setFiles((prev) => [...prev, ...selected]);
    setErrors([]);
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, position) => position !== index));
  };

  const submit = async () => {
    if (files.length === 0 || busy) return;
    setBusy({ done: 0, total: files.length });
    setErrors([]);
    const failures: string[] = [];
    let firstId: string | null = null;
    for (let index = 0; index < files.length; index++) {
      try {
        const job = await api.submitJob(files[index], {
          engine: engine || undefined,
          language: language || undefined,
        });
        firstId = firstId ?? job.id;
      } catch (err) {
        failures.push(`${files[index].name} — ${(err as Error).message}`);
      }
      setBusy({ done: index + 1, total: files.length });
    }
    setBusy(null);
    if (failures.length > 0) setErrors(failures);
    if (firstId) {
      navigate(`/jobs/${firstId}`);
    } else {
      setFiles((prev) => [...prev]);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <header>
        <h1 className="text-xl font-semibold">Новая задача</h1>
        <p className="mt-1 text-sm text-muted">
          Аудио или видео — файл уйдёт в очередь и обработается на вашей GPU.
        </p>
      </header>

      {errors.length > 0 && (
        <ErrorBanner message={`Не удалось загрузить: ${errors.join("; ")}`} />
      )}

      <Card title="Файлы" bodyClassName="p-4">
        <div
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            addFiles(Array.from(event.dataTransfer.files));
          }}
          onClick={() => inputRef.current?.click()}
          className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
            dragging ? "border-accent/70 bg-surface2/50" : "border-edge hover:border-accent/40"
          }`}
        >
          <IconUpload className="size-7 text-muted" />
          <p className="text-sm">Перетащите файлы сюда или нажмите, чтобы выбрать</p>
          <p className="text-xs text-muted">MP4, MKV, MOV, MP3, WAV, M4A, FLAC и другие</p>
          <input
            ref={inputRef}
            type="file"
            multiple
            hidden
            onClick={(event) => event.stopPropagation()}
            onChange={(event) => {
              // Snapshot before resetting: value="" empties the live FileList
              // immediately, while React applies the state update later.
              const selected = Array.from(event.target.files ?? []);
              event.target.value = "";
              addFiles(selected);
            }}
          />
        </div>

        {files.length > 0 && (
          <ul className="mt-4 flex flex-col divide-y divide-edge/60 rounded-lg border border-edge">
            {files.map((file, index) => (
              <li key={`${file.name}-${index}`} className="flex items-center gap-3 px-3 py-2 text-sm">
                <span className="min-w-0 flex-1 truncate">{file.name}</span>
                <span className="tabular shrink-0 text-xs text-muted">{fmtBytes(file.size)}</span>
                <button
                  title="Убрать"
                  onClick={() => removeFile(index)}
                  disabled={busy != null}
                  className="rounded-md p-1 text-muted hover:bg-surface2 hover:text-err"
                >
                  <IconX className="size-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Параметры" bodyClassName="grid gap-4 p-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-xs text-muted uppercase">Движок</span>
          <select
            value={engine}
            onChange={(event) => setEngine(event.target.value)}
            className="rounded-lg border border-edge bg-surface px-3 py-2 text-sm outline-none focus:border-accent/50"
          >
            {(engines?.available ?? [engine]).map((name) => (
              <option key={name} value={name}>
                {name}
                {name === engines?.default ? " (по умолчанию)" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-xs text-muted uppercase">Язык</span>
          <select
            value={language}
            onChange={(event) => setLanguage(event.target.value)}
            className="rounded-lg border border-edge bg-surface px-3 py-2 text-sm outline-none focus:border-accent/50"
          >
            {LANGUAGES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      </Card>

      <div className="flex items-center gap-3">
        <button
          onClick={() => void submit()}
          disabled={files.length === 0 || busy != null}
          className="rounded-lg bg-gradient-to-r from-accent2 to-accent px-5 py-2.5 text-sm font-medium text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? `Загрузка ${busy.done}/${busy.total}…` : `Начать транскрибацию${files.length > 1 ? ` (${files.length})` : ""}`}
        </button>
        {files.length > 0 && !busy && (
          <span className="text-xs text-muted">
            MOSS точнее разделяет спикеров; whisper быстрее.
          </span>
        )}
      </div>
    </div>
  );
}
