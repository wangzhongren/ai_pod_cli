"""Pre-built infrastructure entities provided by the system."""


class DbClient:
    """系统底层自带的数据库客户端"""

    def query(self, sql: str):
        print(f"   [DbClient] 物理执行SQL成功 -> 查得当前库存: 5")
        return {"stock": 5}


class SmsSender:
    """系统底层自带的短信发送服务"""

    def send(self, phone: str, msg: str):
        print(f"   [SmsSender] 物理网络发信成功 -> 向手机 {phone} 发送: {msg}")
