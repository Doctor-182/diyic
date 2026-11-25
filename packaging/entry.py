# -*- coding: utf-8 -*-
"""打包入口：装成 VRDub.exe 之后，所有进程（主程序 + 三个 Qt 子程序）都从这里进来。

--child NAME 由 vrdub.app.main() 内部第一行接管，所以这里不需要额外判断；
multiprocessing.freeze_support() 是给冻结环境的固定仪式（哪怕目前没用进程池）。
"""
import multiprocessing
import sys

from vrdub import app

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(app.main(sys.argv[1:]))
