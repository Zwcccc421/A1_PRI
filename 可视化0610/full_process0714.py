import requests
import time
import threading
import json
import hashlib
import io
from pathlib import Path
from threading import Lock, Event, Semaphore
from typing import Optional
from pypinyin import pinyin, Style
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import subprocess
import sys
import uuid

# ==================== ⚙️ 全局核心配置区 ⚙️ ====================
PORT = 8001
BASE_URL = f"http://127.0.0.1:{PORT}/api/signal"

ELDER_STREAM_URL = f"http://127.0.0.1:{PORT}/stream/elder"
ROBOT_STREAM_URL = f"http://127.0.0.1:{PORT}/stream/robot"
GRASP_STREAM_URL = f"http://127.0.0.1:{PORT}/stream/grasp"

TASK_JSON_URL = "http://219.223.204.104:18080/a4/final.json"
TASK_JSON_FILE = Path("./task_request.json")
LAST_FILE_MTIME = 0.0
LAST_FILE_MD5 = ""

HAND_RAISE_JSON_URL = "http://219.223.204.104:18080/a4/hand_raise.json"
HAND_RAISE_JSON_FILE = Path("./hand_raise.json")
LAST_HAND_RAISE_MTIME = 0.0
LAST_HAND_RAISE_MD5 = ""

REID_SERVER_IP = "219.223.204.104"
REID_PORT = 8000
REID_API_KEY = "my-secret-key"
REID_THRESHOLD = 0.5

REID_UPLOAD_INTERVAL = 0.5    
NEED_MATCH_TIMES = 4         
REID_TIMEOUT = 10            
FIND_SUCCESS_STAY = 0       

A2_SERVER_URL = "http://10.113.53.104:8004/api/text"
A2_API_KEY = "my-secret-key"

GLOBAL_LOCK = Lock()
A4_SESSION_ID = uuid.uuid4().hex
TASK_RUNNING = False
LISTENING_FOR_TASK = False
CURRENT_TASK_INFO = None
MATCH_SUCCESS_COUNT = 0
HAND_RAISE_LOCKED = False

# A1/A4 已改为直连。旧 A1 HTTP/文件轮询函数保留在本文件中供历史参考，
# 但其启动代码已在主入口处注释，不再执行。

# 全局变量，用于管理发送进程
SENDER_PROCESS = None

# ==================== 8004端口 A2事件回调接收 ====================
WAIT_PERSON_START_SEM = Semaphore(0)  # 找人任务：计数信号量
FETCH_START_EVENT = Event()           # 取物：抓取到达（单次，使用Event即可）
FETCH_COMPLETE_EVENT = Event()        # 取物：抓取完成（单次，使用Event即可）
TASK_END_SEM = Semaphore(0)           # 取物返航/找人结束：计数信号量
# ============================================================

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def parse_a1_task_payload(body: dict) -> Optional[dict]:
    """Extract the final task JSON from A1's text-dispatch envelope."""
    candidate = body
    if isinstance(body.get("text"), str):
        try:
            candidate = json.loads(body["text"])
        except json.JSONDecodeError:
            return None
    if not isinstance(candidate, dict):
        return None
    if candidate.get("task_type") not in {"wait_person", "fetch_object"}:
        return None
    if not candidate.get("task_id"):
        return None
    return candidate


def parse_a1_wake_payload(body: dict) -> Optional[dict]:
    """Extract A1 voice/gesture wake events from the A1 text envelope."""
    candidate = body
    if isinstance(body.get("text"), str):
        try:
            candidate = json.loads(body["text"])
        except json.JSONDecodeError:
            return None
    if not isinstance(candidate, dict):
        return None

    if candidate.get("message_type") == "wake_event":
        return candidate
    if (candidate.get("message_type") == "gesture_result"
            and candidate.get("event_type") == "hand_raise"
            and candidate.get("hand_raised")):
        return candidate
    return None


def submit_a1_task(task_data: dict) -> tuple[bool, str]:
    """Accept one A1 task and run it asynchronously without file polling."""
    global TASK_RUNNING, LISTENING_FOR_TASK, CURRENT_TASK_INFO
    with GLOBAL_LOCK:
        if TASK_RUNNING:
            return False, "A4 already has a task in progress"
        TASK_RUNNING = True
        LISTENING_FOR_TASK = False
        CURRENT_TASK_INFO = task_data

    def run_task() -> None:
        global TASK_RUNNING, CURRENT_TASK_INFO
        try:
            task_type = task_data["task_type"]
            if task_type == "wait_person":
                handle_wait_person_task(task_data)
            else:
                handle_fetch_object_task(task_data)
        finally:
            with GLOBAL_LOCK:
                TASK_RUNNING = False
                CURRENT_TASK_INFO = None

    threading.Thread(target=run_task, daemon=True, name="a1-task-dispatch").start()
    return True, "A1 task accepted"


def begin_listening(text: str) -> bool:
    """Ask once per wake session; repeated wake events do not replay the video."""
    global LISTENING_FOR_TASK
    with GLOBAL_LOCK:
        if TASK_RUNNING or LISTENING_FOR_TASK:
            return False
        LISTENING_FOR_TASK = True
    if not send_signal(event="listen", text=text):
        with GLOBAL_LOCK:
            if not TASK_RUNNING:
                LISTENING_FOR_TASK = False
        return False
    return True

class ReportHandler(BaseHTTPRequestHandler):
    server_version = "G1ReportReceiver/1.0"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/a1/status":
            with GLOBAL_LOCK:
                task = CURRENT_TASK_INFO or {}
                status = {
                    "ok": True,
                    "session_id": A4_SESSION_ID,
                    "task_running": TASK_RUNNING,
                    "listening_for_task": LISTENING_FOR_TASK,
                    "task_id": task.get("task_id"),
                    "task_type": task.get("task_type"),
                }
            self._send_json(200, status)
            return
        self._send_json(200, {"ok": True, "msg": "A2事件回调服务存活中"})

    # def do_POST(self):
    #     length = int(self.headers.get("Content-Length", 0) or 0)
    #     raw = self.rfile.read(length) if length > 0 else b""

    #     log("=" * 60)
    #     log(f"收到A2回调 POST {self.path}  from {self.client_address[0]}")

    #     parsed = None
    #     try:
    #         parsed = json.loads(raw.decode("utf-8")) if raw else None
    #     except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    #         log(f"  [警告] JSON 解析失败: {exc}")

    #     if isinstance(parsed, dict):
    #         log(f"  [A2原始报文] {parsed}")
    #         raw_event = parsed.get("event") or parsed.get("action") or parsed.get("status") or parsed.get("msg") or parsed.get("text") or ""
            
    #         event = str(raw_event).strip().replace("_", "-").lower()
            
    #         if event:
    #             log(f"  [解析匹配键] {event}")

    #         if event == "wait-person-start":
    #             WAIT_PERSON_START_SEM.release()
    #             log("  ✅ 触发信号：收到 wait-person-start (到达最终点位)")
    #         elif event == "door-near":
    #             WAIT_PERSON_START_SEM.release()           
    #             TASK_END_SEM.release()          
    #             log(f"  ✅ 触发信号：收到 door-near (途径门口节点+1)")
    #         elif event == "fetch-start":
    #             FETCH_START_EVENT.set()
    #             log("  ✅ 触发信号：切换抓取视角")
    #         elif event == "fetch-complete":
    #             FETCH_COMPLETE_EVENT.set()
    #             log("  ✅ 触发信号：抓取完成，重启导航")
    #         elif event == "task-end":
    #             TASK_END_SEM.release()
    #             log("  ✅ 触发信号：收到 task-end (返航/结束节点+1)")
    #         else:
    #             log(f"  ⚠️ 未处理的事件类型: {raw_event}")

    #     self._send_json(200, {
    #         "ok": True,
    #         "received": parsed,
    #         "server_time": datetime.now().isoformat(timespec="seconds"),
    #     })
    def do_POST(self):
        global TASK_RUNNING # ⚠️ 必须声明全局变量，才能在收到模拟任务时改变状态

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""

        log("=" * 60)
        log(f"收到A2回调 POST {self.path}  from {self.client_address[0]}")

        parsed = None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log(f"  [警告] JSON 解析失败: {exc}")

        # 新的 A1 直连入口：接收 TeamDispatcher._post_a4_text 的封装报文。
        # 该入口不依赖 /a4/final.json、task_request.json 或文件 MD5 轮询。
        if self.path == "/a1/task":
            if not isinstance(parsed, dict):
                self._send_json(400, {"ok": False, "error": "JSON object required"})
                return
            task_data = parse_a1_task_payload(parsed)
            if task_data is not None:
                accepted, message = submit_a1_task(task_data)
                log(f"[A1 任务] id={task_data['task_id']} type={task_data['task_type']} accepted={accepted} {message}")
                self._send_json(202 if accepted else 409, {
                    "ok": accepted,
                    "message": message,
                    "task_id": task_data["task_id"],
                })
                return

            wake_data = parse_a1_wake_payload(parsed)
            if wake_data is not None:
                wake_text = wake_data.get("response_text") or "您好，我在呢，请说。"
                log(
                    "[A1 唤醒] "
                    f"trigger={wake_data.get('trigger_type') or wake_data.get('event_type')} "
                    f"recipient={wake_data.get('care_recipient_id')}"
                )
                # Reuse the existing dashboard state used by the local hand-up simulator.
                prompted = begin_listening(wake_text)
                self._send_json(202, {
                    "ok": True,
                    "message": "A1 wake event accepted",
                    "trigger_type": wake_data.get("trigger_type") or wake_data.get("event_type"),
                    "prompted": prompted,
                })
                return

            self._send_json(400, {"ok": False, "error": "invalid A1 payload"})
            return

        if isinstance(parsed, dict):
            log(f"  [A2原始报文] {parsed}")
            raw_event = parsed.get("event") or parsed.get("action") or parsed.get("status") or parsed.get("msg") or parsed.get("text") or ""
            
            event = str(raw_event).strip().replace("_", "-").lower()
            
            if event:
                log(f"  [解析匹配键] {event}")

            if event == "wait-person-start":
                WAIT_PERSON_START_SEM.release()
                log("  ✅ 触发信号：收到 wait-person-start (到达最终点位)")
            elif event == "door-near":
                WAIT_PERSON_START_SEM.release()           
                TASK_END_SEM.release()          
                log(f"  ✅ 触发信号：收到 door-near (途径门口节点+1)")
            elif event == "fetch-start":
                FETCH_START_EVENT.set()
                log("  ✅ 触发信号：切换抓取视角")
            elif event == "fetch-complete":
                FETCH_COMPLETE_EVENT.set()
                log("  ✅ 触发信号：抓取完成，重启导航")
            elif event == "task-end":
                TASK_END_SEM.release()
                log("  ✅ 触发信号：收到 task-end (返航/结束节点+1)")
            
            # ================= 新增：处理模拟器的三个手动信号 =================
            elif event == "hand-up":
                log("  ✅ 触发信号：收到模拟器老人举手")
                # 直接通过 8001 端口把信号中转给大屏
                begin_listening("张阿姨，请问有什么需要帮忙的吗？")
            
            elif event == "command":
                text = parsed.get("text", "")
                log(f"  ✅ 触发信号：收到模拟任务指令 -> {text}")
                with GLOBAL_LOCK:
                    if TASK_RUNNING:
                        log("  ⚠️ 当前已有任务正在运行，忽略本次模拟指令")
                    else:
                        TASK_RUNNING = True
                        if "徐静" in text or "找" in text:
                            # 模拟构造一个找人任务并启动线程
                            task_data = {"task_id": "mock_find_999", "task_type": "wait_person", "target_person": "徐静"}
                            threading.Thread(target=handle_wait_person_task, args=(task_data,), daemon=True).start()
                        elif "咖啡" in text or "拿" in text:
                            # 模拟构造一个取物任务并启动线程
                            task_data = {"task_id": "mock_fetch_999", "task_type": "fetch_object", "target_object": "咖啡"}
                            threading.Thread(target=handle_fetch_object_task, args=(task_data,), daemon=True).start()
            # ===============================================================
            
            else:
                log(f"  ⚠️ 未处理的事件类型: {raw_event}")

        self._send_json(200, {
            "ok": True,
            "received": parsed,
            "server_time": datetime.now().isoformat(timespec="seconds"),
        })

def start_global_report_server():
    port = 8004
    host = "0.0.0.0"
    def run_server():
        try:
            httpd = ThreadingHTTPServer((host, port), ReportHandler)
            log(f"A2事件回调服务已启动: http://{host}:{port}/")
            httpd.serve_forever()
        except Exception as e:
            log(f"回调服务启动异常: {str(e)}")
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

def get_name_initials(name: str) -> str:
    try:
        initial_list = pinyin(name, style=Style.FIRST_LETTER, errors='default')
        initials = ''.join([item[0] for item in initial_list]).lower()
        return initials
    except Exception as e:
        return name.lower()

def get_file_md5(file_path: Path) -> str:
    if not file_path.exists():
        return ""
    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()

def fetch_task_json_from_url() -> bool:
    try:
        resp = requests.get(TASK_JSON_URL, timeout=10)
        resp.raise_for_status()
        json_data = resp.json()
        with open(TASK_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        return True
    except:
        return False

def fetch_hand_raise_json_from_url() -> bool:
    try:
        resp = requests.get(HAND_RAISE_JSON_URL, timeout=10)
        resp.raise_for_status()
        json_data = resp.json()
        with open(HAND_RAISE_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        return True
    except:
        return False

def check_hand_raise_update() -> bool:
    global LAST_HAND_RAISE_MTIME, LAST_HAND_RAISE_MD5
    if not HAND_RAISE_JSON_FILE.exists():
        return False
    current_mtime = HAND_RAISE_JSON_FILE.stat().st_mtime
    current_md5 = get_file_md5(HAND_RAISE_JSON_FILE)
    if current_mtime > LAST_HAND_RAISE_MTIME or current_md5 != LAST_HAND_RAISE_MD5:
        LAST_HAND_RAISE_MTIME = current_mtime
        LAST_HAND_RAISE_MD5 = current_md5
        return True
    return False

def parse_task_json() -> Optional[dict]:
    try:
        with open(TASK_JSON_FILE, "r", encoding="utf-8") as f:
            outer_data = json.load(f)
        inner_json_str = outer_data.get("text", "{}")
        inner_data = json.loads(inner_json_str)
        task_data = {**outer_data, **inner_data}
        return task_data
    except:
        return None

def get_robot_stream_frame_bytes() -> Optional[bytes]:
    try:
        resp = requests.get(ROBOT_STREAM_URL, stream=True, timeout=10)
        resp.raise_for_status()
        buffer = b""
        frame_header = b'\r\nContent-Type: image/jpeg\r\n\r\n'
        frame_boundary = b'\r\n--frame'
        for chunk in resp.iter_content(chunk_size=8192):
            buffer += chunk
            if frame_header in buffer and frame_boundary in buffer:
                start_idx = buffer.find(frame_header) + len(frame_header)
                end_idx = buffer.find(frame_boundary, start_idx)
                full_jpeg = buffer[start_idx:end_idx].rstrip(b'\r\n')
                resp.close()
                if len(full_jpeg) > 100 and full_jpeg.startswith(b"\xff\xd8") and full_jpeg.endswith(b"\xff\xd9"):
                    return full_jpeg
                else:
                    return None
        resp.close()
        return None
    except:
        return None

def call_reid_api(frame_bytes: bytes, threshold=0.5):
    url = f"http://{REID_SERVER_IP}:{REID_PORT}/api/reid"
    headers = {"X-API-Key": REID_API_KEY}
    try:
        img_io = io.BytesIO(frame_bytes)
        img_io.name = "robot_frame.jpg"
        files = {"file": (img_io.name, img_io, "image/jpeg")}
        data = {
            "threshold": str(threshold),
            "meta": json.dumps({"source": "robot-client", "data_type": "image"}, ensure_ascii=False)
        }
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"status": "error", "error": f"ReID请求异常: {str(e)}"}

def reid_recognize() -> Optional[str]:
    frame_bytes = get_robot_stream_frame_bytes()
    if not frame_bytes:
        return None

    reid_result = call_reid_api(frame_bytes, REID_THRESHOLD)
    if reid_result.get("status") != "ok":
        log(f"[ReID服务报错] {reid_result.get('error', '未知错误')}")
        return None

    person_name = reid_result.get("person_name", "") or ""
    score = float(reid_result.get("score") or 0.0)
    elapsed = float(reid_result.get("elapsed_sec") or 0.0)

    log(f"[ReID识别] 匹配结果:{person_name} | 置信度:{score:.3f} | 耗时:{elapsed:.3f}s")
    return person_name.lower()

def send_signal(event: str, text: Optional[str] = None, value: Optional[int] = None,
                task_type: Optional[str] = None, task_id: Optional[str] = None) -> bool:
    payload = {"event": event, "text": text, "value": value}
    if task_type:
        payload.update(task_type=task_type, task_id=task_id)
    try:
        response = requests.post(BASE_URL, json=payload, timeout=5)
        response.raise_for_status()
        log(f"[大屏信号] event={event} task_id={task_id} response={response.text[:300]}")
        return True
    except requests.RequestException as exc:
        log(f"[大屏信号失败] event={event} task_id={task_id}: {exc}")
        return False

def send_result_to_a2(result: str, task_type: str = "wait_person"):
    headers = {"X-API-Key": A2_API_KEY}
    payload = {
        "text": result,
        "source": "server-A",
        "meta": {"task": task_type}
    }
    try:
        requests.post(A2_SERVER_URL, json=payload, headers=headers, timeout=30)
        log(f"向A2发送{task_type}结果: {result}")
    except Exception as e:
        log(f"向A2发送结果失败: {str(e)}")

# ==================== 业务逻辑：找人任务 ====================
def handle_wait_person_task(task_data: dict):
    global TASK_RUNNING, CURRENT_TASK_INFO, MATCH_SUCCESS_COUNT, SENDER_PROCESS
    task_id = task_data.get("task_id", "unknown")
    target_person = task_data.get("target_person", "")

    try:
        log(f"========== 开始找人任务 ID:{task_id} 目标:{target_person} ==========")
        target_initials = get_name_initials(target_person)
        
        CURRENT_TASK_INFO = task_data
        MATCH_SUCCESS_COUNT = 0

        # 清空历史残留的信号量
        while WAIT_PERSON_START_SEM.acquire(blocking=False): pass
        while TASK_END_SEM.acquire(blocking=False): pass

        send_signal(event="command", text=f"收到指令，正在寻找{target_person}",
                    task_type="wait_person", task_id=task_id)

        # ===== 第一阶段：等待 A2 发送第一次到达信号（门口） =====
        log("等待A2到达门口信号...")
        if not WAIT_PERSON_START_SEM.acquire(timeout=900):
            log("[找人任务] 等待到达门口超时，任务终止")
            send_signal(event="task_finished", text="等待到达门口超时，任务终止")
            send_result_to_a2("0", "wait_person")
            return

        log("✅ 机器人已到门口，大屏切换进门后视频")
        send_signal(event="arrive_door", text="已到达门口，正在进入休息室...")

        # ===== 第二阶段：等待 A2 发送第二次到达信号（找人点位） =====
        log("等待A2到达观测点位信号...")
        if not WAIT_PERSON_START_SEM.acquire(timeout=900):
            log("[找人任务] 等待到达点位超时，任务终止")
            send_signal(event="task_finished", text="等待到达点位超时，任务终止")
            send_result_to_a2("0", "wait_person")
            return

        log("✅ 已到达找人点位，启动人脸识别，切换机器人视角")
        send_signal(event="arrive_point", text="到达观测点位，正在执行视觉识别...")

        find_success = False
        start_time = time.time()

        while TASK_RUNNING and (time.time() - start_time) < REID_TIMEOUT:
            try:
                rec_person = reid_recognize()
            except Exception as e:
                time.sleep(REID_UPLOAD_INTERVAL)
                continue

            if not rec_person:
                time.sleep(REID_UPLOAD_INTERVAL)
                continue

            if rec_person == target_initials:
                MATCH_SUCCESS_COUNT += 1
                log(f"匹配成功 连续次数: {MATCH_SUCCESS_COUNT}/{NEED_MATCH_TIMES}")
                send_signal(event="reid_once", text=f"正在识别中... 已匹配 {MATCH_SUCCESS_COUNT}/{NEED_MATCH_TIMES} 次")
                
                if MATCH_SUCCESS_COUNT >= NEED_MATCH_TIMES:
                    log(f"✅ 锁定目标人员: {target_person}")
                    send_signal(event="target_found", text=f"识别成功，鹏鹏已确认{target_person}位置")
                    
                    # === 新增：成功找到人，启动语音发送脚本 ===
                    log("正在启动语音发送脚本 (audio_sender)...")
                    try:
                        # 确保路径正确，如果不在同一目录下请写绝对路径
                        SENDER_PROCESS = subprocess.Popen([sys.executable, "audio_sender.py", "--backend", "parec"])
                    except Exception as e:
                        log(f"启动发送脚本失败: {e}")
                    # =========================================

                    time.sleep(5) 
                    send_result_to_a2("1", "wait_person")
                    find_success = True
                    time.sleep(FIND_SUCCESS_STAY)
                    break
            else:
                MATCH_SUCCESS_COUNT = 0

            time.sleep(REID_UPLOAD_INTERVAL)

        if not find_success:
            log(f"[找人任务] {REID_TIMEOUT}秒超时，未找到目标")
            send_result_to_a2("0", "wait_person")
            send_signal(event="reid_fail", text="识别超时，未找到目标人员")
            log(f"========== 找人任务 {task_id} 执行完毕(失败) ==========")
            return

        log("等待A2的任务结束回调信号...")
        if TASK_END_SEM.acquire(timeout=900):
            send_signal(event="task_finished", text="任务顺利完成，正在返航")
        else:
            log("❌ 等待任务结束信号超时")
            send_signal(event="task_finished", text="等待结束超时，自动返回待机")
            
        log(f"========== 找人任务 {task_id} 执行完毕 ==========")

    except Exception as e:
        log(f"[找人任务异常] ID:{task_id} 错误:{str(e)}")
        send_signal(event="task_finished", text="任务异常终止")
    finally:
        # === 新增：清理逻辑，防止占用声卡 ===
        if SENDER_PROCESS:
            log("正在关闭语音发送脚本...")
            SENDER_PROCESS.terminate()
            SENDER_PROCESS = None
        # ==================================
        with GLOBAL_LOCK:
            TASK_RUNNING = False
            CURRENT_TASK_INFO = None

# ==================== 业务逻辑：取物任务 ====================
def handle_fetch_object_task(task_data: dict):
    global TASK_RUNNING, CURRENT_TASK_INFO
    task_id = task_data.get("task_id", "unknown")
    target_object = task_data.get("target_object", "")

    try:
        log(f"========== 开始取物任务 ID:{task_id} 目标物品:{target_object} ==========")
        CURRENT_TASK_INFO = task_data

        FETCH_START_EVENT.clear()
        FETCH_COMPLETE_EVENT.clear()
        
        # 清空历史残留的返航信号量
        while TASK_END_SEM.acquire(blocking=False): pass

        send_signal(event="command", text=f"收到！正在帮您取{target_object}，请稍等",
                    task_type="fetch_object", task_id=task_id)

        wait_fetch_start = FETCH_START_EVENT.wait(timeout=900)
        if not wait_fetch_start:
            log("[取物任务] 等待到达点位超时，任务终止")
            send_signal(event="task_finished", text="等待到达取物点位超时，任务终止")
            return

        log("✅ 已到达取物点位，大屏左侧切换为抓取视角")
        send_signal(event="arrive_point", text="到达目标点位，机器人正在抓取咖啡...")

        wait_fetch_complete = FETCH_COMPLETE_EVENT.wait(timeout=900)
        if not wait_fetch_complete:
            log("[取物任务] 等待抓取完成超时，任务终止")
            send_signal(event="task_finished", text="等待抓取完成超时，任务终止")
            return

        log("✅ 抓取完成，大屏左侧切回虚拟人待机画面")
        send_signal(event="grab_finish", text="抓取完成，机器人正在返回...")

        # 吸收 A2 抓取完毕后可能紧跟着发的一个伪结束信号
        log("等待吸收抓取子任务阶段的伪结束信号 (task_end)...")
        if TASK_END_SEM.acquire(timeout=5):
            log("✅ 已吸收抓取子任务阶段的伪结束信号")

        # ===== 返航阶段 1：等待到达 Door1 =====
        log("等待到达走廊门口 (door1)...")
        if not TASK_END_SEM.acquire(timeout=600):
            send_signal(event="task_finished", text="返航异常终止")
            return
        log("✅ 已到达 door1，大屏切换走廊视频")
        send_signal(event="arrive_door_1", text="已穿过门口，进入走廊...")

        # ===== 返航阶段 2：等待到达 Door2 (210门口) =====
        log("等待到达210门口 (door2)...")
        if not TASK_END_SEM.acquire(timeout=600):
            send_signal(event="task_finished", text="返航异常终止")
            return
        log("✅ 已到达 door2，大屏切换进210视频")
        send_signal(event="arrive_door_2", text="到达210门前，正在进入...")

        # ===== 返航阶段 3：等待最终送达目的地 =====
        log("等待任务最终彻底结束信号...")
        if not TASK_END_SEM.acquire(timeout=600):
            send_signal(event="task_finished", text="返航异常终止")
            return
        
        log("✅ 收到真正的任务结束信号 (包含返航与投放已彻底完成)")
        send_signal(event="fetch_task_end", text="咖啡已送达，准备返回待机")

        log(f"========== 取物任务 {task_id} 执行完毕 ==========")

    except Exception as e:
        log(f"[取物任务异常] ID:{task_id} 错误:{str(e)}")
        send_signal(event="task_finished", text="任务异常终止")
    finally:
        with GLOBAL_LOCK:
            TASK_RUNNING = False
            CURRENT_TASK_INFO = None

# ==================== 业务逻辑：举手检测监听 ====================
def hand_raise_monitor():
    global HAND_RAISE_LOCKED
    log("举手检测监听线程已启动")
    
    while True:
        try:
            if TASK_RUNNING:
                HAND_RAISE_LOCKED = False
                time.sleep(1)
                continue

            if fetch_hand_raise_json_from_url():
                with open(HAND_RAISE_JSON_FILE, "r", encoding="utf-8") as f:
                    hand_data = json.load(f)
                
                is_raising = hand_data.get("hand_raised", False)
                care_recipient_id = hand_data.get("care_recipient_id", "")

                if is_raising and not HAND_RAISE_LOCKED:
                    log(f"检测到新举手事件: {care_recipient_id}")
                    send_signal(event="listen", text=f"{care_recipient_id}，请问有什么需要帮忙的吗？")
                    HAND_RAISE_LOCKED = True 
                elif not is_raising:
                    HAND_RAISE_LOCKED = False
        
        except Exception as e:
            pass # 屏蔽网络波动时的日志干扰
        
        time.sleep(1)

# ==================== 任务拉取与分发 ====================
def file_monitor_and_task_dispatcher():
    global TASK_RUNNING
    log("系统启动，进入待机状态")
    send_signal(event="task_standby", text="系统待机中，请下达指令")

    # 记录上一次执行过的任务文件的 MD5 值
    last_task_md5 = ""
    skip_initial_task = True

    while True:
        if not TASK_RUNNING:
            if fetch_task_json_from_url():
                # 计算当前拉取到的文件 MD5
                current_md5 = get_file_md5(TASK_JSON_FILE)

                # 刚开机时，跳过服务器上残留的旧任务，仅记录当前的 MD5 作为初始基准
                if skip_initial_task:
                    last_task_md5 = current_md5
                    skip_initial_task = False
                    time.sleep(1)
                    continue

                with GLOBAL_LOCK:
                    # 如果文件内容的 MD5 没变，说明 A1 还没下发新指令
                    if current_md5 == last_task_md5:
                        time.sleep(1)
                        continue
                    
                    # 发现 MD5 变了，说明是新指令！记录新的 MD5 并上锁
                    last_task_md5 = current_md5
                    TASK_RUNNING = True

                # 开始解析并执行新任务
                task_data = parse_task_json()
                if task_data:
                    task_id = task_data.get("task_id", "unknown")
                    task_type = task_data.get("task_type", "")
                    
                    log(f"[调度中心] 接收到新指令: 任务类型={task_type}, ID={task_id}")

                    if task_type == "wait_person":
                        threading.Thread(target=handle_wait_person_task, args=(task_data,), daemon=True).start()
                    elif task_type == "fetch_object":
                        threading.Thread(target=handle_fetch_object_task, args=(task_data,), daemon=True).start()
                    else:
                        with GLOBAL_LOCK:
                            TASK_RUNNING = False
                else:
                    with GLOBAL_LOCK:
                        TASK_RUNNING = False

        time.sleep(1)

# ==================== 程序入口 ====================
if __name__ == "__main__":
    start_global_report_server()
    # 旧 A1 轮询启动代码（保留但不执行）：
    # threading.Thread(target=hand_raise_monitor, daemon=True).start()
    # file_monitor_and_task_dispatcher()

    log("直连模式：等待 A1 POST /a1/task")
    send_signal(event="task_standby", text="系统待机中，等待 A1 任务")
    while True:
        time.sleep(1)
