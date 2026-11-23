"""opencode HTTP 客户端封装(给 OSIS-AI 用)。

把与 opencode server 通信的细节(opencode-ai SDK / question 处理 / 反问监控)
收敛在 `opencode_client` 包内,对调用方只暴露高层入口:`OpencodeClient`、`ask`、
`QuestionMonitor`。

后端需自行启动:`opencode serve --hostname 127.0.0.1 --port 4096`,
且必须在 OSIS 软件所在的项目目录下启动(opencode 读 SKILL / OSIS 读几何文件
都依赖 cwd)。
"""

from .client import (
    OpencodeClient,
    OpencodeError,
    ask,
    extract_text,
    extract_stats,
    DEFAULT_BASE_URL,
    DEFAULT_PROVIDER,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
)
from .monitor import AutoReplyStats, QuestionMonitor

__all__ = [
    "OpencodeClient",
    "OpencodeError",
    "ask",
    "extract_text",
    "extract_stats",
    "DEFAULT_BASE_URL",
    "DEFAULT_PROVIDER",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "AutoReplyStats",
    "QuestionMonitor",
]
__version__ = "0.6.0"
