class AppError(Exception):
    """只暴露固定业务错误，不向调用者泄露 SQL 或提供商响应。"""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


def missing() -> AppError:
    return AppError("not_found", 404, "资源不存在或无权访问")


def conflict(message: str) -> AppError:
    return AppError("conflict", 409, message)
