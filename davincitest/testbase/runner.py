import math
import sys
import threading
import time
import traceback
from enum import Enum

import kthread

from davincitest.testbase.logger import Logger
from davincitest.testbase.testcase import DAVINCIException, TestCase, TestCaseStatus
from davincitest.testbase.testsuite import SeqTestSuite


class RunnerState(Enum):
    RUNNING = 1
    IDLE = 0


class FailStopFlag(Enum):
    ContinueRun = 0
    StopRun = 1


class TestCaseRunnerBase:
    def run(self, testcase: TestCase):
        raise NotImplementedError("run method must be implemented in subclass")


class TestCaseRunner(TestCaseRunnerBase):
    CLEANUP_TIMEOUT = 5

    def __init__(self, stop_event=None):
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._error = None
        self._testcase = None
        self.case_thread = None
        self._unsafe_to_continue = False
        self._interruption_reason = None
        self.log = Logger().logger()
        self.erase_flag = True

    def _log_testcase_event(self, event_type, message):
        manager = self._testcase.ResultManager
        if manager is None:
            self.log.info("[%s];testcase:%s;%s", event_type, self._testcase.TestItemName, message)
            return
        for index in range(manager.get_channel_capacity()):
            logger = manager.get_logger(index)
            message_text = (f"[{event_type}][{manager.get_channel_index()}][{index}][{manager.get_sn(index) or None}];"
                            f"testcase:{self._testcase.TestItemName};{message}")
            (logger.error if event_type == "TestError" else logger.info)(message_text)

    def _log_testcase_start(self):
        self._log_testcase_event("TestStart", f"TestData:{self._testcase.TestData}")

    def _log_testcase_end(self):
        self._log_testcase_event("TestEnd", f"TestCaseStatus:{self._testcase.Status.name}")

    def _log_testcase_error(self, error_str):
        self._log_testcase_event("TestError", f"Error:{error_str}")

    def _capture_error(self, exc):
        message = str(exc) if isinstance(exc, DAVINCIException) else traceback.format_exc()
        if isinstance(exc, DAVINCIException):
            self.erase_flag = False
        if self._error is None:
            self._error = message
        else:
            self._error += f"\nCleanup error: {message}"

    def _thread_run(self):
        try:
            try:
                self._testcase.pre_test()
                if not self._testcase.cancel_event.is_set():
                    self._testcase.run_test()
            except SystemExit:
                if not self._testcase.cancel_event.is_set():
                    self._capture_error(RuntimeError("Test case raised SystemExit"))
            except BaseException as exc:
                self._capture_error(exc)
        finally:
            # Teardown stays in the same worker: it never races a still-blocked device operation.
            try:
                self._testcase.post_test()
            except SystemExit:
                self._capture_error(RuntimeError("Cleanup interrupted"))
            except BaseException as exc:
                self._capture_error(exc)
            finally:
                if self._testcase.cancel_event.is_set():
                    # A late teardown must not turn a timed-out device back into PASS.
                    self._publish_failure(self._interruption_reason or "Test case execution stopped")

    @staticmethod
    def _get_current_tracebck(thread):
        stack = sys._current_frames().get(thread.ident)
        if stack is None:
            return "Thread already exited"
        return "".join(traceback.format_stack(stack))

    def _publish_failure(self, message):
        case = self._testcase
        case.Status = TestCaseStatus.FAIL
        manager = case.ResultManager
        if manager is not None:
            for index in range(manager.get_channel_capacity()):
                manager.update_result(index, case.TestItemName, False, 0 if self.erase_flag else case.ResultValue, message)
        self._log_testcase_error(message)

    def _finalize_result(self):
        case = self._testcase
        manager = case.ResultManager
        if manager is None:
            if case.Status not in (TestCaseStatus.PASS, TestCaseStatus.FAIL):
                self._publish_failure("Test case did not publish a PASS or FAIL result")
            return
        results = manager.get_case_results(case.TestItemName)
        if case.Status not in (TestCaseStatus.PASS, TestCaseStatus.FAIL):
            if any(result is None for result in results):
                self._publish_failure("Test case did not publish a result for every device")
                return
            case.Status = TestCaseStatus.PASS if all(results) else TestCaseStatus.FAIL
        for index, result in enumerate(results):
            if result is None:
                manager.update_result(index, case.TestItemName, case.Status == TestCaseStatus.PASS,
                                      case.ResultValue, case.Detail)
        if case.Status == TestCaseStatus.FAIL and all(manager.get_case_results(case.TestItemName)):
            self._publish_failure(case.Detail or "Test case reported FAIL despite device PASS results")
        if not all(manager.get_case_results(case.TestItemName)):
            case.Status = TestCaseStatus.FAIL

    def run(self, testcase):
        if self.case_thread is not None and self.case_thread.is_alive():
            raise RuntimeError("Previous test case worker is still active")
        if not isinstance(testcase.TimeOut, (int, float)) or not math.isfinite(testcase.TimeOut) or testcase.TimeOut <= 0:
            raise ValueError("TimeOut must be a positive finite number")
        self._testcase = testcase
        self._error = None
        self.erase_flag = True
        self._unsafe_to_continue = False
        self._interruption_reason = None
        testcase.Status = TestCaseStatus.NT
        testcase.ResultValue = 0
        testcase.cancel_event = threading.Event()
        if testcase.ResultManager is not None:
            testcase.ResultManager.reset_case_result(testcase.TestItemName)
        self._log_testcase_start()

        self.case_thread = kthread.KThread(target=self._thread_run, daemon=True)
        if testcase.ResultManager is not None:
            testcase.ResultManager._test_suite._active_case_thread = self.case_thread
        self.case_thread.start()
        deadline = time.monotonic() + testcase.TimeOut
        while self.case_thread.is_alive() and not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.case_thread.join(min(remaining, 0.05))

        interrupted = self._stop_event.is_set()
        timed_out = self.case_thread.is_alive() and not interrupted
        if timed_out or interrupted:
            self._interruption_reason = "Test case execution Timeout" if timed_out else "Test case execution stopped"
            testcase.cancel_event.set()
            if self.case_thread.is_alive():
                self.log.warning("Stopping test worker:\n%s", self._get_current_tracebck(self.case_thread))
                try:
                    self.case_thread.terminate()
                except threading.ThreadError:
                    pass  # The worker may finish between is_alive() and terminate().
                self.case_thread.join(self.CLEANUP_TIMEOUT)
            self._unsafe_to_continue = self.case_thread.is_alive()
            message = self._interruption_reason
            if self._unsafe_to_continue:
                message += "; worker/cleanup still active, suite stopped"
            if self._error is not None:
                message += f"\n{self._error}"
            self._publish_failure(message)
        elif self._error is not None:
            self._publish_failure(self._error.strip() or "DAVINCIException")
        else:
            self._finalize_result()
        self._log_testcase_end()
        return testcase.Status

    def request_stop(self):
        self._stop_event.set()
        if self._testcase is not None:
            self._testcase.cancel_event.set()

    def run_with_retry(self, testcase):
        attempts = testcase.RetryTimes
        gap = testcase.RetryGap
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise ValueError("RetryTimes must be an integer >= 1 (total attempts)")
        if not isinstance(gap, (int, float)) or not math.isfinite(gap) or gap < 0:
            raise ValueError("RetryGap must be a finite number >= 0")
        for attempt in range(attempts):
            if self._stop_event.is_set():
                break
            self.run(testcase)
            if testcase.Status == TestCaseStatus.PASS or self._unsafe_to_continue:
                break
            if attempt + 1 < attempts and self._stop_event.wait(gap):
                break
        if self._unsafe_to_continue or self._stop_event.is_set() or (
            testcase.Status == TestCaseStatus.FAIL and testcase.FailStopFlag == FailStopFlag.StopRun.value
        ):
            return FailStopFlag.StopRun
        return FailStopFlag.ContinueRun


class SeqTestSuiteRunner:
    def __init__(self, test_suite: SeqTestSuite):
        self._state = RunnerState.IDLE
        self.previous_state = self._state
        self.run_thread = None
        self.stop_flag = False
        self.test_suite = test_suite
        self.case_runner = None
        self.last_error = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self.running_to_idle_callback = lambda: None
        self.idle_to_running_callback = lambda: None
        self.log = Logger().logger()

    def _prepare_run(self):
        with self._lock:
            if self.state == RunnerState.RUNNING or (self.run_thread is not None and self.run_thread.is_alive()):
                raise RuntimeError("Test suite is already running")
            active = getattr(self.test_suite, "_active_case_thread", None)
            if active is not None and active.is_alive():
                raise RuntimeError("Previous test worker/cleanup is still active")
            self._stop_event.clear()
            self.stop_flag = False
            self.last_error = None
            self.test_suite.reset_case_time()
            managers = set()
            for case in self.test_suite:
                case.Status = TestCaseStatus.NT
                case.ResultValue = 0
                if case.ResultManager is not None:
                    managers.add(case.ResultManager)
            for manager in managers:
                manager.begin_run()
            self.test_suite._running = True
            self._set_state(RunnerState.RUNNING)

    def _execute_suite(self):
        previous_passed = True
        try:
            for index, testcase in enumerate(self.test_suite):
                if self._stop_event.is_set():
                    break
                if testcase.Skip == 1 or (testcase.FailSkipFlag == 1 and not previous_passed):
                    self.log.info("Skipping test case %s", testcase.TestItemName)
                    continue
                self.case_runner = TestCaseRunner(self._stop_event)
                started = time.monotonic()
                try:
                    flag = self.case_runner.run_with_retry(testcase)
                finally:
                    self.test_suite.add_case_time(time.monotonic() - started, index)
                previous_passed = previous_passed and testcase.Status == TestCaseStatus.PASS
                if flag == FailStopFlag.StopRun:
                    break
        except Exception as exc:
            self.last_error = exc
            raise
        finally:
            self.test_suite._running = False
            self._set_state(RunnerState.IDLE)

    def run(self):
        self._prepare_run()
        self._execute_suite()

    def _run_background(self):
        try:
            self._execute_suite()
        except Exception:
            self.log.exception("Test suite execution failed")

    def start_run(self):
        with self._lock:
            self._prepare_run()
            self.run_thread = threading.Thread(target=self._run_background, name="run_test", daemon=True)
            try:
                self.run_thread.start()
            except Exception:
                self.test_suite._running = False
                self._set_state(RunnerState.IDLE)
                raise
        self.log.info("Test thread started")
        return self.run_thread

    def wait(self, timeout=None):
        if self.run_thread is not None and self.run_thread is not threading.current_thread():
            self.run_thread.join(timeout)
            if self.run_thread.is_alive():
                return False
        if self.last_error is not None:
            raise self.last_error
        return self.state == RunnerState.IDLE

    def stop_run(self, timeout=None):
        self.stop_flag = True
        self._stop_event.set()
        if self.case_runner is not None:
            self.case_runner.request_stop()
        if self.run_thread is threading.current_thread():
            return False
        return self.wait(TestCaseRunner.CLEANUP_TIMEOUT + 1 if timeout is None else timeout)

    @property
    def cleanup_pending(self):
        active = getattr(self.test_suite, "_active_case_thread", None)
        return active is not None and active.is_alive() and self.state == RunnerState.IDLE

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        self.previous_state = self._state
        self._state = new_state
        if self.previous_state != new_state:
            self.on_state_change(new_state)

    def _set_state(self, new_state):
        self.state = new_state

    def on_state_change(self, new_state):
        self.log.info("Runner state changed from %s to %s", self.previous_state, new_state)
        callback = self.running_to_idle_callback if new_state == RunnerState.IDLE else self.idle_to_running_callback
        try:
            callback()
        except Exception:
            self.log.exception("Runner state callback failed")

    def set_running_to_idle_callback(self, callback, *args, **kwargs):
        self.running_to_idle_callback = lambda: callback(*args, **kwargs)

    def set_idle_to_running_callback(self, callback, *args, **kwargs):
        self.idle_to_running_callback = lambda: callback(*args, **kwargs)
