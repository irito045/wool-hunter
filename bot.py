"""wool-hunter — QQ/微博 羊毛猎人 入口"""

from dotenv import load_dotenv
load_dotenv()

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

# 初始化 NoneBot（.env 里 LOG_LEVEL=ERROR 压住 NoneBot 日志）
nonebot.init()

# 插件用独立 logging：同时输出到控制台和文件，便于事后排查「该发没发」。
# 文件按 5MB 轮转、保留 5 个备份（bot.log, bot.log.1 …），不会无限增长。
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_fmt)
_file_handler = RotatingFileHandler(
    _LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])

# 注册 OneBot V11 适配器
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 加载插件
nonebot.load_plugins("src/plugins")

if __name__ == "__main__":
    print("🐑 wool-hunter 启动中…")
    nonebot.run()
