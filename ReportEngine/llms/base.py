"""
Report Engine 默认的OpenAI兼容LLM客户端封装。

提供统一的非流式/流式调用、可选重试、字节安全拼接与模型元信息查询。
"""

import os
import sys
from typing import Any, Dict, Optional, Generator
from loguru import logger

from openai import OpenAI

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
utils_dir = os.path.join(project_root, "utils")
if utils_dir not in sys.path:
    sys.path.append(utils_dir)

try:
    from retry_helper import with_retry, LLM_RETRY_CONFIG
except ImportError:
    def with_retry(config=None):
        """简化版with_retry占位，实现与真实装饰器一致的调用签名"""
        def decorator(func):
            """直接返回原函数，确保无retry依赖时代码仍可运行"""
            return func
        return decorator

    LLM_RETRY_CONFIG = None


class LLMClient:
    """针对OpenAI Chat Completion API的轻量封装，统一Report Engine调用入口。"""

    def __init__(self, api_key: str, model_name: str, base_url: Optional[str] = None):
        """
        初始化LLM客户端并保存基础连接信息。

        Args:
            api_key: 用于鉴权的API Token
            model_name: 具体模型ID，用于定位供应商能力
            base_url: 自定义兼容接口地址，默认为OpenAI官方
        """
        if not api_key:
            raise ValueError("Report Engine LLM API key is required.")
        if not model_name:
            raise ValueError("Report Engine model name is required.")

        self.api_key = api_key
        self.base_url = base_url
        self.model_name = model_name
        self.provider = model_name
        timeout_fallback = os.getenv("LLM_REQUEST_TIMEOUT") or os.getenv("REPORT_ENGINE_REQUEST_TIMEOUT") or "3000"
        try:
            self.timeout = float(timeout_fallback)
        except ValueError:
            self.timeout = 3000.0

        client_kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "max_retries": 0,
        }
        
        if base_url:
            client_kwargs["base_url"] = base_url
            
        import httpx
        # 创建自定义的 httpx Client 以绕过底层的 Header 规范化机制
        # HTTP/1.1 允许保留 Header 的原始大小写
        headers = {}
        if base_url and "omnisaas.cn" in base_url:
            headers["apikey"] = api_key
            
        custom_http_client = httpx.Client(
            headers=headers,
            timeout=self.timeout
        )
        client_kwargs["http_client"] = custom_http_client

        self.client = OpenAI(**client_kwargs)

    @with_retry(LLM_RETRY_CONFIG)
    def invoke(self, system_prompt: str, user_prompt: str, **kwargs) -> str:
        """
        以非流式方式调用LLM，并返回一次性完成的完整响应。

        Args:
            system_prompt: 系统角色提示
            user_prompt: 用户高优先级指令
            **kwargs: 允许透传temperature/top_p等采样参数

        Returns:
            去除首尾空白后的LLM响应文本
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        allowed_keys = {"temperature", "top_p", "presence_penalty", "frequency_penalty", "stream"}
        extra_params = {key: value for key, value in kwargs.items() if key in allowed_keys and value is not None}

        timeout = kwargs.pop("timeout", self.timeout)

        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            timeout=timeout,
            **extra_params,
        )

        if response.choices and response.choices[0].message:
            return self.validate_response(response.choices[0].message.content)
        return ""

    def stream_invoke(self, system_prompt: str, user_prompt: str, **kwargs) -> Generator[str, None, None]:
        """
        流式调用LLM，逐步返回响应内容。
        带有防御性的自动截断逻辑，以防大模型抛出 "request was too large" 且导致无限重试。
        
        参数:
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。
            **kwargs: 采样参数（temperature、top_p等）。
            
        产出:
            str: 每次yield一段delta文本，方便上层实时渲染。
        """
        allowed_keys = {"temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens"}
        extra_params = {key: value for key, value in kwargs.items() if key in allowed_keys and value is not None}
        if "max_tokens" not in extra_params:
            extra_params["max_tokens"] = 8192
        # 强制使用流式
        extra_params["stream"] = True

        # =======================
        # 新增防御机制：动态截断以防御 "request was too large"
        # =======================
        MAX_CHARS = 100000  # 初始边界：约3万Token
        if len(user_prompt) > MAX_CHARS:
            logger.warning(f"用户提示词过长 ({len(user_prompt)} 字符)，触发初始截断以防止 'request was too large'。")
            half = MAX_CHARS // 2
            user_prompt = user_prompt[:half] + "\n\n...[由于长度限制，中间部分已被系统自动截断]...\n\n" + user_prompt[-half:]

        while True:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            timeout = kwargs.get("timeout", self.timeout)

            try:
                stream = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    timeout=timeout,
                    **extra_params,
                )
                
                for chunk in stream:
                    if chunk.choices and len(chunk.choices) > 0:
                        delta = chunk.choices[0].delta
                        if delta and delta.content:
                            yield delta.content
                break  # 成功后退出循环
                
            except Exception as e:
                error_msg = str(e)
                
                # 处理超大请求报错（动态递减截断）
                if "request was too large" in error_msg.lower() or "context length exceeded" in error_msg.lower():
                    # 计算新的更小的长度限制
                    current_len = len(user_prompt)
                    new_len = int(current_len * 0.7)  # 每次缩减 30%
                    
                    if new_len < 1000:
                        # 缩减到极限仍失败，说明是其他问题，抛出异常
                        logger.error(f"用户提示词已缩减至极限 ({current_len} 字符)，仍然提示请求过大。停止重试。")
                        raise e
                        
                    logger.warning(f"大模型返回 'request was too large'。当前字符数: {current_len}。系统正在自动缩减 30% 至 {new_len} 字符并立即重试...")
                    half = new_len // 2
                    user_prompt = user_prompt[:half] + "\n\n...[因模型上下文溢出，中间部分被系统自动截断]...\n\n" + user_prompt[-half:]
                    continue  # 继续循环进行下一次尝试

                logger.error(f"流式请求失败: {error_msg}")
                # 如果是 502/504 等网关错误导致的 HTML 响应，包装成更明确的异常，以便上层重试机制能够捕获
                if "<html>" in error_msg.lower() or "502 bad gateway" in error_msg.lower() or "504 gateway time-out" in error_msg.lower():
                    raise Exception(f"API 网关超时或返回了无效的 HTML 响应: {error_msg[:200]}...") from e
                raise e
    
    @with_retry(LLM_RETRY_CONFIG)
    def stream_invoke_to_string(self, system_prompt: str, user_prompt: str, **kwargs) -> str:
        """
        流式调用LLM并安全地拼接为完整字符串（避免UTF-8多字节字符截断）。
        
        参数:
            system_prompt: 系统提示词。
            user_prompt: 用户提示词。
            **kwargs: 采样或超时配置。
            
        返回:
            str: 将所有delta拼接后的完整响应。
        """
        # 以字节形式收集所有块
        byte_chunks = []
        for chunk in self.stream_invoke(system_prompt, user_prompt, **kwargs):
            byte_chunks.append(chunk.encode('utf-8'))
        
        # 拼接所有字节，然后一次性解码
        if byte_chunks:
            return b''.join(byte_chunks).decode('utf-8', errors='replace')
        return ""

    @staticmethod
    def validate_response(response: Optional[str]) -> str:
        """兜底处理None/空白字符串，防止上层逻辑崩溃"""
        if response is None:
            return ""
        return response.strip()

    def get_model_info(self) -> Dict[str, Any]:
        """以字典形式返回当前客户端的模型/提供方/基础URL信息"""
        return {
            "provider": self.provider,
            "model": self.model_name,
            "api_base": self.base_url or "default",
        }
