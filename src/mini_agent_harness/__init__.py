"""Mini Agent Harness。导入包不会读密钥、启动线程或创建目录。"""

__version__ = "0.1.0"


def __getattr__(name: str):
    # 延迟导入方便只使用配置、工具类型等底层模块的应用。
    if name == "Harness":
        from .app import Harness
        return Harness
    if name == "Settings":
        from .config import Settings
        return Settings
    raise AttributeError(name)
