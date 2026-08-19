"""Rogue Demo 肉鸽游戏原型 - 程序入口。

运行: python main.py
"""
import os

# 必须在导入 pygame 之前设置，启用 SDL 的系统输入法候选框（IME UI）。
# 不设置时 SDL 默认禁用候选框，中文输入法（搜狗/微软拼音等）只显示拼音条而不显示候选词。
os.environ.setdefault("SDL_IME_SHOW_UI", "1")

from game.ui import App
from game.ai_design import clear_api_log


def main():
    # 每次启动程序时清空 api_log.txt，保证日志文件只记录本次运行期间的错误
    clear_api_log()
    app = App()
    app.run()


if __name__ == "__main__":
    main()
