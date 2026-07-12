"""允许使用 ``python -m app.evaluation`` 启动统一作品集评测 CLI。

入口只委托给 ``portfolio.main``，不复制参数解析或执行逻辑；进程退出码保留完整/快速模式的成功
语义。模块不接受或拼接自由 shell 命令。
"""

from app.evaluation.portfolio import main

raise SystemExit(main())
