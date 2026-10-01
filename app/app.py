"""NoteToFilm —— 文稿成片（出口成片）。

贴文稿 → 分镜 → 生图(可配网关，未配则占位图) → edge-tts 逐镜配音 →
网页双闸复核(图文/配音) → compose_video.py 合成(--fps 2) → 飞书通知。

运行: uvicorn app.app:app --port 8765
配置(env): OPENAI_API_KEY / OPENAI_BASE / TTS_VOICE / FEISHU_WEBHOOK / ACCESS_TOKEN / VIDEO_DATA_DIR
"""

import base64
import asyncio
import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import edge_tts
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("VIDEO_DATA_DIR", str(ROOT / "data"))).resolve()
ENGINE = ROOT / "vendor" / "compose_video.py"
DB = DATA / "tasks.db"
TTS_VOICE = os.environ.get("TTS_VOICE", "zh-CN-YunxiNeural")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
# 设置后所有页面与 API 需带 ?token= / X-Access-Token / cookie 之一；空＝不鉴权（仅本机开发）
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
# 任意 OpenAI-compatible 网关（base 与 key 成对配置）。未配时：分镜按空行分段、生图为占位图。
API_BASE = os.environ.get("OPENAI_BASE", "")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "1024x768")
IMAGE_TIMEOUT = int(os.environ.get("IMAGE_TIMEOUT", "300"))
STYLE_PRESET = os.environ.get(
    "STYLE_PRESET", "扁平插画风格，蓝白主色调，简洁构图，画面中不出现文字"
)
PLACEHOLDER_COLORS = ["#223344", "#334455", "#445566", "#554433", "#663322"]
# 同时处于 提交/生成/渲染 的任务上限；超出直接 429，避免多人同时提交打爆线程与网关
MAX_ACTIVE_TASKS = int(os.environ.get("MAX_ACTIVE_TASKS", "3"))
ACTIVE_STATUSES = ("submitted", "generating", "rendering")

# 单镜重做进行中标记（进程内；重启即清，与素材目录无持久化耦合）
_REGEN_LOCK = threading.Lock()
_REGEN_INFLIGHT: set[tuple[int, int, str]] = set()


def regen_mark(task_id: int, idx: int, kind: str):
    with _REGEN_LOCK:
        _REGEN_INFLIGHT.add((task_id, idx, kind))


def regen_clear(task_id: int, idx: int, kind: str):
    with _REGEN_LOCK:
        _REGEN_INFLIGHT.discard((task_id, idx, kind))


def regen_busy(task_id: int, idx: int, kind: str) -> bool:
    with _REGEN_LOCK:
        return (task_id, idx, kind) in _REGEN_INFLIGHT


def any_regen_busy(task_id: int) -> bool:
    with _REGEN_LOCK:
        return any(key[0] == task_id for key in _REGEN_INFLIGHT)


def count_active_tasks(conn) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM tasks WHERE status IN "
        f"({','.join('?' * len(ACTIVE_STATUSES))})",
        ACTIVE_STATUSES,
    ).fetchone()["c"]


def api_post(path: str, payload: dict, timeout: int) -> dict:
    if not API_BASE or not API_KEY:
        raise RuntimeError("网关未配置：需要 OPENAI_BASE 与 OPENAI_API_KEY 成对设置")
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)

app = FastAPI(title="NoteToFilm")


@app.middleware("http")
async def token_auth(request: Request, call_next):
    if ACCESS_TOKEN:
        supplied = (
            request.query_params.get("token")
            or request.headers.get("X-Access-Token")
            or request.cookies.get("avtoken")
        )
        if supplied != ACCESS_TOKEN:
            if request.url.path.startswith(("/api/", "/files/")):
                return JSONResponse({"detail": "缺少或错误的访问令牌"}, status_code=401)
            return HTMLResponse(
                "<!doctype html><html lang=zh><head><meta charset=utf-8>"
                "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
                "<title>NoteToFilm · 访问令牌</title>"
                "<style>html{color-scheme:light}"
                "body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f6f8;color:#1f2329;"
                "max-width:380px;margin:80px auto;padding:0 20px}"
                "input{width:62%;padding:8px;border:1px solid #c9ccd4;border-radius:6px;box-sizing:border-box}"
                "button{padding:8px 16px;margin-left:8px;border:1px solid #3370ff;"
                "background:#3370ff;color:#fff;border-radius:6px;cursor:pointer}</style></head>"
                "<body><h3>NoteToFilm</h3><form method=get>"
                "<input name=token placeholder=访问令牌 autofocus>"
                "<button>进入</button></form></body>",
                status_code=401,
            )
        response = await call_next(request)
        response.set_cookie("avtoken", ACCESS_TOKEN, httponly=True, samesite="lax", max_age=30 * 86400)
        return response
    return await call_next(request)


@contextmanager
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              script TEXT NOT NULL,
              status TEXT NOT NULL,
              error TEXT,
              created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS scenes(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL,
              idx INTEGER NOT NULL,
              narration TEXT NOT NULL,
              img_ok INTEGER NOT NULL DEFAULT 0,
              aud_ok INTEGER NOT NULL DEFAULT 0,
              img_placeholder INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        # 旧库迁移：09-28 前建的 scenes 表补占位图标记列
        try:
            conn.execute("ALTER TABLE scenes ADD COLUMN img_placeholder INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass


def recover_interrupted():
    """启动自愈：上次运行中的任务复位为 interrupted，由复核页"尝试恢复"接续。"""
    with db() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='interrupted',"
            "error=COALESCE(error,'服务重启导致任务中断，可在复核页尝试恢复')"
            " WHERE status IN ('generating','rendering')"
        )
    if cur.rowcount:
        print(f"[startup] 复位中断任务 {cur.rowcount} 个", file=sys.stderr, flush=True)


init()
recover_interrupted()


def notify(text: str):
    if not FEISHU_WEBHOOK:
        return
    try:
        req = urllib.request.Request(
            FEISHU_WEBHOOK,
            data=json.dumps({"msg_type": "text", "content": {"text": text}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ---------------- 编排 ----------------

def split_scenes(script: str) -> list[str]:
    """LLM 分镜（输出 JSON 数组）；无 key 或失败时回退按空行分段。"""
    plain = [p.strip() for p in re.split(r"\n+", script) if p.strip()][:30] or [script.strip()]
    if not API_KEY:
        return plain
    try:
        resp = api_post(
            "/chat/completions",
            {
                "model": LLM_MODEL,
                "temperature": 0.3,
                "messages": [{
                    "role": "user",
                    "content": (
                        "把文稿拆成分镜旁白。规则：每镜表达一个完整的意思，忠实原文，"
                        "不新增事实、人物、数字或引文；最多 30 镜；"
                        "只输出 JSON 字符串数组，不要其他内容。\n文稿：\n" + script
                    ),
                }],
            },
            timeout=120,
        )
        content = resp["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
        scenes = json.loads(content)
        if isinstance(scenes, list) and scenes and all(
            isinstance(s, str) and s.strip() for s in scenes
        ):
            return [s.strip() for s in scenes][:30]
    except Exception:
        pass
    return plain


def scene_prompt(narration: str) -> str:
    return f"{STYLE_PRESET}。用一幅画面表达以下内容：{narration}"


def gen_image(task_id: int, idx: int, narration: str) -> str:
    """生图成功返回相对路径；失败抛异常（调用方回退占位图，不自动重试付费调用）。"""
    path = DATA / str(task_id) / "images" / f"{idx:03d}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    resp = api_post(
        "/images/generations",
        {
            "model": IMAGE_MODEL,
            "prompt": scene_prompt(narration),
            "size": IMAGE_SIZE,
            "quality": "low",
            "n": 1,
        },
        timeout=IMAGE_TIMEOUT,
    )
    d = resp["data"][0]
    raw = (
        base64.b64decode(d["b64_json"]) if d.get("b64_json")
        else urllib.request.urlopen(d["url"], timeout=60).read()
    )
    if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
        raise ValueError("上游返回的不是 PNG/JPEG 图片")
    path.write_bytes(raw)
    return f"images/{idx:03d}.png"


def placeholder_image(task_id: int, idx: int) -> str:
    color = PLACEHOLDER_COLORS[idx % len(PLACEHOLDER_COLORS)][1:]
    path = DATA / str(task_id) / "images" / f"{idx:03d}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=0x{color}:s=1920x1080", "-frames:v", "1", str(path)],
        check=True,
    )
    return f"images/{idx:03d}.png"


async def gen_audio(text: str, out: Path):
    await asyncio.wait_for(edge_tts.Communicate(text, TTS_VOICE).save(str(out)), timeout=120)


def run_task(task_id: int):
    def fail(err: str):
        with db() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', error=? WHERE id=?",
                (err[:2000], task_id),
            )
        notify(f"视频任务 {task_id} 失败，请在本地复核页查看详情")

    try:
        with db() as conn:
            script = conn.execute(
                "SELECT script FROM tasks WHERE id=?", (task_id,)
            ).fetchone()["script"]
            conn.execute("UPDATE tasks SET status='generating' WHERE id=?", (task_id,))
        img_failures = []
        narrations = split_scenes(script)
        print(f"[task {task_id}] 分镜数={len(narrations)}", file=sys.stderr, flush=True)
        for idx, narration in enumerate(narrations):
            print(f"[task {task_id}] 镜{idx} 生图…", file=sys.stderr, flush=True)
            placeholder = True
            try:
                if API_KEY:
                    gen_image(task_id, idx, narration)
                    placeholder = False
            except Exception:
                img_failures.append(idx)  # 付费调用失败不盲重试，占位图＋人工单镜重做
            if placeholder:
                placeholder_image(task_id, idx)
            print(f"[task {task_id}] 镜{idx} 配音…", file=sys.stderr, flush=True)
            audio = f"audio/{idx:03d}.mp3"
            out = DATA / str(task_id) / audio
            out.parent.mkdir(parents=True, exist_ok=True)
            asyncio.run(gen_audio(narration, out))
            with db() as conn:
                conn.execute(
                    "INSERT INTO scenes(task_id, idx, narration, img_placeholder) VALUES(?,?,?,?)",
                    (task_id, idx, narration, int(placeholder)),
                )
        print(f"[task {task_id}] 编排完成", file=sys.stderr, flush=True)
        with db() as conn:
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        tail = f"（{len(img_failures)} 镜生图失败已用占位图，请在复核页重做）" if img_failures else ""
        notify(f"视频任务 {task_id} 已生成素材，待复核{tail}")
    except Exception as exc:
        fail(str(exc))


def render_task(task_id: int):
    def fail(err: str):
        with db() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', error=? WHERE id=?",
                (err[:2000], task_id),
            )
        notify(f"视频任务 {task_id} 渲染失败，请在本地复核页查看详情")

    try:
        task_dir = DATA / str(task_id)
        with db() as conn:
            rows = conn.execute(
                "SELECT idx, narration FROM scenes WHERE task_id=? ORDER BY idx",
                (task_id,),
            ).fetchall()
        manifest = {
            "schema_version": 1,
            "title": f"task-{task_id}",
            "scenes": [
                {
                    "id": f"{r['idx']:03d}",
                    "source": "task",
                    "narration": r["narration"],
                    "image": f"images/{r['idx']:03d}.png",
                    "audio": f"audio/{r['idx']:03d}.mp3",
                }
                for r in rows
            ],
        }
        (task_dir / "scenes.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(ENGINE),
             "--manifest", "scenes.json", "--work-dir", f"render-{int(time.time())}",
             "--output", "video.mp4", "--fps", "2", "--render"],
            cwd=task_dir, capture_output=True, text=True, timeout=1800,
        )
        if result.returncode or not (task_dir / "video.mp4").is_file():
            raise RuntimeError(result.stderr[-800:] or "engine returned no video")
        with db() as conn:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        notify(f"视频任务 {task_id} 已成片：{int((task_dir / 'video.mp4').stat().st_size / 1e6)}MB")
    except Exception as exc:
        fail(str(exc))


# ---------------- API ----------------

class Submit(BaseModel):
    script: str


@app.post("/api/submit")
def submit(body: Submit):
    script = body.script.strip()
    if not script:
        raise HTTPException(400, "文稿不能为空")
    with db() as conn:
        active = count_active_tasks(conn)
        if active >= MAX_ACTIVE_TASKS:
            raise HTTPException(
                429, f"系统繁忙：{active} 个任务进行中（上限 {MAX_ACTIVE_TASKS}），请稍后再提交"
            )
        dup = conn.execute(
            "SELECT id FROM tasks WHERE script=? AND status IN "
            "('submitted','generating','review','rendering') ORDER BY id DESC LIMIT 1",
            (script,),
        ).fetchone()
        if dup:
            raise HTTPException(
                409, f"相同文稿已有任务 {dup['id']} 在处理中，请从任务列表打开它继续，勿重复提交"
            )
        cur = conn.execute(
            "INSERT INTO tasks(script, status, created_at) VALUES(?, 'submitted', ?)",
            (script, datetime.now().isoformat(timespec="seconds")),
        )
        task_id = cur.lastrowid
    threading.Thread(target=run_task, args=(task_id,), daemon=True).start()
    return {"task_id": task_id}


def _versioned(path: Path, url: str) -> str:
    # 素材会被重做覆盖且 URL 不变，浏览器会拿缓存旧图，必须带 mtime 版本参数
    return f"{url}?v={int(path.stat().st_mtime)}" if path.exists() else url


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    with db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "任务不存在")
        scenes = conn.execute(
            "SELECT * FROM scenes WHERE task_id=? ORDER BY idx", (task_id,)
        ).fetchall()
    return {
        "id": task["id"], "status": task["status"], "error": task["error"],
        "created_at": task["created_at"], "busy": any_regen_busy(task_id),
        "video": _versioned(
            DATA / str(task_id) / "video.mp4", f"/files/{task_id}/video.mp4"
        ),
        "scenes": [
            {"idx": s["idx"], "narration": s["narration"], "img_ok": s["img_ok"],
             "aud_ok": s["aud_ok"], "img_placeholder": s["img_placeholder"],
             "img_busy": regen_busy(task_id, s["idx"], "img"),
             "aud_busy": regen_busy(task_id, s["idx"], "aud"),
             "img_exists": (DATA / str(task_id) / "images" / f"{s['idx']:03d}.png").exists(),
             "image": _versioned(
                 DATA / str(task_id) / "images" / f"{s['idx']:03d}.png",
                 f"/files/{task_id}/images/{s['idx']:03d}.png"),
             "audio": _versioned(
                 DATA / str(task_id) / "audio" / f"{s['idx']:03d}.mp3",
                 f"/files/{task_id}/audio/{s['idx']:03d}.mp3")}
            for s in scenes
        ],
    }


class Gate(BaseModel):
    gate: str  # img | aud
    passed: bool


def require_review(conn, task_id: int):
    task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "review":
        raise HTTPException(409, "仅待复核任务可以修改素材或确认")


@app.get("/api/tasks")
def list_tasks():
    with db() as conn:
        rows = conn.execute(
            "SELECT t.id, t.status, t.created_at, COUNT(s.id) AS scenes"
            " FROM tasks t LEFT JOIN scenes s ON s.task_id=t.id"
            " GROUP BY t.id ORDER BY t.id DESC LIMIT 200"
        ).fetchall()
    return {"tasks": [dict(r) for r in rows]}


@app.post("/api/tasks/{task_id}/retry")
def retry(task_id: int, force: bool = False):
    """中断/失败任务恢复：有成片→标记完成；素材齐→回待复核（免费）；否则确认后整体重跑（重新付费生图）。"""
    with db() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        scenes = conn.execute(
            "SELECT idx FROM scenes WHERE task_id=? ORDER BY idx", (task_id,)
        ).fetchall()
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("interrupted", "failed"):
        raise HTTPException(409, "仅中断或失败任务可尝试恢复")
    task_dir = DATA / str(task_id)
    if (task_dir / "video.mp4").is_file() and (task_dir / "video.build.json").is_file():
        with db() as conn:
            conn.execute("UPDATE tasks SET status='done', error=NULL WHERE id=?", (task_id,))
        notify(f"视频任务 {task_id} 恢复核验：成片已在，已标记完成")
        return {"ok": True, "result": "done", "message": "检测到已成片，已标记完成"}
    complete = bool(scenes) and all(
        (task_dir / "images" / f"{s['idx']:03d}.png").is_file()
        and (task_dir / "audio" / f"{s['idx']:03d}.mp3").is_file()
        for s in scenes
    )
    if complete:
        with db() as conn:
            conn.execute("UPDATE tasks SET status='review', error=NULL WHERE id=?", (task_id,))
        return {"ok": True, "result": "review", "message": "素材完整，已回到待复核，确认后可直接合成"}
    if not force:
        return {"ok": False, "needs_confirm": True,
                "message": "素材不完整，恢复将重新编排并重新调用付费生图，确认继续？"}
    with db() as conn:
        if count_active_tasks(conn) >= MAX_ACTIVE_TASKS:
            raise HTTPException(
                429, f"系统繁忙：进行中任务已达上限 {MAX_ACTIVE_TASKS}，请稍后再试"
            )
        conn.execute("DELETE FROM scenes WHERE task_id=?", (task_id,))
        conn.execute("UPDATE tasks SET status='submitted', error=NULL WHERE id=?", (task_id,))
    shutil.rmtree(task_dir, ignore_errors=True)
    threading.Thread(target=run_task, args=(task_id,), daemon=True).start()
    return {"ok": True, "result": "rerun", "message": "已重新开始编排"}


@app.post("/api/tasks/{task_id}/scenes/{idx}/gate")
def gate(task_id: int, idx: int, body: Gate):
    if body.gate not in ("img", "aud"):
        raise HTTPException(400, "gate 必须是 img 或 aud")
    col = "img_ok" if body.gate == "img" else "aud_ok"
    with db() as conn:
        require_review(conn, task_id)
        scene = conn.execute(
            "SELECT img_placeholder FROM scenes WHERE task_id=? AND idx=?", (task_id, idx)
        ).fetchone()
        if not scene:
            raise HTTPException(404, "镜不存在")
        if body.gate == "img" and body.passed and scene["img_placeholder"]:
            raise HTTPException(409, "该镜仍是占位图，不能通过图文复核，请先重做图")
        cur = conn.execute(
            f"UPDATE scenes SET {col}=? WHERE task_id=? AND idx=?",
            (int(body.passed), task_id, idx),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "镜不存在")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/scenes/{idx}/regen-image")
def regen_image(task_id: int, idx: int):
    """人工点击触发单镜重做生图（付费调用，绝不自动重试）。"""
    with db() as conn:
        require_review(conn, task_id)
        row = conn.execute(
            "SELECT narration FROM scenes WHERE task_id=? AND idx=?", (task_id, idx)
        ).fetchone()
        if not row:
            raise HTTPException(404, "镜不存在")
        if not API_KEY:
            raise HTTPException(409, "未配置生图网关")
        conn.execute(
            "UPDATE scenes SET img_ok=0 WHERE task_id=? AND idx=?", (task_id, idx)
        )

    regen_mark(task_id, idx, "img")

    def work():
        ok = False
        try:
            gen_image(task_id, idx, row["narration"])
            ok = True
        except Exception:
            pass
        with db() as conn:
            if ok:
                conn.execute(
                    "UPDATE scenes SET img_ok=0, img_placeholder=0 WHERE task_id=? AND idx=?",
                    (task_id, idx),
                )
            else:
                conn.execute(
                    "UPDATE scenes SET img_ok=0 WHERE task_id=? AND idx=?", (task_id, idx)
                )
        regen_clear(task_id, idx, "img")
        if not ok:
            notify(f"任务 {task_id} 镜 {idx} 重做生图失败，未自动重试")

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "message": "重做已发起，稍后刷新查看"}


@app.post("/api/tasks/{task_id}/scenes/{idx}/regen-audio")
def regen_audio(task_id: int, idx: int):
    """人工触发单镜重配音（edge-tts 免费，同步执行）。"""
    with db() as conn:
        require_review(conn, task_id)
        row = conn.execute(
            "SELECT narration FROM scenes WHERE task_id=? AND idx=?", (task_id, idx)
        ).fetchone()
        if not row:
            raise HTTPException(404, "镜不存在")
        conn.execute(
            "UPDATE scenes SET aud_ok=0 WHERE task_id=? AND idx=?", (task_id, idx)
        )
    out = DATA / str(task_id) / "audio" / f"{idx:03d}.mp3"
    regen_mark(task_id, idx, "aud")
    try:
        asyncio.run(gen_audio(row["narration"], out))
    except Exception as exc:
        raise HTTPException(500, f"配音失败：{exc}")
    finally:
        regen_clear(task_id, idx, "aud")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/render")
def render(task_id: int):
    with db() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        scenes = conn.execute(
            "SELECT * FROM scenes WHERE task_id=? ORDER BY idx", (task_id,)
        ).fetchall()
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("review",):
        raise HTTPException(409, f"状态 {task['status']} 不可合成")
    if not scenes:
        raise HTTPException(409, "没有可合成的分镜")
    blocked = [
        s["idx"] for s in scenes
        if not (s["img_ok"] and s["aud_ok"]) or s["img_placeholder"]
    ]
    if blocked:
        raise HTTPException(409, f"镜 {blocked} 未通过双闸复核或仍为占位图")
    with db() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='rendering' WHERE id=? AND status='review'", (task_id,)
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "任务状态已变化，请刷新")
    threading.Thread(target=render_task, args=(task_id,), daemon=True).start()
    return {"ok": True}


@app.get("/files/{task_id}/{name:path}")
def files(task_id: int, name: str):
    task_dir = (DATA / str(task_id)).resolve()
    path = (task_dir / name).resolve()
    if not path.is_relative_to(task_dir) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/tasks/{task_id}/review", response_class=HTMLResponse)
def review_page(task_id: int):
    return REVIEW_HTML.replace("__TASK_ID__", str(task_id))


REVIEW_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>复核 · NoteToFilm</title><style>
html{color-scheme:light}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f6f8;color:#1f2329;max-width:900px;margin:0 auto;padding:0 16px 40px}
.sc{background:#fff;border:1px solid #dee0e3;border-radius:8px;padding:14px 16px;margin:12px 0}
.sc img{max-width:320px;border-radius:4px;display:block;margin:8px 0}
.sc audio{display:block;margin:8px 0}
.sc button{margin:4px 8px 4px 0}
.nt{background:#f7f8fa;border-left:3px solid #c9ccd4;padding:6px 10px;margin:6px 0;font-size:14px;color:#41464e}
button{cursor:pointer;border:1px solid #c9ccd4;border-radius:6px;background:#fff;color:#1f2329;padding:6px 14px}
button:disabled{cursor:not-allowed;opacity:.4}
#compose{font-size:15px;background:#3370ff;border-color:#3370ff;color:#fff;padding:8px 20px}
#bar{position:sticky;top:0;background:#f5f6f8;padding:10px 0;border-bottom:1px solid #dee0e3;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
#gates{display:flex;gap:8px;font-size:13px}
.gate{border-radius:4px;padding:1px 8px;background:#e9ebef;color:#686f7a}
.gate.full{background:#e8f7ee;color:#157347}
.gate.warn{background:#fff7e6;color:#b25e00}
#hint{color:#d25f00;font-size:14px}
#hint.err{color:#c4340f}
#hint.okc{color:#157347}
.ok{background:#e8f7ee;border-color:#b7e4c7;color:#157347}
.no{background:#fff7e6;border-color:#ffe1a8;color:#b25e00}
.ph{display:inline-block;background:#fdecec;border:1px solid #f5b5b5;color:#c4340f;border-radius:4px;padding:1px 8px;font-size:12px;font-weight:600}
.errbox{background:#fff;border:1px solid #f5b5b5;border-left:4px solid #d84b4b;border-radius:8px;padding:12px 16px;margin:12px 0;color:#c4340f}
.missing{display:inline-block;width:320px;max-width:100%;padding:48px 0;text-align:center;background:#e9ebef;color:#686f7a;border-radius:4px;margin:8px 0;font-size:13px}
@media (max-width:640px){
body{padding:0 10px 32px}
.sc{padding:10px 12px}
.sc img{max-width:100%}
.sc audio{width:100%}
.sc button{padding:6px 10px;font-size:13px}
#compose{font-size:14px;padding:8px 14px}
}
</style></head><body>
<div id=bar><button id=compose onclick=compose()>全部通过，合成视频</button>
<span id=gates></span><span id=hint></span></div><div id=list>加载中…</div>
<script>
const TID=__TASK_ID__;
let currentStatus='submitted',lastBusy=false;
async function request(url,options){
 const r=await fetch(url,options);
 const j=await r.json();
 if(!r.ok)throw new Error(j.detail||'请求失败');
 return j;
}
function setHint(msg,kind){const h=document.getElementById('hint');
 h.textContent=msg||'';h.className=kind||'';}
function gateStats(scenes){
 const n=scenes.length;
 return {n,
  img:scenes.filter(s=>s.img_ok&&!s.img_placeholder).length,
  aud:scenes.filter(s=>s.aud_ok).length,
  ph:scenes.filter(s=>s.img_placeholder).length};
}
function renderBar(t){
 const bar=document.getElementById('gates');
 const c=document.getElementById('compose');
 if(t.status==='review'&&t.scenes.length){
  const s=gateStats(t.scenes);
  bar.innerHTML=`<span class="gate ${s.img===s.n?'full':''}">图文 ${s.img}/${s.n}</span>`+
   `<span class="gate ${s.aud===s.n?'full':''}">配音 ${s.aud}/${s.n}</span>`+
   (s.ph?`<span class="gate warn">占位图 ${s.ph} 张</span>`:'');
  const ready=s.img===s.n&&s.aud===s.n&&s.ph===0;
  c.disabled=!ready;
  if(ready)setHint('全部就绪，可合成','okc');
 }else{bar.innerHTML='';c.disabled=true;}
}
async function load(){
 try{
 const t=await request(`/api/tasks/${TID}`);
 currentStatus=t.status;lastBusy=!!t.busy;
 renderBar(t);
 if(t.status==='done'){document.getElementById('list').innerHTML=
  `<h2>已成片</h2><p><a href="${t.video||'/files/'+TID+'/video.mp4'}">下载成片 video.mp4</a></p>`+
  t.scenes.map(s=>scHtml(s)).join('');return;}
 if(['interrupted','failed'].includes(t.status)){document.getElementById('list').innerHTML=
  `<h2>${t.status==='failed'?'任务失败':'任务中断'}</h2>
   <div class=errbox>${esc(t.error||'原因未知')}</div>
   <button onclick=retryTask()>尝试恢复</button>`;return;}
 document.getElementById('list').innerHTML=t.scenes.map(s=>scHtml(s)).join('');
 if(t.status==='generating')setHint('生成中，页面每 5 秒自动刷新');
 }catch(e){setHint(e.message,'err');}
}
function scHtml(s){const locked=currentStatus!=='review'?'disabled':'';
 const ph=s.img_placeholder?' <span class=ph>占位图·需重做</span>':'';
 // 占位图只锁图文闸（后端 409 语义）；配音复核不受占位图影响
 const imgGateLock=(locked||s.img_placeholder)?'disabled':'';
 const audGateLock=locked?'disabled':'';
 const gateTip=s.img_placeholder?' title=占位图重做通过后再复核':'';
 const imgTag=s.img_exists
  ?`<a href="${s.image}" target=_blank title=点击查看原图><img src="${s.image}"></a>`
  :`<span class=missing>图片生成中…</span>`;
 return `<div class=sc><b>镜 ${String(s.idx+1).padStart(2,'0')}</b>${ph}
 <div class=nt>${esc(s.narration)}</div>${imgTag}
 <audio controls src="${s.audio}"></audio>
 <button ${imgGateLock}${gateTip} class="${s.img_ok?'ok':'no'}" onclick=gate(${s.idx},'img',true)>${s.img_ok?'图文已过 ✓':'图文通过'}</button>
 <button ${s.img_busy||locked?'disabled':''} onclick=regen(${s.idx},'regen-image')>${s.img_busy?'重做中…':'重做图'}</button>
 <button ${audGateLock} class="${s.aud_ok?'ok':'no'}" onclick=gate(${s.idx},'aud',true)>${s.aud_ok?'配音已过 ✓':'配音通过'}</button>
 <button ${s.aud_busy||locked?'disabled':''} onclick=regen(${s.idx},'regen-audio')>${s.aud_busy?'配音中…':'重配音'}</button></div>`;}
async function gate(idx,g,p){try{
 await request(`/api/tasks/${TID}/scenes/${idx}/gate`,
 {method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({gate:g,passed:p})});await load();
 }catch(e){setHint(e.message,'err');}}
async function regen(idx,ep){
 const img=ep==='regen-image';
 if(img&&!confirm('重做这张图会再次调用付费生图服务，是否继续？'))return;
 try{
 await request(`/api/tasks/${TID}/scenes/${idx}/${ep}`,{method:'POST'});
 setHint(img?'重做已发起，完成后自动刷新':'配音已更新，请重新核听','okc');
 await load();
 }catch(e){setHint(e.message,'err');}}
async function retryTask(){try{
 let j=await request(`/api/tasks/${TID}/retry`,{method:'POST'});
 if(j.needs_confirm&&confirm(j.message)){
  j=await request(`/api/tasks/${TID}/retry?force=1`,{method:'POST'});}
 setHint(j.message||'','okc');await load();
 }catch(e){setHint(e.message,'err');}}
async function compose(){try{
 await request(`/api/tasks/${TID}/render`,{method:'POST'});
 setHint('渲染已发起，成片后可在此下载','okc');await load();
 }catch(e){setHint(e.message,'err');}}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
load();setInterval(()=>{
 if(['submitted','generating','rendering'].includes(currentStatus)||lastBusy)load();
},5000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel=icon href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8E%AC%3C/text%3E%3C/svg%3E">
<title>NoteToFilm · 出口成片</title>
<style>
html{color-scheme:light}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:#f5f6f8;color:#1f2329;margin:0;padding:0}
.wrap{max-width:720px;margin:40px auto;padding:0 20px}
textarea{width:100%;box-sizing:border-box;border:1px solid #c9ccd4;border-radius:6px;padding:10px;font-size:14px}
button{cursor:pointer;border:1px solid #3370ff;background:#3370ff;color:#fff;border-radius:6px;padding:8px 22px;margin-top:10px}
#est{color:#686f7a;font-size:13px;min-height:18px;margin-top:6px}
table{border-collapse:collapse;width:100%;background:#fff;margin-top:8px}
th,td{border:1px solid #dee0e3;padding:6px 10px;font-size:14px;text-align:left}
th{background:#eef0f3}
.st{display:inline-block;border-radius:4px;padding:1px 8px;font-size:12px}
.st.done{background:#e8f7ee;color:#157347}
.st.review{background:#fff7e6;color:#b25e00}
.st.failed,.st.interrupted{background:#fdecec;color:#c4340f}
.st.generating,.st.rendering,.st.submitted{background:#e8f0ff;color:#2b6de8}
#out{margin:10px 0;color:#157347}
@media (max-width:640px){
.wrap{margin:20px auto;padding:0 12px}
th,td{padding:5px 8px;font-size:13px}
table{display:block;overflow-x:auto}
.wrap>button{width:100%}
}
</style></head><body><div class=wrap>
<h2>NoteToFilm · 出口成片</h2>
<textarea id=script rows=12 placeholder="粘贴文稿，空行分段（每段一镜）"></textarea>
<div id=est></div>
<button onclick=submit()>提交生成</button><div id=out></div>
<h3>最近任务</h3><table id=tasks></table>
</div>
<script>
const STATUS={submitted:'已提交',generating:'生成中',review:'待复核',rendering:'渲染中',
 done:'已完成',failed:'失败',interrupted:'已中断'};
const ta=document.getElementById('script');
ta.addEventListener('input',()=>{
 const segs=ta.value.split(/\\n\\s*\\n/).map(s=>s.trim()).filter(Boolean);
 document.getElementById('est').textContent=segs.length?`按空行识别 ${segs.length} 段；实际镜数以 AI 分镜结果为准`:'';
});
async function submit(){const s=ta.value.trim();
 if(!s){alert('请输入文稿');return;}
 const r=await fetch('/api/submit',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({script:s})});const j=await r.json();
 if(!r.ok){alert(j.detail||'提交失败');return;}
 document.getElementById('out').innerHTML=
  `任务 ${j.task_id} 已提交：<a href="/tasks/${j.task_id}/review">去复核</a>`;loadTasks();}
async function loadTasks(){const r=await fetch('/api/tasks');const j=await r.json();
 const head='<tr><th>#</th><th>状态</th><th>镜数</th><th>创建时间</th><th>操作</th></tr>';
 if(!j.tasks.length){document.getElementById('tasks').innerHTML=
  head+'<tr><td colspan=5 style="text-align:center;padding:18px;color:#686f7a">暂无任务</td></tr>';return;}
 document.getElementById('tasks').innerHTML=head+
  j.tasks.map(t=>`<tr><td>${t.id}</td><td><span class="st ${t.status}">${STATUS[t.status]||t.status}</span></td><td>${t.scenes}</td>`+
   `<td>${(t.created_at||'').replace('T',' ').slice(0,16)}</td><td><a href="/tasks/${t.id}/review">打开</a></td></tr>`).join('');}
loadTasks();setInterval(loadTasks,5000);
</script></body></html>"""
