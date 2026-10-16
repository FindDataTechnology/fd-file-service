"""统一文件服务（fd-file-service）。

服务不理解业务：文件归属由调用方以不透明 ``owner`` 字符串声明
（如 ``wire:cust_x``、``yizuo:sub_y``），本服务只负责「该域下的字节」。
"""

__version__ = "0.1.0"
