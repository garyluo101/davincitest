
class Config:
    def __init__(self, env: str = 'prod'):

        self.env = env
        self.setting_key_list = ["TestItemName", "TestDescription", "Unit", "LSL", "USL", "TestCase", "TestData",
        "TimeOut", "RetryTimes", "RetryGap", "Detail", "FailStopFlag", "Skip", "FailSkipFlag"]
