import builtins
import threading

from davincitest.testbase.loader import TestCaseLoader
from davincitest.testbase.logger import Logger
from davincitest.testbase.runner import RunnerState, SeqTestSuiteRunner
from davincitest.testbase.testresult import TestResultManager
from davincitest.testbase.testsuite import SeqTestSuite


class TestManager:
    __instance = None
    __lock = threading.RLock()

    def __new__(cls):
        with cls.__lock:
            if cls.__instance is None:
                cls.__instance = super().__new__(cls)
            return cls.__instance

    def __init__(self):
        with self.__lock:
            if getattr(self, "_initialized", False):
                return
            self.log = Logger().logger()
            self.loader = TestCaseLoader()
            self.test_suite_list = []
            self.result_manager_list = []
            self.runner_list = []
            self.channel_count = 1
            self.device_count = 1
            self.result_config = None
            self.global_config = None
            self._config_lock = threading.RLock()
            self._initialized = True

    def overload_print(self):
        builtins.print = self.custom_print

    def custom_print(self, *args, level="info", **kwargs):
        separator = kwargs.get("sep", " ")
        message = (" " if separator is None else separator).join(str(arg) for arg in args)
        getattr(self.log, level if level in ("info", "debug", "warning", "error") else "info")(message, stacklevel=2)

    def _ensure_not_running(self, channel_index=None):
        runners = self.runner_list if channel_index is None else [self.runner_list[channel_index]]
        for runner in runners:
            if runner is None:
                continue
            active = getattr(runner.test_suite, "_active_case_thread", None)
            if runner.state == RunnerState.RUNNING or (active is not None and active.is_alive()):
                raise RuntimeError("Cannot replace or close a channel while testing/cleanup is active")

    def _check_channel(self, channel_index):
        if not isinstance(channel_index, int) or isinstance(channel_index, bool):
            raise ValueError("channel_index must be int")
        if not self.runner_list:
            raise ValueError("Call set_config() before loading a channel")
        if not 0 <= channel_index < self.channel_count:
            raise IndexError(f"Channel {channel_index} out of range (0..{self.channel_count - 1})")

    def set_config(self, file_path):
        with self._config_lock:
            self._ensure_not_running()
            loader = TestCaseLoader()
            loader.set_config(file_path)
            channels, devices, config, global_config = loader.load_common_settings()
            for manager in self.result_manager_list:
                if manager is not None:
                    manager.close_loggers()
            self.loader = loader
            self.channel_count, self.device_count = channels, devices
            self.result_config, self.global_config = config, global_config
            self.test_suite_list = [None] * channels
            self.runner_list = [None] * channels
            self.result_manager_list = [None] * channels

    def load_test_suite_to_channel(self, test_mode: str, channel_index: int, share_data_mgr=None):
        with self._config_lock:
            self._check_channel(channel_index)
            self._ensure_not_running(channel_index)
            suite = self.loader.load_seq_test_suite(test_mode)
            errors = self.loader.get_last_errors()
            if any(errors.values()):
                messages = [f"{kind}: {key} - {value}" for kind, values in errors.items() for key, value in values.items()]
                for message in messages:
                    self.log.error(message)
                raise ValueError("Test suite load failed:\n" + "\n".join(messages))
            suite.share_data_mgr = share_data_mgr
            result_manager = TestResultManager(channel_index, self.device_count, suite, self.result_config,
                                               self.global_config, test_mode)
            try:
                result_manager.init_test_result_table(self.device_count)
                self._bind_result_manager_to_testcase(suite, result_manager)
            except Exception:
                result_manager.close_loggers()
                raise
            runner = SeqTestSuiteRunner(suite)
            previous = self.result_manager_list[channel_index]
            if previous is not None:
                previous.close_loggers()
            self.test_suite_list[channel_index] = suite
            self.result_manager_list[channel_index] = result_manager
            self.runner_list[channel_index] = runner
            return runner

    @staticmethod
    def _bind_result_manager_to_testcase(test_suite: SeqTestSuite, result_manager: TestResultManager):
        for case in test_suite:
            case.ResultManager = result_manager

    def get_result_info_channel_total(self, channel_index: int):
        with self._config_lock:
            self._check_channel(channel_index)
            manager = self.result_manager_list[channel_index]
            if manager is None:
                raise ValueError(f"Channel {channel_index} has no loaded test suite")
            return manager.get_result_info_hor()

    def close(self):
        with self._config_lock:
            self._ensure_not_running()
            for manager in self.result_manager_list:
                if manager is not None:
                    manager.close_loggers()
