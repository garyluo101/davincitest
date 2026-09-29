
from davincitest.testbase.testcase import TestCase, TestCaseStatus

class AddTest(TestCase):

    def run_test(self):
        result = False
        res = self.TestData["a"] + self.TestData["b"]
        self.ResultValue = res
        if res >= 5:
            result = True
        capacity = self.ResultManager.get_channel_capacity()
        for i in range(capacity):
            self.ResultManager.logger_list[i].debug(f"a={self.TestData['a']}, b={self.TestData['b']}, res={res}")

        for i in range(capacity):
            self.ResultManager.update_result(i, self.TestItemName, result, res, self.TestDescription)
        self.Status = TestCaseStatus.PASS if result else TestCaseStatus.FAIL


class MinusTest(TestCase):
    def run_test(self):
        result = False
        res = self.TestData["a"] - self.TestData["b"]
        self.ResultValue = res
        if res >= 5:
            result = True
        capacity = self.ResultManager.get_channel_capacity()
        for i in range(capacity):
            self.ResultManager.logger_list[i].debug(f"a={self.TestData['a']}, b={self.TestData['b']}, res={res}")

        for i in range(capacity):
            self.ResultManager.update_result(i, self.TestItemName, result, res, self.TestDescription)
        self.Status = TestCaseStatus.PASS if result else TestCaseStatus.FAIL
