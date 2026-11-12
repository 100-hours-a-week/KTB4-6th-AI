class TaskError(Exception):
    """분석 작업 실패. HTTP 상태와 본문은 각 진입점이 code로 결정한다."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
