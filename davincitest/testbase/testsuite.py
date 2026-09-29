from davincitest.testbase.testcase import TestCase

class TestSuiteBase:

    @property
    def suite_class_name(self):

        cls = type(self)
        if cls.__module__ == "__main__":
            return cls.__name__
        return cls.__module__ + "." + cls.__name__

class SeqTestSuite(TestSuiteBase):

    def __init__(self, testcases: list[TestCase]):
        self._testcases = testcases
        self._case_take_time = [0.0] * len(testcases)
        self.__share_data_mgr = None


    def __iter__(self):
        for it in self._testcases:
            yield it

    def __len__(self):
        return len(self._testcases)

    def get_case_time(self):
        return list(self._case_take_time)

    def reset_case_time(self):
        self._case_take_time = [0.0] * len(self._testcases)
        self._next_time_index = 0

    def add_case_time(self, duration, index=None):
        if index is None:
            index = getattr(self, "_next_time_index", 0)
            self._next_time_index = index + 1
        self._case_take_time[index] = duration

    @property
    def share_data_mgr(self):
        return self.__share_data_mgr

    @share_data_mgr.setter
    def share_data_mgr(self, mgr):
        self.__share_data_mgr = mgr
        for it in self._testcases:
            it.share_data_mgr = mgr
