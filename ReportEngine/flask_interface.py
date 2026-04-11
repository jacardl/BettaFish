"""
Report Engine Flask接口。

该模块为前端/CLI提供统一HTTP/SSE入口，负责：
1. 初始化 ReportAgent 并串联后台线程；
2. 管理任务排队、进度查询、流式推送与日志下载；
3. 提供模板列表、输入文件检查等周边能力。
"""

import os
import json
import threading
import time
from collections import deque, defaultdict
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from flask import Blueprint, request, jsonify, Response, send_file, stream_with_context
from typing import Dict, Any, List, Optional
from loguru import logger
from .agent import ReportAgent, create_agent
from .nodes import ChapterJsonParseError
from .utils.config import settings


from werkzeug.utils import secure_filename
import docx
import fitz  # PyMuPDF

# 创建Blueprint
report_bp = Blueprint('report_engine', __name__)

# 全局变量
report_agent = None
current_task = None
task_lock = threading.Lock()

# ====== 流式推送与任务历史管理 ======
# 通过有界deque缓存最近的事件，方便SSE断线后快速补发
MAX_TASK_HISTORY = 5
STREAM_HEARTBEAT_INTERVAL = 15  # 心跳间隔秒
STREAM_IDLE_TIMEOUT = 120  # 终态后最长保活时间，避免孤儿SSE阻塞
STREAM_TERMINAL_STATUSES = {"completed", "error", "cancelled"}
stream_lock = threading.Lock()
stream_subscribers = defaultdict(list)
tasks_registry: Dict[str, 'ReportTask'] = {}
LOG_STREAM_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
log_stream_handler_id: Optional[int] = None

EXCLUDED_ENGINE_PATH_KEYWORDS = ("ForumEngine", "InsightEngine", "MediaEngine", "QueryEngine")

def _is_excluded_engine_log(record: Dict[str, Any]) -> bool:
    """
    判断日志是否来自其他引擎（Insight/Media/Query/Forum），用于过滤混入的日志。

    返回:
        bool: True 表示应当过滤（即不写入/不转发）。
    """
    try:
        file_path = record["file"].path
        if any(keyword in file_path for keyword in EXCLUDED_ENGINE_PATH_KEYWORDS):
            return True
    except Exception:
        pass

    # 兜底：尝试按模块名过滤，防止file信息缺失时误混入
    try:
        module_name = record.get("module", "")
        if isinstance(module_name, str):
            lowered = module_name.lower()
            return any(keyword.lower() in lowered for keyword in EXCLUDED_ENGINE_PATH_KEYWORDS)
    except Exception:
        pass

    return False


def _stream_log_to_task(message):
    """
    将loguru日志同步到当前任务的SSE事件，保证前端实时可见。

    仅在存在运行中的任务时推送，避免无关日志刷屏。
    """
    try:
        record = message.record
        level_name = record["level"].name
        if level_name not in LOG_STREAM_LEVELS:
            return
        if _is_excluded_engine_log(record):
            return

        with task_lock:
            task = current_task

        if not task or task.status not in ("running", "pending"):
            return

        timestamp = record["time"].strftime("%H:%M:%S.%f")[:-3]
        formatted_line = f"[{timestamp}] [{level_name}] {record['message']}"
        task.publish_event(
            "log",
            {
                "line": formatted_line,
                "level": level_name.lower(),
                "timestamp": timestamp,
                "message": record["message"],
                "module": record.get("module", ""),
                "function": record.get("function", ""),
            },
        )
    except Exception:
        # 避免在日志钩子里产生日志递归
        pass


def _setup_log_stream_forwarder():
    """为当前进程挂载一次性的loguru钩子，用于SSE实时转发。"""
    global log_stream_handler_id
    if log_stream_handler_id is not None:
        return
    log_stream_handler_id = logger.add(
        _stream_log_to_task,
        level="DEBUG",
        enqueue=False,
        catch=True,
    )


def _register_stream(task_id: str) -> Queue:
    """
    为指定任务注册一个事件队列，供SSE监听器消费。

    返回的 Queue 会存入 `stream_subscribers`，SSE 生成器将不断读取。

    参数:
        task_id: 需要监听的任务ID。

    返回:
        Queue: 线程安全的事件队列。
    """
    queue = Queue()
    with stream_lock:
        stream_subscribers[task_id].append(queue)
    return queue


def _unregister_stream(task_id: str, queue: Queue):
    """
    安全移除事件队列，避免内存泄漏。

    需要在finally中调用，保证异常情况下资源也能释放。

    参数:
        task_id: 任务ID。
        queue: 之前注册的事件队列。
    """
    with stream_lock:
        listeners = stream_subscribers.get(task_id, [])
        if queue in listeners:
            listeners.remove(queue)
        if not listeners and task_id in stream_subscribers:
            stream_subscribers.pop(task_id, None)


def _broadcast_event(task_id: str, event: Dict[str, Any]):
    """
    将事件推送给所有监听者，失败时做好异常捕获。

    采用浅拷贝监听列表，防止并发移除导致遍历异常。

    参数:
        task_id: 待推送的任务ID。
        event: 结构化事件payload。
    """
    with stream_lock:
        listeners = list(stream_subscribers.get(task_id, []))
    for queue in listeners:
        try:
            queue.put(event, timeout=0.1)
        except Exception:
            logger.exception("推送流式事件失败，跳过当前监听队列")


def _prune_task_history_locked():
    """
    在task_lock持有期间调用，清理过多的历史任务。

    仅保留最近 `MAX_TASK_HISTORY` 个任务，避免长时间运行占用过多内存。

    说明:
        该函数假设调用方已获取 `task_lock`，否则存在竞态风险。
    """
    if len(tasks_registry) <= MAX_TASK_HISTORY:
        return
    # 按创建时间排序，移除最旧的任务
    sorted_tasks = sorted(tasks_registry.values(), key=lambda t: t.created_at)
    for task in sorted_tasks[:-MAX_TASK_HISTORY]:
        tasks_registry.pop(task.task_id, None)


def _get_task(task_id: str) -> Optional['ReportTask']:
    """
    统一的任务查找方法，优先返回当前任务。

    避免重复写锁逻辑，便于多个API共享。

    参数:
        task_id: 任务ID。

    返回:
        ReportTask | None: 命中时返回任务实例，否则为None。
    """
    with task_lock:
        if current_task and current_task.task_id == task_id:
            return current_task
        task = tasks_registry.get(task_id)
        if task:
            return task
            
    # 如果是基于文件恢复的历史任务，临时构建一个Task对象返回
    if task_id.startswith('recovered_file_') and report_agent:
        output_dir = Path(report_agent.config.OUTPUT_DIR)
        if output_dir.exists():
            filename = task_id.replace('recovered_file_', '')
            target_html = output_dir / filename
            
            if target_html.exists():
                temp_task = ReportTask("历史任务 (从文件恢复)", task_id)
                temp_task.status = "completed"
                temp_task.progress = 100
                temp_task.report_file_ready = True
                temp_task.report_file_path = str(target_html.resolve())
                temp_task.report_file_name = target_html.name
                temp_task.created_at = datetime.fromtimestamp(target_html.stat().st_ctime)
                temp_task.updated_at = datetime.fromtimestamp(target_html.stat().st_mtime)
                
                try:
                    temp_task.html_content = target_html.read_text(encoding="utf-8")
                except Exception:
                    pass
                    
                base_name_parts = target_html.stem.replace('final_report_', '').split('_')
                if len(base_name_parts) >= 2:
                    timestamp = f"{base_name_parts[-2]}_{base_name_parts[-1]}"
                    query_safe = '_'.join(base_name_parts[:-2])
                    
                    ir_filename = f"report_ir_{query_safe}_{timestamp}.json"
                    ir_path = Path(report_agent.config.DOCUMENT_IR_OUTPUT_DIR) / ir_filename
                    if ir_path.exists():
                        temp_task.ir_file_ready = True
                        temp_task.ir_file_path = str(ir_path.resolve())
                        
                    state_filename = f"report_state_{query_safe}_{timestamp}.json"
                    state_path = output_dir / state_filename
                    if state_path.exists():
                        temp_task.state_file_ready = True
                        temp_task.state_file_path = str(state_path.resolve())
                        try:
                            state_data = json.loads(state_path.read_text(encoding="utf-8"))
                            
                            # 尝试从state中获取
                            insight_report = state_data.get('insight_engine_report', '')
                            media_report = state_data.get('media_engine_report', '')
                            query_report = state_data.get('query_engine_report', '')
                            forum_logs = state_data.get('forum_logs', '')
                            
                            # 如果为空，尝试去三个引擎的目录里找对应的文件
                            # query_safe 的格式如：游戏达巴_水痕之地
                            # 各引擎目录的报告前缀是 deep_search_report_，匹配查询词开头
                            if not insight_report:
                                insight_report = _find_engine_report('insight', query_safe)
                            if not media_report:
                                media_report = _find_engine_report('media', query_safe)
                            if not query_report:
                                query_report = _find_engine_report('query', query_safe)

                            temp_task.history_inputs = {
                                'insight': insight_report,
                                'media': media_report,
                                'query': query_report,
                                'forum': forum_logs
                            }
                        except Exception as e:
                            logger.warning(f"读取历史任务状态文件失败 {state_path}: {e}")
                        
                return temp_task
    return None

def _find_engine_report(engine_name: str, query_safe: str) -> str:
    """
    根据引擎名称和查询词前缀，在对应的目录中寻找最新的 md 报告并读取内容。
    """
    engine_dir = Path(f"{engine_name}_engine_streamlit_reports")
    if not engine_dir.exists():
        return ""
        
    md_files = list(engine_dir.glob(f"deep_search_report_{query_safe}*.md"))
    if md_files:
        # 如果找到多个，按修改时间取最新的
        latest_file = max(md_files, key=lambda p: p.stat().st_mtime)
        try:
            return latest_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"读取 {engine_name} 引擎报告文件失败 {latest_file}: {e}")
    return ""


def _format_sse(event: Dict[str, Any]) -> str:
    """
    按SSE协议格式化消息。

    输出形如 `id:/event:/data:` 的三段文本，供浏览器端直接消费。

    参数:
        event: 事件payload，至少包含 id/type。

    返回:
        str: SSE协议要求的字符串。
    """
    payload = json.dumps(event, ensure_ascii=False)
    event_id = event.get('id', 0)
    event_type = event.get('type', 'message')
    return f"id: {event_id}\nevent: {event_type}\ndata: {payload}\n\n"


def _safe_filename_segment(value: str, fallback: str = "report") -> str:
    """
    生成可用于文件名的安全片段，保留字母数字与常见分隔符。

    参数:
        value: 原始字符串。
        fallback: 兜底文本，当value为空或清洗后为空时使用。
    """
    sanitized = "".join(c for c in str(value) if c.isalnum() or c in (" ", "-", "_")).strip()
    sanitized = sanitized.replace(" ", "_")
    return sanitized or fallback


def initialize_report_engine():
    """
    初始化Report Engine。

    单例化 ReportAgent，方便 API 启动后直接接收任务。

    返回:
        bool: 初始化成功返回True，异常时返回False。
    """
    global report_agent
    try:
        report_agent = create_agent()
        logger.info("Report Engine初始化成功")
        _setup_log_stream_forwarder()

        # 检测 PDF 生成依赖（Pango）
        try:
            from .utils.dependency_check import log_dependency_status
            log_dependency_status()
        except Exception as dep_err:
            logger.warning(f"依赖检测失败: {dep_err}")

        return True
    except Exception as e:
        logger.exception(f"Report Engine初始化失败: {str(e)}")
        return False


class CancelGenerationException(Exception):
    """用户主动取消生成的异常"""
    pass

class ReportTask:
    """
    报告生成任务。

    该对象串联运行状态、进度、事件历史及最终文件路径，
    既供后台线程更新，也供HTTP接口读取。
    """

    def __init__(self, query: str, task_id: str, custom_template: str = ""):
        """
        初始化任务对象，记录查询词、自定义模板与运行期元数据。

        Args:
            query: 最终需要生成的报告主题
            task_id: 任务唯一ID，通常由时间戳构造
            custom_template: 可选的自定义Markdown模板
        """
        self.task_id = task_id
        self.query = query
        self.custom_template = custom_template
        self.seed_context = ""
        self.seed_filename = ""
        self.seed_url = ""
        self.status = "pending"  # 四种状态（pending/running/completed/error）
        self.progress = 0
        self.result = None
        self.error_message = ""
        self.is_cancelled = False
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
        self.html_content = ""
        self.report_file_path = ""
        self.report_file_relative_path = ""
        self.report_file_name = ""
        self.state_file_path = ""
        self.state_file_relative_path = ""
        self.ir_file_path = ""
        self.ir_file_relative_path = ""
        self.markdown_file_path = ""
        self.markdown_file_relative_path = ""
        self.markdown_file_name = ""
        self.history_inputs = {}  # 保存从状态文件中恢复的三个引擎的历史数据
        # ====== 流式事件缓存与并发保护 ======
        # 使用deque保存最近的事件，结合锁保证多线程下的安全访问
        self.event_history: deque = deque(maxlen=1000)
        self._event_lock = threading.Lock()
        self.last_event_id = 0

    def update_status(self, status: str, progress: int = None, error_message: str = ""):
        """
        更新任务状态并广播事件。

        会自动刷新 `updated_at`、错误信息，并触发 `status` 类型的 SSE。

        参数:
            status: 任务阶段（pending/running/completed/error/cancelled）。
            progress: 可选的进度百分比。
            error_message: 出错时的人类可读说明。
        """
        self.status = status
        if progress is not None:
            self.progress = progress
        if error_message:
            self.error_message = error_message
        self.updated_at = datetime.now()
        # 推送状态变更事件，方便前端实时刷新
        self.publish_event(
            'status',
            {
                'status': self.status,
                'progress': self.progress,
                'error_message': self.error_message,
                'hint': error_message or '',
                'task': self.to_dict(),
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式，方便直接返回给JSON API。"""
        return {
            'task_id': self.task_id,
            'query': self.query,
            'status': self.status,
            'progress': self.progress,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'has_result': bool(self.html_content),
            'report_file_ready': bool(self.report_file_path),
            'report_file_name': self.report_file_name,
            'report_file_path': self.report_file_relative_path or self.report_file_path,
            'state_file_ready': bool(self.state_file_path),
            'state_file_path': self.state_file_relative_path or self.state_file_path,
            'ir_file_ready': bool(self.ir_file_path),
            'ir_file_path': self.ir_file_relative_path or self.ir_file_path,
            'markdown_file_ready': bool(self.markdown_file_path),
            'markdown_file_name': self.markdown_file_name,
            'markdown_file_path': self.markdown_file_relative_path or self.markdown_file_path,
            'history_inputs': self.history_inputs
        }

    def publish_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """
        将任意事件放入缓存并广播，所有新增逻辑均配套中文说明。

        参数:
            event_type: SSE中的event名称。
            payload: 实际业务数据。
        """
        timestamp = datetime.utcnow().isoformat() + 'Z'
        event: Dict[str, Any] = {
            'id': 0,
            'type': event_type,
            'task_id': self.task_id,
            'timestamp': timestamp,
            'payload': payload,
        }
        with self._event_lock:
            self.last_event_id += 1
            event['id'] = self.last_event_id
            self.event_history.append(event)
        _broadcast_event(self.task_id, event)

    def history_since(self, last_event_id: Optional[int]) -> List[Dict[str, Any]]:
        """
        根据Last-Event-ID补发历史事件，确保断线重连无遗漏。

        参数:
            last_event_id: SSE客户端记录的最后一个事件ID。

        返回:
            list[dict]: 从 last_event_id 之后的事件列表。
        """
        with self._event_lock:
            if last_event_id is None:
                return list(self.event_history)
            return [evt for evt in self.event_history if evt['id'] > last_event_id]


def check_engines_ready(task_id: str = None) -> Dict[str, Any]:
    """
    检查三个子引擎是否都有新文件。

    调用 ReportAgent 的基准检测逻辑，并附带论坛日志存在性，
    是 /status、/generate 的前置校验。
    如果提供了 task_id，则检查该特定任务的文件是否存在。
    """
    directories = {
        'insight': 'insight_engine_streamlit_reports',
        'media': 'media_engine_streamlit_reports',
        'query': 'query_engine_streamlit_reports'
    }

    forum_log_path = 'logs/forum.log'

    if not report_agent:
        return {
            'ready': False,
            'error': 'Report Engine未初始化'
        }

    return report_agent.check_input_files(
        directories['insight'],
        directories['media'],
        directories['query'],
        forum_log_path,
        task_id=task_id
    )


def run_report_generation(task: ReportTask, query: str, custom_template: str = ""):
    """
    在后台线程中运行报告生成。

    包括：检查输入→加载文档→调用ReportAgent→持久化输出→
    推送阶段性事件。出现错误会自动推送并写状态。

    参数:
        task: 本次任务对象，内部持有事件队列。
        query: 报告主题。
        custom_template: 可选的自定义模板字符串。
    """
    global current_task

    try:
        # 在局部闭包内封装推送逻辑，便于传递给ReportAgent
        def stream_handler(event_type: str, payload: Dict[str, Any]):
            if task.is_cancelled:
                raise CancelGenerationException("用户已取消生成")
            
            # 同步获取最新的增量HTML内容到任务对象中，使得 /result 接口能读取
            if event_type == 'stage' and payload.get('stage') == 'chapter_html_ready':
                if hasattr(report_agent, 'state') and hasattr(report_agent.state, 'html_content'):
                    task.html_content = report_agent.state.html_content

            """所有阶段事件都通过同一个接口分发，保证日志一致。"""
            task.publish_event(event_type, payload)
            # 如果事件包含进度信息，同步更新任务进度
            if event_type == 'progress' and 'progress' in payload:
                task.update_status("running", payload['progress'])

        task.update_status("running", 5)
        task.publish_event('stage', {'message': '任务已启动，正在检查输入文件', 'stage': 'prepare'})

        # 检查输入文件
        check_result = check_engines_ready(task_id=task.task_id)
        if not check_result['ready']:
            task.update_status("error", 0, f"输入文件未准备就绪: {check_result.get('missing_files', [])}")
            return

        task.publish_event('stage', {
            'message': '输入文件检查通过，准备载入内容',
            'stage': 'io_ready',
            'files': check_result.get('latest_files', {})
        })

        # 加载输入文件
        content = report_agent.load_input_files(check_result['latest_files'])
        task.publish_event('stage', {'message': '源数据加载完成，启动生成流程', 'stage': 'data_loaded'})

        # 将 seed_context 注入到 forum_logs 中（如果存在），作为最高优先级的补充信息
        if task.seed_context:
            logger.info(f"注入种子文件上下文到报告引擎, 长度: {len(task.seed_context)}")
            seed_supplement = (
                "\n\n=======================================================\n"
                "【系统级补充：用户上传的绝对真实种子材料（最高优先级事实基准）】\n"
                "提示：这部分内容是用户手动上传的确切资料，你可以将其作为参考资料引用。\n"
                f"信息源标题(Title): {task.seed_filename}\n"
                f"原始链接(URL): {task.seed_url}\n"
                "=======================================================\n"
                f"{task.seed_context}\n\n"
            )
            content['forum_logs'] = seed_supplement + content.get('forum_logs', '')

        # 生成报告（附带兜底重试，缓解瞬时网络抖动）
        for attempt in range(1, 3):
            try:
                task.publish_event('stage', {
                    'message': f'正在调用ReportAgent生成报告（第{attempt}次尝试）',
                    'stage': 'agent_running',
                    'attempt': attempt
                })
                generation_result = report_agent.generate_report(
                    query=query,
                    reports=content['reports'],
                    forum_logs=content['forum_logs'],
                    custom_template=custom_template,
                    save_report=True,
                    stream_handler=stream_handler
                )
                break
            except ChapterJsonParseError as err:
                hint_message = "尝试将Report Engine的API更换为算力更强、上下文更长的LLM"
                task.publish_event('warning', {
                    'message': hint_message,
                    'stage': 'agent_running',
                    'attempt': attempt,
                    'reason': 'chapter_json_parse',
                    'error': str(err),
                    'task': task.to_dict(),
                })
                raise ChapterJsonParseError(hint_message) from err
            except CancelGenerationException:
                # 如果是用户取消，则不进行重试，直接抛出到外层处理
                raise
            except Exception as err:
                # 将错误即时推送至前端，方便观察重试策略
                task.publish_event('warning', {
                    'message': f'ReportAgent执行失败: {str(err)}',
                    'stage': 'agent_running',
                    'attempt': attempt
                })
                if attempt == 2:
                    raise
                # 简单的指数退避，防止频繁触发限流（单位秒）
                backoff = min(5 * attempt, 15)
                task.publish_event('stage', {
                    'message': f'{backoff} 秒后重试生成任务',
                    'stage': 'retry_wait',
                    'wait_seconds': backoff
                })
                time.sleep(backoff)

        if isinstance(generation_result, dict):
            html_report = generation_result.get('html_content', '')
        else:
            html_report = generation_result

        task.publish_event('stage', {'message': '报告生成完毕，准备持久化', 'stage': 'persist'})

        # 保存结果
        task.html_content = html_report
        if isinstance(generation_result, dict):
            task.report_file_path = generation_result.get('report_filepath', '')
            task.report_file_relative_path = generation_result.get('report_relative_path', '')
            task.report_file_name = generation_result.get('report_filename', '')
            task.state_file_path = generation_result.get('state_filepath', '')
            task.state_file_relative_path = generation_result.get('state_relative_path', '')
            task.ir_file_path = generation_result.get('ir_filepath', '')
            task.ir_file_relative_path = generation_result.get('ir_relative_path', '')
            
        # 报告生成成功后，更新文件基准，防止前端无限循环触发自动生成
        try:
            if report_agent:
                report_agent._initialize_file_baseline()
        except Exception as e:
            logger.error(f"更新文件基准失败: {e}")

        task.publish_event('html_ready', {
            'message': 'HTML渲染完成，可刷新预览',
            'report_file': task.report_file_relative_path or task.report_file_path,
            'state_file': task.state_file_relative_path or task.state_file_path,
            'task': task.to_dict(),
        })
        task.update_status("completed", 100)
        task.publish_event('completed', {
            'message': '任务完成',
            'duration_seconds': (task.updated_at - task.created_at).total_seconds(),
            'report_file': task.report_file_relative_path or task.report_file_path,
            'task': task.to_dict(),
        })

    except CancelGenerationException:
        task.update_status("cancelled", 0, "用户主动取消了生成")
        task.publish_event('stage', {'message': '生成已取消', 'stage': 'cancelled'})
        logger.info(f"任务 {task.task_id} 已被用户取消")
        with task_lock:
            if current_task and current_task.task_id == task.task_id:
                current_task = None
    except Exception as e:
        logger.exception(f"报告生成过程中发生错误: {str(e)}")
        task.update_status("error", 0, str(e))
        task.publish_event('error', {
            'message': str(e),
            'stage': 'failed',
            'task': task.to_dict(),
        })
        # 只在出错时清理任务
        with task_lock:
            if current_task and current_task.task_id == task.task_id:
                current_task = None


import tempfile
from InsightEngine.tools.keyword_optimizer import KeywordOptimizer

@report_bp.route('/seed/<seed_id>', methods=['GET'])
def get_seed_content(seed_id: str):
    """
    提供在线预览用户上传的 Seed 内容的接口。
    前端可以通过弹窗或新标签页调用该接口查看纯文本数据。
    """
    try:
        # 安全校验，防止目录穿越
        if ".." in seed_id or "/" in seed_id or "\\" in seed_id:
            return jsonify({'success': False, 'error': '无效的 seed_id'}), 400

        seed_path = Path(settings.OUTPUT_DIR) / 'seeds' / f"{seed_id}.json"
        if not seed_path.exists():
            # 兼容旧版本 .txt
            old_path = Path(settings.OUTPUT_DIR) / 'seeds' / f"{seed_id}.txt"
            if not old_path.exists():
                return jsonify({'success': False, 'error': 'Seed 文件不存在或已过期'}), 404
            content = old_path.read_text(encoding='utf-8')
            return Response(content, mimetype='text/plain')

        try:
            data = json.loads(seed_path.read_text(encoding='utf-8'))
            content = data.get('text', '')
            return Response(content, mimetype='text/plain')
        except Exception as e:
            logger.error(f"解析 Seed JSON 失败: {e}")
            return jsonify({'success': False, 'error': '解析数据格式失败'}), 500
    except Exception as e:
        logger.error(f"读取 Seed 内容失败: {e}")
        return jsonify({'success': False, 'error': '读取内容时发生错误'}), 500

@report_bp.route('/analyze_seed', methods=['POST'])
def analyze_seed():
    """
    处理上传的种子附件，提取文本并结合原查询生成优化关键词。
    支持 .txt, .md, .pdf, .doc, .docx
    """
    try:
        query = request.form.get('query', '').strip()
        if not query:
            return jsonify({'success': False, 'error': '未提供原始查询'}), 400

        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': '未提供附件'}), 400

        combined_text = []
        for file in files:
            filename = file.filename.lower()
            ext = os.path.splitext(filename)[1]
            
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp:
                file.save(temp.name)
                temp_path = temp.name

            try:
                extracted = ""
                if ext in ['.txt', '.md']:
                    with open(temp_path, 'r', encoding='utf-8', errors='ignore') as f:
                        extracted = f.read()
                elif ext == '.pdf':
                    doc = fitz.open(temp_path)
                    for page in doc:
                        extracted += page.get_text() + "\n"
                    doc.close()
                elif ext in ['.doc', '.docx']:
                    doc = docx.Document(temp_path)
                    extracted = "\n".join([para.text for para in doc.paragraphs])
                else:
                    logger.warning(f"不支持的文件格式: {ext}")
                    
                if extracted.strip():
                    # 特意为附件拼装一个假的 URL 格式供 LLM 引用
                    fake_url = f"file:///{file.filename}"
                    combined_text.append(f"--- 附件标题: {file.filename} ---\nURL: {fake_url}\n{extracted.strip()}")
            except Exception as e:
                logger.error(f"解析附件 {file.filename} 失败: {e}")
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        seed_context = "\n\n".join(combined_text)
        if not seed_context:
            return jsonify({'success': False, 'error': '无法从附件中提取任何文本'}), 400

        # 获取文件名用于展示
        file_names = [f.filename for f in files]
        primary_filename = file_names[0] if file_names else "上传的附件"

        # 截断过长文本，避免大模型Token爆炸（这里取前15000字符作为上下文参考）
        context_for_optimizer = seed_context[:15000]

        # 调用关键词优化器
        optimizer = KeywordOptimizer()
        prompt_context = (
            "【以下是用户提供的真实种子附件内容，请重点提取其中的实体名词、事件和核心痛点，"
            "确保生成的关键词不脱离这些事实锚点】\n\n" + context_for_optimizer
        )
        
        response = optimizer.optimize_keywords(query, prompt_context)
        
        if not response.success:
            return jsonify({'success': False, 'error': response.error_message}), 500

        # 将完整的 seed_context 和元数据保存到临时目录，供后续生成报告时调用
        seed_id = f"seed_{int(time.time()*1000)}"
        seed_dir = Path(settings.OUTPUT_DIR) / 'seeds'
        seed_dir.mkdir(parents=True, exist_ok=True)
        
        # 结构化保存，方便后续注入为合法信息源
        seed_data = {
            "text": seed_context,
            "filename": primary_filename,
            "fake_url": f"seed://{seed_id}/{primary_filename}",
            "timestamp": datetime.now().isoformat()
        }
        
        seed_path = seed_dir / f"{seed_id}.json"
        seed_path.write_text(json.dumps(seed_data, ensure_ascii=False), encoding='utf-8')

        # 触发 seed 入库，将其写入本地 daily_news 表并记录 crawler 日志
        try:
            from InsightEngine.utils.data_ingestion import ingest_seed_data
            import threading
            threading.Thread(target=ingest_seed_data, args=(seed_id,), daemon=True).start()
        except Exception as e:
            logger.error(f"异步触发 seed 入库失败: {e}")

        return jsonify({
            'success': True,
            'optimized_keywords': response.optimized_keywords,
            'reasoning': response.reasoning,
            'seed_id': seed_id
        })

    except Exception as e:
        logger.exception(f"分析种子文件失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@report_bp.route('/cancel/<task_id>', methods=['POST'])
def cancel_report(task_id: str):
    """
    取消正在生成的报告任务。
    """
    global current_task
    try:
        task = _get_task(task_id)
        if not task:
            return jsonify({'success': False, 'error': '任务不存在'}), 404

        if task.status == 'completed':
            return jsonify({'success': False, 'error': '报告已经生成完成，无法取消'}), 400

        # 设置取消标志位，后台线程会在下一个 stream 事件点抛出异常中断
        task.is_cancelled = True
        task.update_status("cancelled", 0, "用户主动取消了生成")
        task.publish_event('stage', {'message': '生成已取消', 'stage': 'cancelled'})
        
        with task_lock:
            if current_task and current_task.task_id == task_id:
                current_task = None
                
        return jsonify({'success': True, 'message': '已发送取消指令'})
    except Exception as e:
        logger.error(f"取消任务失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@report_bp.route('/history', methods=['GET'])
def get_history_tasks():
    """
    获取历史任务列表。
    扫描输出目录下的所有报告文件，解析元数据，按时间倒序返回。
    """
    try:
        if not report_agent:
            return jsonify({'success': False, 'error': 'Report Engine未初始化'}), 500
            
        output_dir = Path(report_agent.config.OUTPUT_DIR)
        tasks = []
        
        if output_dir.exists():
            # 扫描所有生成的报告文件
            html_files = list(output_dir.glob("final_report_*.html"))
            
            for html_path in html_files:
                try:
                    # 从文件名解析信息: final_report_{query_safe}_{timestamp}.html
                    stem = html_path.stem
                    parts = stem.replace('final_report_', '').split('_')
                    
                    if len(parts) >= 2:
                        timestamp = f"{parts[-2]}_{parts[-1]}"
                        query_safe = '_'.join(parts[:-2])
                        task_id = f"recovered_file_{html_path.name}"
                        
                        task_data = {
                            'task_id': task_id,
                            'query': query_safe.replace('_', ' ') or '历史任务',
                            'status': 'completed',
                            'progress': 100,
                            'created_at': datetime.fromtimestamp(html_path.stat().st_ctime).isoformat(),
                            'updated_at': datetime.fromtimestamp(html_path.stat().st_mtime).isoformat(),
                            'report_file_name': html_path.name,
                            'report_file_path': str(html_path.resolve()),
                            'has_result': True,
                            'report_file_ready': True,
                            'state_file_ready': False,
                            'ir_file_ready': False
                        }
                        
                        # 检查相关文件
                        ir_filename = f"report_ir_{query_safe}_{timestamp}.json"
                        ir_path = Path(report_agent.config.DOCUMENT_IR_OUTPUT_DIR) / ir_filename
                        if ir_path.exists():
                            task_data['ir_file_ready'] = True
                            task_data['ir_file_path'] = str(ir_path.resolve())
                            
                        state_filename = f"report_state_{query_safe}_{timestamp}.json"
                        state_path = output_dir / state_filename
                        if state_path.exists():
                            task_data['state_file_ready'] = True
                            task_data['state_file_path'] = str(state_path.resolve())
                            
                        # 检查子引擎的报告文件
                        task_data['related_reports'] = []
                        engine_dirs = {
                            'insight': 'insight_engine_streamlit_reports',
                            'media': 'media_engine_streamlit_reports',
                            'query': 'query_engine_streamlit_reports'
                        }
                        
                        for engine_name, engine_dir in engine_dirs.items():
                            engine_report_name = f"deep_search_report_{query_safe}_{timestamp}.md"
                            # 子引擎的报告生成在项目根目录下的各个 engine_dir 中，而不是在 final_reports 下
                            engine_report_path = Path(engine_dir) / engine_report_name
                            if engine_report_path.exists():
                                task_data['related_reports'].append({
                                    'engine': engine_name,
                                    'filename': engine_report_name,
                                    'path': str(engine_report_path.resolve())
                                })
                                
                        tasks.append(task_data)
                except Exception as e:
                    logger.warning(f"解析历史任务文件失败 {html_path.name}: {e}")
                    
        # 按更新时间倒序排列
        tasks.sort(key=lambda x: x['updated_at'], reverse=True)
        
        # 将内存中还没持久化但已完成的任务也合并进去（去重）
        with task_lock:
            memory_tasks = [t.to_dict() for t in tasks_registry.values() if t.status == 'completed']
            
        # 简单去重，优先使用内存中的数据（可能包含更完整的元数据）
        # 使用 report_file_name 进行去重，因为不同环境下的绝对路径和相对路径格式可能不同
        seen_names = {t.get('report_file_name'): t for t in memory_tasks if t.get('report_file_name')}
        final_tasks = memory_tasks.copy()
        
        for t in tasks:
            mem_task = seen_names.get(t.get('report_file_name'))
            if mem_task is None:
                final_tasks.append(t)
                seen_names[t.get('report_file_name')] = t
            else:
                # 内存中的任务可能缺少 related_reports 信息，从磁盘解析的任务中补充
                if 'related_reports' not in mem_task or not mem_task['related_reports']:
                    mem_task['related_reports'] = t.get('related_reports', [])
                
        # 再次按时间排序
        final_tasks.sort(key=lambda x: x['updated_at'], reverse=True)

        return jsonify({
            'success': True,
            'tasks': final_tasks
        })
    except Exception as e:
        logger.exception(f"获取历史任务列表失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@report_bp.route('/history/<task_id>', methods=['DELETE'])
def delete_history_task(task_id: str):
    """
    删除指定的历史任务及其相关的所有报告和状态文件。
    
    参数:
        task_id: 要删除的任务ID，如果是恢复的任务，前缀通常为 'recovered_file_'
    """
    try:
        if not report_agent:
            return jsonify({'success': False, 'error': 'Report Engine未初始化'}), 500
            
        output_dir = Path(report_agent.config.OUTPUT_DIR)
        ir_output_dir = Path(report_agent.config.DOCUMENT_IR_OUTPUT_DIR)
        deleted_files = []
        
        # 1. 尝试从内存中获取该任务的信息
        task = _get_task(task_id)
        
        # 获取文件名或安全前缀
        html_file_name = ""
        if task and task.report_file_name:
            html_file_name = task.report_file_name
        elif task_id.startswith('recovered_file_'):
            html_file_name = task_id.replace('recovered_file_', '')
        
        if html_file_name:
            # 找到 HTML 报告文件
            html_path = output_dir / html_file_name
            if html_path.exists():
                html_path.unlink()
                deleted_files.append(html_file_name)
            
            # 解析时间戳和查询词，尝试删除关联的 IR 和 State 文件
            stem = Path(html_file_name).stem
            parts = stem.replace('final_report_', '').split('_')
            if len(parts) >= 2:
                timestamp = f"{parts[-2]}_{parts[-1]}"
                query_safe = '_'.join(parts[:-2])
                
                ir_filename = f"report_ir_{query_safe}_{timestamp}.json"
                ir_path = ir_output_dir / ir_filename
                if ir_path.exists():
                    ir_path.unlink()
                    deleted_files.append(ir_filename)
                    
                state_filename = f"report_state_{query_safe}_{timestamp}.json"
                state_path = output_dir / state_filename
                if state_path.exists():
                    state_path.unlink()
                    deleted_files.append(state_filename)
                
                # 删除子引擎生成的关联报告
                engine_dirs = {
                    'insight': 'insight_engine_streamlit_reports',
                    'media': 'media_engine_streamlit_reports',
                    'query': 'query_engine_streamlit_reports'
                }
                for engine_name, engine_dir in engine_dirs.items():
                    engine_report_name = f"deep_search_report_{query_safe}_{timestamp}.md"
                    engine_report_path = Path(engine_dir) / engine_report_name
                    if engine_report_path.exists():
                        engine_report_path.unlink()
                        deleted_files.append(f"{engine_dir}/{engine_report_name}")
                    
                    # 同时删除可能的状态文件
                    engine_state_name = f"state_{query_safe}_{timestamp}.json"
                    engine_state_path = Path(engine_dir) / engine_state_name
                    if engine_state_path.exists():
                        engine_state_path.unlink()
                        deleted_files.append(f"{engine_dir}/{engine_state_name}")
        
        # 从内存中移除
        with task_lock:
            if task_id in tasks_registry:
                tasks_registry.pop(task_id)
        
        return jsonify({
            'success': True,
            'message': '任务及相关文件已删除',
            'deleted_files': deleted_files
        })
        
    except Exception as e:
        logger.exception(f"删除历史任务失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@report_bp.route('/status', methods=['GET'])
def get_status():
    """
    获取Report Engine状态，包括引擎就绪情况与当前任务信息。

    返回:
        Response: JSON结构包含initialized/engines_ready/当前任务等。
    """
    try:
        task_id = request.args.get('task_id')
        engines_status = check_engines_ready(task_id=task_id)

        # 移除自动兜底最新历史文件的逻辑，避免新任务开始前显示旧报告
        # （如果需要查看历史报告，请使用历史记录列表功能）
        task_data = current_task.to_dict() if current_task else None

        return jsonify({
            'success': True,
            'initialized': report_agent is not None,
            'engines_ready': engines_status['ready'],
            'files_found': engines_status.get('files_found', []),
            'missing_files': engines_status.get('missing_files', []),
            'current_task': task_data
        })
    except Exception as e:
        logger.exception(f"获取Report Engine状态失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@report_bp.route('/generate', methods=['POST'])
def generate_report():
    """
    开始生成报告。

    负责排队、创建后台线程、清空日志并返回SSE地址。

    请求体:
        query: 报告主题（可选）。
        custom_template: 自定义模板字符串（可选）。

    返回:
        Response: JSON，包含 task_id 与 SSE stream url。
    """
    global current_task

    try:
        # 检查是否有任务在运行
        with task_lock:
            if current_task and current_task.status == "running":
                return jsonify({
                    'success': False,
                    'error': '已有报告生成任务在运行中',
                    'current_task': current_task.to_dict()
                }), 400

            # 如果有已完成的任务，清理它
            if current_task and current_task.status in ["completed", "error"]:
                current_task = None

        # 获取请求参数
        data = request.get_json() or {}
        if not isinstance(data, dict):
            logger.warning("generate_report 接收到非对象JSON负载，已忽略原始内容")
            data = {}
        query = data.get('query', '智能舆情分析报告')
        custom_template = data.get('custom_template', '')
        seed_id = data.get('seed_id', '')
        
        # 精简任务ID生成逻辑：6个汉字 + 6个日期数字 + 4个随机数字
        import re
        import random
        import string
        
        # 提取汉字，不足6个用默认汉字补齐
        chinese_chars = re.sub(r'[^\u4e00-\u9fa5]', '', query)
        if len(chinese_chars) < 6:
            chinese_chars += '舆情分析研究报告'
        short_name = chinese_chars[:6]
        
        # 6个日期数字: YYMMDD
        date_str = datetime.now().strftime('%y%m%d')
        
        # 4个随机数字
        short_code = ''.join(random.choices(string.digits, k=4))
        
        default_task_id = f"{short_name}{date_str}{short_code}"
        
        task_id = data.get('task_id') or default_task_id

        # 清空日志文件
        clear_report_log()

        # 检查Report Engine是否初始化
        if not report_agent:
            return jsonify({
                'success': False,
                'error': 'Report Engine未初始化'
            }), 500

        # 检查输入文件是否准备就绪
        engines_status = check_engines_ready(task_id=task_id)
        if not engines_status['ready']:
            return jsonify({
                'success': False,
                'error': '输入文件未准备就绪',
                'missing_files': engines_status.get('missing_files', [])
            }), 400

        # 创建新任务
        task = ReportTask(query, task_id, custom_template)

        # 尝试读取种子文件上下文
        if seed_id:
            seed_path = Path(settings.OUTPUT_DIR) / 'seeds' / f"{seed_id}.json"
            try:
                if seed_path.exists():
                    seed_data = json.loads(seed_path.read_text(encoding='utf-8'))
                    task.seed_context = seed_data.get('text', '')
                    # 结构化保存文件名和伪造URL，用于后续包装为 Search 结果
                    task.seed_filename = seed_data.get('filename', '上传的附件')
                    task.seed_url = seed_data.get('fake_url', f"seed://{seed_id}/attachment")
                    logger.info(f"任务 {task_id} 已成功加载种子上下文 {seed_id} (JSON)，大小: {len(task.seed_context)} 字符")
                else:
                    # 兼容旧版本
                    old_path = Path(settings.OUTPUT_DIR) / 'seeds' / f"{seed_id}.txt"
                    if old_path.exists():
                        task.seed_context = old_path.read_text(encoding='utf-8')
                        task.seed_filename = "上传的附件"
                        task.seed_url = f"seed://{seed_id}/attachment"
                        logger.info(f"任务 {task_id} 已成功加载种子上下文 {seed_id} (TXT)，大小: {len(task.seed_context)} 字符")
            except Exception as e:
                logger.error(f"读取种子上下文 {seed_id} 失败: {e}")

        with task_lock:
            current_task = task
            tasks_registry[task_id] = task
            _prune_task_history_locked()

        # 把 task_id 透传给 report_agent 的 state
        if report_agent:
            report_agent.state.task_id = task_id

        # 通过主动推送pending事件告知前端任务已经排队
        task.publish_event(
            'status',
            {
                'status': task.status,
                'progress': task.progress,
                'message': '任务已排队，等待资源空闲',
                'task': task.to_dict(),
            }
        )

        # 在后台线程中运行报告生成
        thread = threading.Thread(
            target=run_report_generation,
            args=(task, query, custom_template),
            daemon=True
        )
        thread.start()

        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': '报告生成已启动',
            'task': task.to_dict(),
            'stream_url': f"/api/report/stream/{task_id}"
        })

    except Exception as e:
        logger.exception(f"开始生成报告失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@report_bp.route('/progress/<task_id>', methods=['GET'])
def get_progress(task_id: str):
    """
    获取报告生成进度，若任务被清理则返回一个完成态兜底。

    参数:
        task_id: 任务唯一标识。

    返回:
        Response: JSON包含任务当前状态。
    """
    try:
        task = _get_task(task_id)
        if not task:
            # 如果任务不存在，可能是历史记录已被清理，回传一个完成态兜底
            return jsonify({
                'success': True,
                'task': {
                    'task_id': task_id,
                    'status': 'completed',
                    'progress': 100,
                    'error_message': '',
                    'has_result': True,
                    'report_file_ready': False,
                    'report_file_name': '',
                    'report_file_path': '',
                    'state_file_ready': False,
                    'state_file_path': ''
                }
            })

        return jsonify({
            'success': True,
            'task': task.to_dict()
        })

    except Exception as e:
        logger.exception(f"获取报告生成进度失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@report_bp.route('/stream/<task_id>', methods=['GET'])
def stream_task(task_id: str):
    """
    基于SSE的实时推送接口。

    - 自动补发Last-Event-ID之后的历史事件；
    - 周期性发送心跳以防代理中断；
    - 任务结束后自动注销监听。

    参数:
        task_id: 任务唯一标识。

    返回:
        Response: `text/event-stream` 类型响应。
    """
    task = _get_task(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404

    last_event_header = request.headers.get('Last-Event-ID')
    try:
        last_event_id = int(last_event_header) if last_event_header else None
    except ValueError:
        last_event_id = None

    def client_disconnected() -> bool:
        """
        尽早探测客户端是否已经断开，避免继续写入触发BrokenPipe。

        eventlet 在 Windows 上会在关闭连接时抛出 ConnectionAbortedError，
        提前退出生成器可以缩减无意义的日志。
        """
        try:
            env_input = request.environ.get('wsgi.input')
            return bool(getattr(env_input, 'closed', False))
        except Exception:
            return False

    def event_generator():
        """
        SSE事件生成器。

        - 负责注册并消费对应任务的事件队列；
        - 先回放历史事件再持续监听实时事件；
        - 周期性发送心跳并在任务结束后自动注销监听。
        """
        queue = _register_stream(task_id)
        last_data_ts = time.time()
        try:
            # 断线重连场景下，先补发历史事件，保证界面状态一致
            history = task.history_since(last_event_id)
            for event in history:
                yield _format_sse(event)
                if event.get('type') != 'heartbeat':
                    last_data_ts = time.time()

            finished = task.status in STREAM_TERMINAL_STATUSES
            while True:
                if finished:
                    break
                if client_disconnected():
                    logger.info(f"SSE客户端已断开，停止推送: {task_id}")
                    break
                event = None
                try:
                    event = queue.get(timeout=STREAM_HEARTBEAT_INTERVAL)
                except Empty:
                    if task.status in STREAM_TERMINAL_STATUSES:
                        logger.info(f"任务 {task_id} 已结束且无新事件，SSE自动收口")
                        break
                    heartbeat = {
                        'id': f"hb-{int(time.time() * 1000)}",
                        'type': 'heartbeat',
                        'task_id': task_id,
                        'timestamp': datetime.utcnow().isoformat() + 'Z',
                        'payload': {'status': task.status}
                    }
                    event = heartbeat
                if event is None:
                    logger.warning(f"SSE推送获取事件失败（task {task_id}），提前结束")
                    break

                try:
                    yield _format_sse(event)
                    if event.get('type') != 'heartbeat':
                        last_data_ts = time.time()
                except GeneratorExit:
                    logger.info(f"SSE生成器关闭，停止任务 {task_id} 推送")
                    break
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
                    logger.warning(f"SSE连接被客户端中断（task {task_id}）: {exc}")
                    break
                except Exception as exc:
                    event_type = event.get('type') if isinstance(event, dict) else 'unknown'
                    logger.exception(f"SSE推送失败（task {task_id}, event {event_type}）: {exc}")
                    break

                if event.get('type') in ("completed", "error", "cancelled"):
                    finished = True
                else:
                    finished = finished or task.status in STREAM_TERMINAL_STATUSES

                # 终态下最多保活一段时间，防止前端早已结束但后台循环未退出
                if task.status in STREAM_TERMINAL_STATUSES:
                    idle_for = time.time() - last_data_ts
                    if idle_for > STREAM_IDLE_TIMEOUT:
                        logger.info(f"任务 {task_id} 已终态且空闲 {int(idle_for)}s，主动关闭SSE")
                        break
        finally:
            _unregister_stream(task_id, queue)

    response = Response(
        stream_with_context(event_generator()),
        mimetype='text/event-stream'
    )
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


@report_bp.route('/result/<task_id>', methods=['GET'])
def get_result(task_id: str):
    """
    获取报告生成结果（或增量预览）。

    参数:
        task_id: 任务ID。

    返回:
        Response: JSON，包含HTML预览与文件路径。
    """
    try:
        task = _get_task(task_id)
        if not task:
            return jsonify({
                'success': False,
                'error': '任务不存在'
            }), 404

        # 优先返回内存中的已渲染 HTML，即使任务未完成（实现“生成一章看一章”）
        if getattr(task, 'html_content', None):
            return Response(task.html_content, mimetype='text/html')

        if task.status != "completed":
            # 如果任务被取消或出错，尝试从硬盘中搜索匹配的历史文件兜底
            if report_agent:
                output_dir = Path(report_agent.config.OUTPUT_DIR)
                base_id = task_id.replace('recovered_file_', '').replace('final_report_', '').replace('.html', '')
                html_files = list(output_dir.glob(f"*{base_id}*.html"))
                if html_files:
                    latest_html = max(html_files, key=lambda p: p.stat().st_mtime)
                    try:
                        with open(latest_html, 'r', encoding='utf-8') as f:
                            return Response(f.read(), mimetype='text/html')
                    except Exception as e:
                        pass
                        
            return jsonify({
                'success': False,
                'error': '报告尚未生成任何内容',
                'task': task.to_dict()
            }), 400

        return Response(
            task.html_content,
            mimetype='text/html'
        )

    except Exception as e:
        logger.exception(f"获取报告生成结果失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@report_bp.route('/result/<task_id>/json', methods=['GET'])
def get_result_json(task_id: str):
    """获取报告生成结果（JSON格式）"""
    try:
        task = _get_task(task_id)
        if not task:
            return jsonify({
                'success': False,
                'error': '任务不存在'
            }), 404

        if task.status != "completed":
            # 兜底查询硬盘
            if report_agent:
                output_dir = Path(report_agent.config.OUTPUT_DIR)
                base_id = task_id.replace('recovered_file_', '').replace('final_report_', '').replace('.html', '')
                html_files = list(output_dir.glob(f"*{base_id}*.html"))
                if html_files:
                    latest_html = max(html_files, key=lambda p: p.stat().st_mtime)
                    try:
                        with open(latest_html, 'r', encoding='utf-8') as f:
                            return jsonify({
                                'success': True,
                                'task': task.to_dict(),
                                'html_content': f.read()
                            })
                    except Exception as e:
                        pass
                        
            return jsonify({
                'success': False,
                'error': '报告尚未完成',
                'task': task.to_dict()
            }), 400

        return jsonify({
            'success': True,
            'task': task.to_dict(),
            'html_content': task.html_content
        })

    except Exception as e:
        logger.exception(f"获取报告生成结果失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@report_bp.route('/download/<task_id>', methods=['GET'])
def download_report(task_id: str):
    """
    下载已生成的报告HTML文件或中间产物。
    如果是历史任务，从本地文件系统中匹配。
    """
    try:
        task = _get_task(task_id)
        
        # 如果内存中没有这个任务，尝试从硬盘中搜索匹配的历史文件
        if not task:
            # 去 final_reports 和 document_ir 目录里找
            # task_id 在此处可能是纯 ID 也可能是完整文件名
            base_id = task_id.replace('report_state_', '').replace('final_report_', '').replace('.json', '').replace('.html', '')
            
            # 支持下载 JSON (IR数据/状态数据)
            if task_id.endswith('.json'):
                json_path = os.path.join(REPORT_DIR, task_id)
                if not os.path.exists(json_path):
                    json_path = os.path.join(REPORT_DIR, 'document_ir', task_id)
                
                if os.path.exists(json_path):
                    return send_file(
                        json_path,
                        mimetype='application/json',
                        as_attachment=True,
                        download_name=task_id
                    )
                return jsonify({'success': False, 'error': '未找到对应的JSON历史文件'}), 404
            
            # 否则默认搜索 HTML 最终报告
            html_files = list(Path(REPORT_DIR).glob(f"*{base_id}*.html"))
            if html_files:
                return send_file(
                    str(html_files[0]),
                    mimetype='text/html',
                    as_attachment=True,
                    download_name=html_files[0].name
                )
                
            return jsonify({
                'success': False,
                'error': '任务不存在且未找到对应的历史报告文件'
            }), 404

        if task.status != "completed" or not task.report_file_path:
            return jsonify({
                'success': False,
                'error': '报告尚未完成或尚未保存'
            }), 400

        if not os.path.exists(task.report_file_path):
            return jsonify({
                'success': False,
                'error': '报告文件不存在或已被删除'
            }), 404

        download_name = task.report_file_name or os.path.basename(task.report_file_path)
        return send_file(
            task.report_file_path,
            mimetype='text/html',
            as_attachment=True,
            download_name=download_name
        )

    except Exception as e:
        logger.error(f"下载报告失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500




@report_bp.route('/templates', methods=['GET'])
def get_templates():
    """
    获取可用模板列表，便于前端展示可选Markdown骨架。

    返回:
        Response: JSON，列出模板名称/描述/大小。
    """
    try:
        if not report_agent:
            return jsonify({
                'success': False,
                'error': 'Report Engine未初始化'
            }), 500

        template_dir = settings.TEMPLATE_DIR
        templates = []

        if os.path.exists(template_dir):
            for filename in os.listdir(template_dir):
                if filename.endswith('.md'):
                    template_path = os.path.join(template_dir, filename)
                    try:
                        with open(template_path, 'r', encoding='utf-8') as f:
                            content = f.read()

                        templates.append({
                            'name': filename.replace('.md', ''),
                            'filename': filename,
                            'description': content.split('\n')[0] if content else '无描述',
                            'size': len(content)
                        })
                    except Exception as e:
                        logger.exception(f"读取模板失败 {filename}: {str(e)}")

        return jsonify({
            'success': True,
            'templates': templates,
            'template_dir': template_dir
        })

    except Exception as e:
        logger.exception(f"获取可用模板列表失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


# 错误处理
@report_bp.errorhandler(404)
def not_found(error):
    """404兜底处理：保证接口统一返回JSON结构"""
    logger.exception(f"API端点不存在: {str(error)}")
    return jsonify({
        'success': False,
        'error': 'API端点不存在'
    }), 404


@report_bp.errorhandler(500)
def internal_error(error):
    """500兜底处理：捕获未被主动捕获的异常"""
    logger.exception(f"服务器内部错误: {str(error)}")
    return jsonify({
        'success': False,
        'error': '服务器内部错误'
    }), 500


def clear_report_log():
    """
    清空report.log文件，方便新任务只查看本次运行日志。

    返回:
        None
    """
    try:
        log_file = settings.LOG_FILE

        # 【修复】使用truncate而非重新打开，避免与logger的文件句柄冲突
        # 追加模式打开，然后truncate，保持文件句柄有效
        with open(log_file, 'r+', encoding='utf-8') as f:
            f.truncate(0)  # 清空文件内容但不关闭文件
            f.flush()      # 立即刷新

        logger.info(f"已清空日志文件: {log_file}")
    except FileNotFoundError:
        # 文件不存在，创建空文件
        try:
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write('')
            logger.info(f"创建日志文件: {log_file}")
        except Exception as e:
            logger.exception(f"创建日志文件失败: {str(e)}")
    except Exception as e:
        logger.exception(f"清空日志文件失败: {str(e)}")


@report_bp.route('/log', methods=['GET'])
def get_report_log():
    """
    获取report.log内容，并按行去除空白返回。

    【修复】优化大文件读取，添加错误处理和文件锁

    返回:
        Response: JSON，包含最新日志行数组。
    """
    try:
        log_file = settings.LOG_FILE

        if not os.path.exists(log_file):
            return jsonify({
                'success': True,
                'log_lines': []
            })

        # 【修复】检查文件大小，避免读取过大文件导致内存问题
        file_size = os.path.getsize(log_file)
        max_size = 10 * 1024 * 1024  # 10MB限制

        if file_size > max_size:
            # 文件过大，只读取最后10MB
            with open(log_file, 'rb') as f:
                f.seek(-max_size, 2)  # 从文件末尾往前10MB
                # 跳过可能不完整的第一行
                f.readline()
                content = f.read().decode('utf-8', errors='replace')
            lines = content.splitlines()
            logger.warning(f"日志文件过大 ({file_size} bytes)，仅返回最后 {max_size} bytes")
        else:
            # 正常大小，完整读取
            with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()

        # 清理行尾的换行符和空行
        log_lines = [line.rstrip('\n\r') for line in lines if line.strip()]

        return jsonify({
            'success': True,
            'log_lines': log_lines
        })

    except PermissionError as e:
        logger.error(f"读取日志权限不足: {str(e)}")
        return jsonify({
            'success': False,
            'error': '读取日志权限不足'
        }), 403
    except UnicodeDecodeError as e:
        logger.error(f"日志文件编码错误: {str(e)}")
        return jsonify({
            'success': False,
            'error': '日志文件编码错误'
        }), 500
    except Exception as e:
        logger.exception(f"读取日志失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'读取日志失败: {str(e)}'
        }), 500


@report_bp.route('/log/clear', methods=['POST'])
def clear_log():
    """
    手动清空日志，提供REST入口供前端一键重置。

    返回:
        Response: JSON，标记是否清理成功。
    """
    try:
        clear_report_log()
        return jsonify({
            'success': True,
            'message': '日志已清空'
        })
    except Exception as e:
        logger.exception(f"清空日志失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'清空日志失败: {str(e)}'
        }), 500


@report_bp.route('/export/md/<task_id>', methods=['GET'])
def export_markdown(task_id: str):
    """
    导出报告为 Markdown 格式。

    基于已保存的 Document IR 调用 MarkdownRenderer，生成文件并返回下载。
    """
    try:
        task = _get_task(task_id)
        if not task:
            return jsonify({
                'success': False,
                'error': '任务不存在'
            }), 404

        if task.status != 'completed':
            return jsonify({
                'success': False,
                'error': f'任务未完成，当前状态: {task.status}'
            }), 400

        if not task.ir_file_path or not os.path.exists(task.ir_file_path):
            return jsonify({
                'success': False,
                'error': 'IR文件不存在，无法生成Markdown'
            }), 404

        with open(task.ir_file_path, 'r', encoding='utf-8') as f:
            document_ir = json.load(f)

        from .renderers import MarkdownRenderer
        renderer = MarkdownRenderer()
        # 传入 ir_file_path，修复后的图表会自动保存到 IR 文件
        markdown_text = renderer.render(document_ir, ir_file_path=task.ir_file_path)

        metadata = document_ir.get('metadata') if isinstance(document_ir, dict) else {}
        topic = (metadata or {}).get('topic') or (metadata or {}).get('title') or (metadata or {}).get('query') or task.query
        safe_topic = _safe_filename_segment(topic or 'report')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"report_{safe_topic}_{timestamp}.md"

        output_dir = Path(settings.OUTPUT_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / filename
        md_path.write_text(markdown_text, encoding='utf-8')

        task.markdown_file_path = str(md_path.resolve())
        task.markdown_file_relative_path = os.path.relpath(task.markdown_file_path, os.getcwd())
        task.markdown_file_name = filename

        logger.info(f"导出Markdown完成: {md_path}")

        return send_file(
            task.markdown_file_path,
            mimetype='text/markdown',
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        logger.exception(f"导出Markdown失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'导出Markdown失败: {str(e)}'
        }), 500


@report_bp.route('/export/pdf/<task_id>', methods=['GET'])
def export_pdf(task_id: str):
    """
    导出报告为PDF格式。

    从IR JSON文件生成优化的PDF，支持自动布局调整。

    参数:
        task_id: 任务ID

    查询参数:
        optimize: 是否启用布局优化（默认true）

    返回:
        Response: PDF文件流或错误信息
    """
    try:
        # 检测 Pango 依赖
        from .utils.dependency_check import check_pango_available
        pango_available, pango_message = check_pango_available()
        if not pango_available:
            return jsonify({
                'success': False,
                'error': 'PDF 导出功能不可用：缺少系统依赖',
                'details': '请查看根目录 README.md “源码启动”的第二步（PDF 导出依赖）了解安装方法',
                'help_url': 'https://github.com/666ghj/BettaFish#2-安装-pdf-导出所需系统依赖可选',
                'system_message': pango_message
            }), 503

        # 获取任务信息
        task = _get_task(task_id)
        if not task:
            return jsonify({
                'success': False,
                'error': '任务不存在'
            }), 404

        # 检查任务是否完成
        if task.status != 'completed':
            return jsonify({
                'success': False,
                'error': f'任务未完成，当前状态: {task.status}'
            }), 400

        # 获取IR文件路径
        if not task.ir_file_path or not os.path.exists(task.ir_file_path):
            return jsonify({
                'success': False,
                'error': 'IR文件不存在'
            }), 404

        # 读取IR数据
        with open(task.ir_file_path, 'r', encoding='utf-8') as f:
            document_ir = json.load(f)

        # 检查是否启用布局优化
        optimize = request.args.get('optimize', 'true').lower() == 'true'

        # 创建PDF渲染器并生成PDF
        from .renderers import PDFRenderer
        renderer = PDFRenderer()

        logger.info(f"开始导出PDF，任务ID: {task_id}，布局优化: {optimize}")

        # 生成PDF字节流
        pdf_bytes = renderer.render_to_bytes(document_ir, optimize_layout=optimize)

        # 确定下载文件名
        topic = document_ir.get('metadata', {}).get('topic', 'report')
        pdf_filename = f"report_{topic}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

        # 返回PDF文件
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="{pdf_filename}"',
                'Content-Type': 'application/pdf'
            }
        )

    except Exception as e:
        logger.exception(f"导出PDF失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'导出PDF失败: {str(e)}'
        }), 500


@report_bp.route('/export/pdf-from-ir', methods=['POST'])
def export_pdf_from_ir():
    """
    从IR JSON直接导出PDF（不需要任务ID）。

    适用于前端直接传递IR数据的场景。

    请求体:
        {
            "document_ir": {...},  // Document IR JSON
            "optimize": true       // 是否启用布局优化（可选）
        }

    返回:
        Response: PDF文件流或错误信息
    """
    try:
        # 检测 Pango 依赖
        from .utils.dependency_check import check_pango_available
        pango_available, pango_message = check_pango_available()
        if not pango_available:
            return jsonify({
                'success': False,
                'error': 'PDF 导出功能不可用：缺少系统依赖',
                'details': '请查看根目录 README.md “源码启动”的第二步（PDF 导出依赖）了解安装方法',
                'help_url': 'https://github.com/666ghj/BettaFish#2-安装-pdf-导出所需系统依赖可选',
                'system_message': pango_message
            }), 503

        data = request.get_json() or {}
        if not isinstance(data, dict):
            logger.warning("export_pdf_from_ir 请求体不是JSON对象")
            return jsonify({
                'success': False,
                'error': '请求体必须是JSON对象'
            }), 400

        if not data or 'document_ir' not in data:
            return jsonify({
                'success': False,
                'error': '缺少document_ir参数'
            }), 400

        document_ir = data['document_ir']
        optimize = data.get('optimize', True)

        # 创建PDF渲染器并生成PDF
        from .renderers import PDFRenderer
        renderer = PDFRenderer()

        logger.info(f"从IR直接导出PDF，布局优化: {optimize}")

        # 生成PDF字节流
        pdf_bytes = renderer.render_to_bytes(document_ir, optimize_layout=optimize)

        # 确定下载文件名
        topic = document_ir.get('metadata', {}).get('topic', 'report')
        pdf_filename = f"report_{topic}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

        # 返回PDF文件
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename="{pdf_filename}"',
                'Content-Type': 'application/pdf'
            }
        )

    except Exception as e:
        logger.exception(f"从IR导出PDF失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'导出PDF失败: {str(e)}'
        }), 500
