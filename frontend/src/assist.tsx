/** Human assist: the operator only ever drives a page the worker is holding, through one ticketed socket (§10.2). */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, Hand, KeyRound, Loader2, MousePointer2, TextCursorInput, Undo2, X } from "lucide-react";

import { ApiFailure, api, listQuery, socketUrl } from "./api";
import { useSession } from "./session";
import type { HumanTask } from "./types";
import { Busy, ErrorNote, Pill, ReloadButton, fmtTime, statusTone, usePaged } from "./ui";

const KEYS = ["Enter", "Tab", "Escape", "Backspace", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Space"];

type Frame = {
  frame_id: number;
  page_url?: string;
  viewport?: [number, number];
  remaining_seconds?: number;
  image_base64?: string;
};

type SocketMessage = {
  type: string;
  step_id?: string;
  worker_local?: boolean;
  data?: Frame;
  reason?: string;
  command_id?: string;
  state?: string;
  status?: string;
};

export function AssistPage({ onOpenRun }: { onOpenRun: (id: string) => void }) {
  const { projectId, can, whoami } = useSession();
  const [status, setStatus] = useState("open");

  const fetcher = useMemo(
    () => (cursor: string | null) =>
      api.get<{ items: HumanTask[]; next_cursor?: string | null }>(
        `/projects/${projectId}/human-tasks`,
        listQuery({ limit: 20, cursor }, { status: status || undefined }),
      ),
    [projectId, status],
  );
  const list = usePaged<HumanTask>(fetcher);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (list.items.length === 0) {
      setSelected(null);
      return;
    }
    if (selected && list.items.some((item) => item.human_task_id === selected)) return;
    const mine = list.items.find((item) => item.controller_id === whoami?.user_id);
    setSelected(mine?.human_task_id ?? list.items[0].human_task_id);
  }, [list.items, selected, whoami]);

  const current = list.items.find((item) => item.human_task_id === selected) ?? null;

  if (!can("human_control")) {
    return (
      <section className="pane">
        <div className="pane-head">
          <h2>人工接管</h2>
        </div>
        <div className="empty">当前账号在本项目没有 HUMAN_CONTROL 权限，无法接管页面。</div>
      </section>
    );
  }

  return (
    <div className="split">
      <section className="pane">
        <div className="pane-head">
          <h2>等待接管的步骤</h2>
          <div className="pane-actions">
            <select className="mini" value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="open">进行中</option>
              <option value="all">全部</option>
              <option value="PENDING">待认领</option>
              <option value="CLAIMED">已认领</option>
              <option value="RESUME_REQUESTED">恢复确认中</option>
              <option value="COMPLETED">已完成</option>
              <option value="EXPIRED">已超时</option>
            </select>
            <ReloadButton onClick={list.reload} disabled={list.loading} />
          </div>
        </div>
        <ErrorNote error={list.error} />
        {list.loading && <Busy />}
        {!list.loading && list.items.length === 0 && <div className="empty">当前没有需要人工接管的步骤。</div>}
        <ul className="rows">
          {list.items.map((task) => (
            <li key={task.human_task_id} className={selected === task.human_task_id ? "row chosen" : "row"} onClick={() => setSelected(task.human_task_id)}>
              <div className="row-main">
                <strong>
                  步骤 {task.step_id} · {task.reason}
                </strong>
                <small>
                  运行 {task.execution_id.slice(0, 8)} · 期限 {fmtTime(task.deadline)}
                </small>
              </div>
              <div className="row-side">
                <Pill tone={statusTone(task.status)}>{task.status}</Pill>
                {task.holds_control ? <Pill tone="ok">控制中</Pill> : task.controller_id ? <Pill tone="warn">他人控制</Pill> : <Pill>无人认领</Pill>}
                <Pill tone={task.remaining_seconds > 60 ? "" : "bad"}>剩 {task.remaining_seconds}s</Pill>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="pane wide">
        {current ? (
          <ControlRoom key={current.human_task_id} task={current} onOpenRun={onOpenRun} onChanged={list.reload} />
        ) : (
          <div className="placeholder">选择一个接管任务。</div>
        )}
      </section>
    </div>
  );
}

function ControlRoom({ task, onOpenRun, onChanged }: { task: HumanTask; onOpenRun: (id: string) => void; onChanged: () => void }) {
  const { whoami } = useSession();
  const [current, setCurrent] = useState<HumanTask>(task);
  const [frame, setFrame] = useState<Frame | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const [socketState, setSocketState] = useState<"idle" | "connecting" | "open" | "closed">("idle");
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const [otp, setOtp] = useState("");
  const sequence = useRef(0);
  const socketRef = useRef<WebSocket | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => setCurrent(task), [task]);

  const append = useCallback((line: string) => {
    setLog((prev) => [...prev.slice(-120), line]);
  }, []);

  /** The picture is viewport-sized; a click maps through that scale, never through the CSS box (§10.2). */
  useEffect(() => {
    const image = frame?.image_base64;
    const target = canvas.current;
    if (!image || !target) return;
    const bitmap = new Image();
    bitmap.onload = () => {
      const [width, height] = frame?.viewport ?? [bitmap.naturalWidth, bitmap.naturalHeight];
      target.width = width;
      target.height = height;
      const context = target.getContext("2d");
      if (!context) return;
      context.clearRect(0, 0, width, height);
      context.drawImage(bitmap, 0, 0, width, height);
    };
    bitmap.src = `data:image/png;base64,${image}`;
  }, [frame]);

  const send = useCallback(
    (payload: Record<string, unknown>) => {
      const socket = socketRef.current;
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        setError("控制通道尚未连接：先认领任务并等待画面就绪。");
        return;
      }
      sequence.current += 1;
      const body = { ...payload, sequence: sequence.current, frame_id: frame?.frame_id ?? 0 };
      socket.send(JSON.stringify({ type: "command", payload: body }));
      append(`→ ${JSON.stringify(body)}`);
    },
    [append, frame],
  );

  const openChannel = useCallback(async () => {
    setSocketState("connecting");
    setError("");
    try {
      const issued = await api.post<{ channel: string }>(`/human-tasks/${current.human_task_id}/control-ticket`);
      const socket = new WebSocket(socketUrl(issued.channel));
      socketRef.current = socket;
      socket.onopen = () => {
        setSocketState("open");
        append("控制通道已连接");
      };
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data as string) as SocketMessage;
        if (message.type === "frame" && message.data) {
          setFrame(message.data);
          return;
        }
        if (message.type === "ready") {
          append(`就绪：步骤 ${message.step_id}，画面由本节点采集 ${message.worker_local ? "是" : "否"}`);
          return;
        }
        if (message.type === "command_result") {
          append(`指令 ${message.status}${message.reason ? ` · ${message.reason}` : ""}`);
          onChanged();
          return;
        }
        if (message.type === "accepted") {
          append(`指令已记录 ${(message.command_id ?? "").slice(0, 8)} · ${message.state ?? ""}`);
          return;
        }
        append(`${message.type}: ${message.reason ?? ""}`);
      };
      socket.onerror = () => setError("控制通道发生错误；页面操作只由持有租约的 worker 执行。");
      socket.onclose = (event) => {
        setSocketState("closed");
        socketRef.current = null;
        if (event.code !== 1000) append(`通道关闭 ${event.code} ${event.reason ?? ""}`);
        onChanged();
      };
    } catch (error) {
      setSocketState("idle");
      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    }
  }, [append, current.human_task_id, onChanged]);

  useEffect(() => {
    return () => {
      socketRef.current?.close(1000, "unmounted");
      socketRef.current = null;
    };
  }, []);

  async function claim() {
    setError("");
    try {
      const claimed = await api.post<HumanTask>(`/human-tasks/${current.human_task_id}/claim`, { expected_status: current.status });
      setCurrent(claimed);
      sequence.current = 0;
      append(`已认领，租约至 ${fmtTime(claimed.control_lease_until)}`);
      await openChannel();
      onChanged();
    } catch (error) {
      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    }
  }

  function clickAt(event: React.MouseEvent<HTMLCanvasElement>) {
    const target = canvas.current;
    if (!target || !live) return;
    const rect = target.getBoundingClientRect();
    send({
      operation: "click",
      x: Math.round(((event.clientX - rect.left) / rect.width) * target.width),
      y: Math.round(((event.clientY - rect.top) / rect.height) * target.height),
    });
  }

  async function release() {
    setError("");
    try {
      const released = await api.post<HumanTask>(`/human-tasks/${current.human_task_id}/release`, { reason: "operator released control from the console" });
      setCurrent(released);
      socketRef.current?.close(1000, "released");
      append("已交还控制；任务仍然打开，可再次认领");
      onChanged();
    } catch (error) {
      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    }
  }

  async function resume(stepCompleted: boolean) {
    setError("");
    try {
      const result = await api.post<{ command_id: string; task_status: string; execution_id: string }>(
        `/human-tasks/${current.human_task_id}/resume`,
        { step_completed: stepCompleted, note: stepCompleted ? "operator completed the step by hand" : "hand back without claiming completion" },
        { "Idempotency-Key": `resume:${current.human_task_id}:${Date.now().toString(36)}` },
      );
      append(`恢复请求已提交（${result.task_status}），由 worker 校验后才算通过`);
      socketRef.current?.close(1000, "resumed");
      setCurrent((prev) => ({ ...prev, status: result.task_status }));
      onChanged();
      onOpenRun(result.execution_id);
    } catch (error) {
      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    }
  }

  const mine = current.controller_id === whoami?.user_id;
  const live = socketState === "open";

  return (
    <div className="control-room">
      <div className="pane-head">
        <div>
          <h2>接管控制台</h2>
          <small className="muted">
            任务 {current.human_task_id.slice(0, 8)} · 步骤 {current.step_id} · 帧 {frame?.frame_id ?? 0} · 指令序号 {sequence.current}
            {frame?.page_url ? ` · ${frame.page_url}` : ""}
          </small>
        </div>
        <div className="pane-actions">
          <Pill tone={live ? "ok" : socketState === "connecting" ? "warn" : "muted"}>
            {live ? "通道已连接" : socketState === "connecting" ? "连接中" : "未连接"}
          </Pill>
          <Pill tone={statusTone(current.status)}>{current.status}</Pill>
          {!mine && (
            <button className="button primary" onClick={claim}>
              <Hand size={14} />
              认领控制
            </button>
          )}
          {mine && !live && (
            <button className="button" onClick={openChannel}>
              <Loader2 size={14} />
              重新连接
            </button>
          )}
        </div>
      </div>

      <ErrorNote error={error} />

      <div className="viewport-wrap">
        <canvas ref={canvas} className="viewport" width={1280} height={720} onClick={clickAt} />
        {!frame && <div className="viewport-empty">认领后这里显示 worker 实时采样的画面；画面从不落盘（§10.4）。</div>}
      </div>

      <div className="control-tools">
        <div className="tool-group">
          <strong>
            <MousePointer2 size={13} />
            点击
          </strong>
          <span className="muted">在画面上点击，坐标按真实视口换算</span>
        </div>
        <div className="tool-group">
          <strong>
            <TextCursorInput size={13} />
            输入
          </strong>
          <div className="inline">
            <input
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="输入文本后回车发送"
              disabled={!live}
              onKeyDown={(event) => {
                if (event.key === "Enter" && text) {
                  send({ operation: "type", text });
                  setText("");
                }
              }}
            />
            <button
              className="button"
              disabled={!live || !text}
              onClick={() => {
                send({ operation: "type", text });
                setText("");
              }}
            >
              发送
            </button>
          </div>
        </div>
        <div className="tool-group">
          <strong>
            <KeyRound size={13} />
            按键
          </strong>
          <div className="keys">
            {KEYS.map((item) => (
              <button key={item} className="key" disabled={!live} onClick={() => send({ operation: "key", key: item })}>
                {item}
              </button>
            ))}
          </div>
        </div>
        <div className="tool-group">
          <strong>滚动</strong>
          <div className="inline">
            <button className="button" disabled={!live} onClick={() => send({ operation: "scroll", dy: -600 })}>
              上滚
            </button>
            <button className="button" disabled={!live} onClick={() => send({ operation: "scroll", dy: 600 })}>
              下滚
            </button>
          </div>
        </div>
        <div className="tool-group">
          <strong>一次性验证码</strong>
          <div className="inline">
            <input value={otp} onChange={(event) => setOtp(event.target.value)} placeholder="只进页面，不落盘" disabled={!live} />
            <button
              className="button"
              disabled={!live || !otp}
              onClick={async () => {
                setError("");
                try {
                  sequence.current += 1;
                  await api.post(`/human-tasks/${current.human_task_id}/otp`, {
                    text: otp,
                    sequence: sequence.current,
                    frame_id: frame?.frame_id ?? 0,
                  });
                  append("验证码已输入页面，内容不会被记录");
                  setOtp("");
                } catch (error) {
                  setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                }
              }}
            >
              提交
            </button>
          </div>
        </div>
      </div>

      <div className="finish-row">
        <button className="button primary" disabled={!mine} onClick={() => resume(true)}>
          <Check size={14} />
          本步已由人工完成
        </button>
        <button className="button" disabled={!mine} onClick={() => resume(false)}>
          <Undo2 size={14} />
          未代为完成，交回 worker
        </button>
        <button className="button" disabled={!mine} onClick={release}>
          <X size={14} />
          交还控制
        </button>
      </div>

      <div className="log compact">
        {log.length === 0 && <div className="empty">还没有控制事件。</div>}
        {log.map((line, index) => (
          <div key={`${index}-${line.slice(0, 10)}`}>{line}</div>
        ))}
      </div>
    </div>
  );
}
