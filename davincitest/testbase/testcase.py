from enum import Enum
import threading

class TestCaseStatus(Enum):
    PASS = 0
    FAIL = 1
    NT = -1

class DAVINCIException(Exception):
    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(self.message)

class TestCase:
    def __init__(self, attrs: dict | None = None):
        self.Unit: str = ""
        self.LSL: float = 0
        self.USL: float = 0
        self.ResultValue: float = 0
        self.TestItemName: str = ""
        self.TestDescription: str = ""
        self.Detail: str = ""
        self.Status = TestCaseStatus.NT
        self.TimeOut: float = 60
        self.RetryTimes: int = 1
        self.RetryGap: float = 3
        self.TestData: dict = {}
        self.ResultManager = None
        self.FailStopFlag: int = 0
        self.FailSkipFlag: int = 0
        self.Skip: int = 0
        self.share_data_mgr = None
        self.cancel_event = threading.Event()

        if attrs:
            for key, value in attrs.items():
                setattr(self, key, value)

    def pre_test(self):
        pass

    def run_test(self):
        raise NotImplementedError(f"Please implement run_test() in {self.__class__.__name__}")

    def post_test(self):
        pass

    def __str__(self):
        return (f"TestItemName : {self.TestItemName}, "
                f"TestDescription : {self.TestDescription}, "
                f"Unit : {self.Unit}, "
                f"LSL : {self.LSL}, "
                f"USL : {self.USL}, "
                f"ResultValue : {self.ResultValue}, "
                f"Status : {self.Status}"
                f"Detail : {self.Detail}")
