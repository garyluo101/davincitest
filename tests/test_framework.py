import csv
import datetime
import io
import logging
import os
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

_LOG_DIR = tempfile.TemporaryDirectory(prefix="davincitest-tests-logs-")
os.environ["DAVINCITEST_LOG_DIR"] = _LOG_DIR.name

from davincitest.manage import TestManager
from davincitest.main import main
from davincitest.testbase.dtams import DtasHeader, DtasRow, dtas_csv
from davincitest.testbase.loader import TestCaseLoader
from davincitest.testbase.logger import DailyLogHandler, DutLogger, Logger
from davincitest.testbase.runner import FailStopFlag, RunnerState, SeqTestSuiteRunner, TestCaseRunner
from davincitest.testbase.testcase import DAVINCIException, TestCase, TestCaseStatus
from davincitest.testbase.testresult import TestResultConfig, TestResultManager
from davincitest.testbase.testsuite import SeqTestSuite


class PassingCase(TestCase):
    def run_test(self):
        self.Status = TestCaseStatus.PASS
        self.ResultValue = 7


class FailingCase(TestCase):
    def run_test(self):
        self.Status = TestCaseStatus.FAIL


class LifecycleCase(PassingCase):
    def __init__(self, attrs=None):
        super().__init__(attrs)
        self.calls = []

    def pre_test(self):
        self.calls.append("pre")

    def run_test(self):
        self.calls.append("run")
        super().run_test()

    def post_test(self):
        self.calls.append("post")


class FrameworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.managers = []
        self.addCleanup(self.close_managers)

    def close_managers(self):
        for manager in self.managers:
            manager.close_loggers()

    def manager(self, cases, devices=1, channel=0):
        suite = SeqTestSuite(cases)
        config = TestResultConfig()
        config.StationID = "001"
        config.StationName = "station"
        config.ProjectName = "project"
        manager = TestResultManager(channel, devices, suite, config, {}, "debug")
        manager.init_test_result_table(devices)
        for case in cases:
            case.ResultManager = manager
        self.managers.append(manager)
        return suite, manager

    def case(self, cls=PassingCase, name="item", **attrs):
        return cls({"TestItemName": name, "RetryGap": 0, **attrs})

    def workbook(self, rows=None, global_values=None, filename="config.xlsx"):
        book = Workbook()
        common = book.active
        common.title = "global"
        common.append(["StationID", "StationName", "TestSite", "OperatorID", "ProjectName", "TestSWRev", "ChannelCount", "DeviceCount", "GlobalConfig"])
        common.append(global_values or ["001", "station", "site", "007", "project", "2.3.4", 2, 2, '{"enabled": true}'])
        sheet = book.create_sheet("debug")
        sheet.append(["TestItemName", "TestCase", "TestData", "TimeOut", "RetryTimes", "RetryGap", "FailStopFlag", "Skip", "FailSkipFlag"])
        for row in rows or []:
            sheet.append(row)
        path = Path(self.tmp.name) / filename
        book.save(path)
        book.close()
        return path

    def good_row(self, name="item"):
        return [name, f"{PassingCase.__module__}.PassingCase", "{}", 1, 1, 0, 0, 0, 0]

    def test_default_cases_execute_and_record_results(self):
        cases = [self.case(LifecycleCase, "first"), self.case(LifecycleCase, "second")]
        suite, manager = self.manager(cases, devices=2)
        runner = SeqTestSuiteRunner(suite)
        states = []
        runner.set_idle_to_running_callback(lambda: states.append(runner.state))
        runner.set_running_to_idle_callback(lambda: states.append(runner.state))
        runner.run()
        self.assertEqual([case.calls for case in cases], [["pre", "run", "post"]] * 2)
        self.assertEqual(states, [RunnerState.RUNNING, RunnerState.IDLE])
        self.assertEqual(len(suite.get_case_time()), 2)
        self.assertTrue(all(manager.get_result(i) for i in range(2)))

    def test_pre_failure_skips_run_and_still_cleans_up(self):
        class Broken(LifecycleCase):
            def pre_test(self):
                self.calls.append("pre")
                raise ValueError("setup broken")
        case = self.case(Broken)
        _, manager = self.manager([case])
        TestCaseRunner().run(case)
        self.assertEqual(case.calls, ["pre", "post"])
        self.assertEqual(case.Status, TestCaseStatus.FAIL)
        self.assertIn("setup broken", manager._test_result_table_list[0]["item"].detail)

    def test_post_failure_overrides_pass(self):
        class Broken(PassingCase):
            def post_test(self):
                raise RuntimeError("cleanup broken")
        case = self.case(Broken)
        _, manager = self.manager([case])
        TestCaseRunner().run(case)
        self.assertEqual(case.Status, TestCaseStatus.FAIL)
        self.assertFalse(manager.get_result(0))

    def test_business_exception_preserves_value_even_with_empty_message(self):
        class Broken(TestCase):
            def run_test(self):
                self.ResultValue = 42
                raise DAVINCIException()
        case = self.case(Broken)
        _, manager = self.manager([case])
        TestCaseRunner().run(case)
        self.assertEqual(case.Status, TestCaseStatus.FAIL)
        self.assertEqual(manager.get_result_value(0, "item"), 42)

    def test_retry_resets_error_and_results(self):
        class Flaky(TestCase):
            attempts = 0
            def run_test(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise DAVINCIException("first attempt")
                self.Status = TestCaseStatus.PASS
                self.ResultValue = 8
        case = self.case(Flaky, RetryTimes=3)
        _, manager = self.manager([case], devices=2)
        TestCaseRunner().run_with_retry(case)
        self.assertEqual(case.attempts, 2)
        self.assertTrue(manager.get_result(0))
        self.assertTrue(manager.get_result(1))

    def test_retry_is_driven_by_all_device_results(self):
        class Partial(TestCase):
            attempts = 0
            def run_test(self):
                self.attempts += 1
                self.ResultManager.update_result(0, self.TestItemName, True, 1, "")
                self.ResultManager.update_result(1, self.TestItemName, self.attempts > 1, 1, "")
                self.Status = TestCaseStatus.PASS
        case = self.case(Partial, RetryTimes=2)
        _, manager = self.manager([case], devices=2)
        TestCaseRunner().run_with_retry(case)
        self.assertEqual(case.attempts, 2)
        self.assertTrue(manager.get_result(1))

    def test_timeout_fails_and_cleans_up_once(self):
        class Slow(LifecycleCase):
            def run_test(self):
                self.calls.append("run")
                time.sleep(0.08)
        case = self.case(Slow, TimeOut=0.01)
        _, manager = self.manager([case])
        runner = TestCaseRunner()
        runner.run(case)
        self.assertEqual(case.Status, TestCaseStatus.FAIL)
        self.assertEqual(case.calls, ["pre", "run", "post"])
        self.assertFalse(runner.case_thread.is_alive())
        self.assertIn("Timeout", manager._test_result_table_list[0]["item"].detail)

    def test_still_blocked_case_stops_suite_and_prevents_restart(self):
        entered = threading.Event()
        release = threading.Event()
        class Blocked(LifecycleCase):
            def run_test(self):
                entered.set()
                release.wait(2)
        case = self.case(Blocked, "blocked", TimeOut=0.02)
        later = self.case(LifecycleCase, "later")
        suite, _ = self.manager([case, later])
        runner = SeqTestSuiteRunner(suite)
        try:
            with patch.object(TestCaseRunner, "CLEANUP_TIMEOUT", 0.01):
                runner.start_run()
                self.assertTrue(entered.wait(1))
                self.assertTrue(runner.wait(1))
                self.assertEqual(later.calls, [])
                with self.assertRaises(RuntimeError):
                    runner.start_run()
        finally:
            release.set()
            runner.case_runner.case_thread.join(1)

    def test_failure_skips_only_dependent_cases_and_keeps_timing_aligned(self):
        cases = [self.case(FailingCase, "fail"), self.case(LifecycleCase, "dependent", FailSkipFlag=1), self.case(LifecycleCase, "independent")]
        suite, manager = self.manager(cases)
        SeqTestSuiteRunner(suite).run()
        self.assertEqual(cases[1].calls, [])
        self.assertEqual(cases[2].calls, ["pre", "run", "post"])
        self.assertEqual(suite.get_case_time()[1], 0)
        self.assertGreater(manager.get_case_time("independent"), 0)

    def test_fail_stop_returns_idle(self):
        cases = [self.case(FailingCase, "fail", FailStopFlag=1), self.case(LifecycleCase, "later")]
        suite, _ = self.manager(cases)
        runner = SeqTestSuiteRunner(suite)
        runner.run()
        self.assertEqual(cases[1].calls, [])
        self.assertEqual(runner.state, RunnerState.IDLE)
        self.assertEqual(len(suite.get_case_time()), 2)

    def test_stop_cleans_up_and_cancels_retry_wait(self):
        entered = threading.Event()
        class Retrying(LifecycleCase):
            def run_test(self):
                entered.set()
                self.Status = TestCaseStatus.FAIL
        case = self.case(Retrying, RetryTimes=3, RetryGap=10)
        suite, _ = self.manager([case])
        runner = SeqTestSuiteRunner(suite)
        runner.start_run()
        self.assertTrue(entered.wait(1))
        self.assertTrue(runner.stop_run(timeout=1))
        self.assertEqual(runner.state, RunnerState.IDLE)

    def test_duplicate_start_is_rejected_and_second_run_resets_results(self):
        entered = threading.Event()
        release = threading.Event()
        class Blocking(PassingCase):
            def run_test(self):
                entered.set()
                release.wait(1)
                super().run_test()
        case = self.case(Blocking)
        suite, manager = self.manager([case])
        manager.set_sn(0, "SN")
        runner = SeqTestSuiteRunner(suite)
        runner.start_run()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(RuntimeError):
            runner.start_run()
        release.set()
        self.assertTrue(runner.wait(1))
        case.run_test = lambda: setattr(case, "Status", TestCaseStatus.FAIL)
        runner.run()
        self.assertFalse(manager.get_result(0))
        self.assertEqual(manager.get_sn(0), "SN")
        self.assertEqual(len(suite.get_case_time()), 1)

    def test_background_error_is_visible_and_state_restored(self):
        case = self.case()
        suite, manager = self.manager([case])
        runner = SeqTestSuiteRunner(suite)
        with patch.object(manager, "reset_case_result", side_effect=RuntimeError("internal error")):
            runner.start_run()
            with self.assertRaisesRegex(RuntimeError, "internal error"):
                runner.wait(1)
        self.assertEqual(runner.state, RunnerState.IDLE)

    def test_loader_uses_defaults_and_preserves_version(self):
        path = self.workbook([["item", f"{PassingCase.__module__}.PassingCase", "", "", "", "", "", "", ""]])
        loader = TestCaseLoader()
        loader.set_config(path)
        channels, devices, config, global_config = loader.load_common_settings()
        suite = loader.load_seq_test_suite("debug")
        self.assertEqual((channels, devices), (2, 2))
        self.assertEqual(config.TestSWRev, "2.3.4")
        self.assertEqual(global_config, {"enabled": True})
        self.assertEqual(next(iter(suite)).TestData, {})
        self.assertEqual(loader.get_last_errors(), {"key_error": {}, "case_error": {}, "load_error": {}})

    def test_loader_clears_previous_errors_and_isolates_instances(self):
        first = TestCaseLoader()
        first.set_config(self.workbook([["bad", "missing.Case", "{}", 1, 1, 0, 0, 0, 0]]))
        first.load_seq_test_suite("debug")
        self.assertTrue(first.get_last_errors()["load_error"])
        second = TestCaseLoader()
        path = self.workbook([self.good_row()], filename="other.xlsx")
        second.set_config(path)
        second.load_common_settings()
        first.load_seq_test_suite("debug")
        self.assertTrue(first.get_last_errors()["load_error"])
        first.set_config(path)
        first.load_seq_test_suite("debug")
        self.assertFalse(any(first.get_last_errors().values()))

    def test_loader_validates_bad_values_missing_columns_and_empty_sheets(self):
        for column, value in [(2, "[1]"), (3, -1), (4, 0), (4, 1.5), (5, -1), (6, 2)]:
            with self.subTest(column=column, value=value):
                row = self.good_row()
                row[column] = value
                loader = TestCaseLoader()
                loader.set_config(self.workbook([row]))
                loader.load_seq_test_suite("debug")
                self.assertTrue(any(loader.get_last_errors().values()))
        loader.set_config(self.workbook())
        self.assertEqual(len(loader.load_seq_test_suite("debug")), 0)
        self.assertTrue(loader.get_last_errors()["case_error"])
        loader.load_seq_test_suite("missing")
        self.assertTrue(loader.get_last_errors()["load_error"])

    def test_loader_rejects_duplicate_names_and_skips_in_all_modes(self):
        row = self.good_row("skip")
        row[1], row[7] = "missing.Case", 1
        loader = TestCaseLoader()
        loader.set_config(self.workbook([self.good_row(), row]))
        self.assertEqual(len(loader.load_seq_test_suite("debug")), 1)
        self.assertFalse(any(loader.get_last_errors().values()))
        loader.set_config(self.workbook([self.good_row(), self.good_row()]))
        loader.load_seq_test_suite("debug")
        self.assertTrue(loader.get_last_errors()["case_error"])

    def test_invalid_global_config_is_not_silently_ignored(self):
        for value in ["{bad json", "[]"]:
            with self.subTest(value=value):
                loader = TestCaseLoader()
                loader.set_config(self.workbook(global_values=["001", "station", "site", "007", "project", "1", 1, 1, value]))
                with self.assertRaises(ValueError):
                    loader.load_common_settings()

    def test_empty_config_file_has_clear_error(self):
        path = Path(self.tmp.name) / "empty.xlsx"
        path.touch()
        with self.assertRaisesRegex(ValueError, "empty|空"):
            TestCaseLoader().set_config(path)

    def test_result_indices_and_names_are_validated(self):
        _, manager = self.manager([self.case()], devices=2)
        for index in [-1, 2]:
            with self.subTest(index=index):
                with self.assertRaises(IndexError):
                    manager.update_result(index, "item", True, 1, "")
                with self.assertRaises(IndexError):
                    manager.get_sn(index)
        with self.assertRaises(KeyError):
            manager.update_result(0, "missing", True, 1, "")

    def test_serial_number_updates_are_atomic(self):
        _, manager = self.manager([self.case()], devices=2)
        manager.set_sn(0, "A")
        manager.set_sn(1, "B")
        with self.assertRaises(ValueError):
            manager.set_sn(1, "A")
        self.assertEqual(manager.get_sn(1), "B")
        manager.set_sn(0, "C")
        with self.assertRaises(ValueError):
            manager.get_index("A")
        manager.set_sn(0, "")
        manager.set_sn(1, "")
        self.assertEqual(manager.sn_index_dict, {})

    def test_report_contains_structured_header_device_log_and_zip(self):
        case = self.case(LifecycleCase)
        suite, manager = self.manager([case], devices=2)
        manager.set_sn(0, "SN001")
        SeqTestSuiteRunner(suite).run()
        data_dir = Path(self.tmp.name) / "data"
        data_dir.mkdir()
        (data_dir / "raw.txt").write_text("raw data")
        report_dir = Path(self.tmp.name) / "reports"
        self.assertEqual(manager.create_dtams_report(str(report_dir), [str(data_dir)]), [True, False])
        csv_path = next(report_dir.rglob("*.csv"))
        with csv_path.open(encoding="gbk", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[1], list(DtasHeader().__dict__))
        self.assertEqual(rows[2][2], "SN001")
        self.assertEqual(rows[5][:2], ["", "item"])
        log_path = next(report_dir.rglob("*.log"))
        self.assertIn("TestEnd", log_path.read_text(encoding="utf-8"))
        with zipfile.ZipFile(next(report_dir.rglob("*.zip"))) as archive:
            self.assertIn("raw.txt", archive.namelist())
            self.assertTrue(any(name.endswith(".csv") for name in archive.namelist()))
        self.assertEqual(manager.create_dtams_report(str(report_dir)), [True, False])
        SeqTestSuiteRunner(suite).run()
        self.assertTrue(manager.get_result(0))

    def test_report_preserves_detail_without_case_time(self):
        _, manager = self.manager([self.case()])
        manager.set_sn(0, "SN")
        manager.update_result(0, "item", False, 0, "original detail")
        manager.create_dtams_report(self.tmp.name)
        with next(Path(self.tmp.name).rglob("*.csv")).open(encoding="gbk", newline="") as stream:
            self.assertIn("original detail", list(csv.reader(stream))[5][-1])

    def test_report_filename_is_safe_for_arbitrary_serial(self):
        _, manager = self.manager([self.case()])
        manager.set_sn(0, "../SN:01/设备")
        manager.create_dtams_report(self.tmp.name)
        csv_path = next(Path(self.tmp.name).rglob("*.csv"))
        self.assertNotIn(":", csv_path.name)
        self.assertNotIn("..", csv_path.name)

    def test_dut_logger_creation_keeps_other_handlers_open(self):
        loggers = DutLogger("test")
        first = loggers.logger("first")
        first.info("before second")
        handler = next(h for h in first.handlers if isinstance(h, logging.FileHandler))
        second = loggers.logger("second")
        self.assertFalse(handler._closed)
        first.info("after second")
        loggers.close_handlers("first")
        loggers.close_handlers("second")

    def test_csv_header_is_not_python_dict_repr(self):
        path = Path(self.tmp.name) / "report.csv"
        dtas_csv(str(path), "station", DtasHeader(), [DtasRow()])
        with path.open(encoding="gbk", newline="") as stream:
            self.assertEqual(list(csv.reader(stream))[1], list(DtasHeader().__dict__))

    def test_manager_load_failure_keeps_previous_channel(self):
        manager = TestManager()
        path = self.workbook([self.good_row()])
        manager.set_config(path)
        manager.load_test_suite_to_channel("debug", 0)
        original = manager.runner_list[0]
        with self.assertRaises(ValueError):
            manager.load_test_suite_to_channel("missing", 0)
        self.assertIs(manager.runner_list[0], original)
        with self.assertRaises(IndexError):
            manager.load_test_suite_to_channel("debug", -1)
        for result in manager.result_manager_list:
            if result is not None:
                result.close_loggers()

    def test_sample_workbook_end_to_end(self):
        manager = TestManager()
        manager.set_config(Path(__file__).resolve().parents[1] / "davincitest" / "config1.xlsx")
        manager.load_test_suite_to_channel("debug", 0)
        for case in manager.test_suite_list[0]:
            case.RetryGap = 0
        runner = manager.runner_list[0]
        runner.start_run()
        self.assertTrue(runner.wait(2))
        result = manager.get_result_info_channel_total(0)[0]
        self.assertEqual(result["add_test"]["value"], "PASS")
        self.assertEqual(result["minus_test"]["value"], "FAIL")
        self.assertEqual(manager.result_manager_list[0].get_result_value(0, "minus_test"), -4)
        manager.result_manager_list[0].close_loggers()

    def test_results_can_drive_status_but_missing_result_fails(self):
        class ResultsOnly(TestCase):
            def run_test(self):
                for index in range(self.ResultManager.get_channel_capacity()):
                    self.ResultManager.update_result(index, self.TestItemName, True, 3, "measured")
        case = self.case(ResultsOnly)
        _, manager = self.manager([case], devices=2)
        TestCaseRunner().run(case)
        self.assertEqual(case.Status, TestCaseStatus.PASS)
        self.assertTrue(manager.get_result(1))
        case.run_test = lambda: None
        TestCaseRunner().run(case)
        self.assertEqual(case.Status, TestCaseStatus.FAIL)
        self.assertFalse(manager.get_result(0))

    def test_explicit_fail_cannot_export_pass(self):
        class Contradiction(TestCase):
            def run_test(self):
                self.ResultManager.update_result(0, self.TestItemName, True, 1, "")
                self.Status = TestCaseStatus.FAIL
        case = self.case(Contradiction)
        _, manager = self.manager([case])
        TestCaseRunner().run(case)
        self.assertFalse(manager.get_result(0))

    def test_second_error_does_not_reuse_business_exception_value(self):
        class DifferentErrors(TestCase):
            attempts = 0
            def run_test(self):
                self.attempts += 1
                self.ResultValue = 55
                if self.attempts == 1:
                    raise DAVINCIException("business")
                raise RuntimeError("other error")
        case = self.case(DifferentErrors, RetryTimes=2)
        _, manager = self.manager([case])
        TestCaseRunner().run_with_retry(case)
        self.assertEqual(manager.get_result_value(0, "item"), 0)

    def test_stop_during_setup_runs_cleanup_once_without_testing(self):
        entered = threading.Event()
        class Setup(LifecycleCase):
            def pre_test(self):
                self.calls.append("pre")
                entered.set()
                self.cancel_event.wait(1)
        case = self.case(Setup)
        suite, _ = self.manager([case])
        runner = SeqTestSuiteRunner(suite)
        runner.start_run()
        self.assertTrue(entered.wait(1))
        self.assertTrue(runner.stop_run(timeout=1))
        self.assertEqual(case.calls, ["pre", "post"])
        self.assertEqual(case.Status, TestCaseStatus.FAIL)

    def test_timeout_in_cleanup_never_runs_cleanup_twice(self):
        class SlowCleanup(LifecycleCase):
            def post_test(self):
                self.calls.append("post")
                time.sleep(0.05)
        case = self.case(SlowCleanup, TimeOut=0.01)
        _, manager = self.manager([case])
        runner = TestCaseRunner()
        runner.run(case)
        self.assertEqual(case.calls, ["pre", "run", "post"])
        self.assertFalse(manager.get_result(0))
        self.assertFalse(runner.case_thread.is_alive())

    def test_report_rejected_while_worker_is_active(self):
        entered = threading.Event()
        release = threading.Event()
        class Active(PassingCase):
            def run_test(self):
                entered.set()
                release.wait(1)
                super().run_test()
        case = self.case(Active)
        suite, manager = self.manager([case])
        runner = SeqTestSuiteRunner(suite)
        runner.start_run()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(RuntimeError):
                manager.create_dtams_report(self.tmp.name)
        finally:
            release.set()
            runner.wait(1)

    def test_channels_run_and_export_independently(self):
        manager = TestManager()
        path = self.workbook([self.good_row()])
        manager.set_config(path)
        shared = object()
        runners = [manager.load_test_suite_to_channel("debug", index, shared) for index in range(2)]
        results = manager.result_manager_list
        try:
            for index, result in enumerate(results):
                result.set_sn(0, f"CHANNEL{index}A")
                result.set_sn(1, f"CHANNEL{index}B")
                self.assertIs(next(iter(manager.test_suite_list[index])).share_data_mgr, shared)
            for runner in runners:
                runner.start_run()
            for runner in runners:
                self.assertTrue(runner.wait(1))
            for result in results:
                self.assertEqual(result.create_dtams_report(self.tmp.name), [True, True])
                self.assertTrue(result.get_result(0))
                self.assertTrue(result.get_result(1))
            self.assertEqual(len(list(Path(self.tmp.name).rglob("*.csv"))), 4)
            self.assertIsNot(results[0]._result_config, results[1]._result_config)
        finally:
            manager.close()

    def test_manager_rejects_config_changes_while_running_and_rolls_back_bad_config(self):
        manager = TestManager()
        path = self.workbook([self.good_row()])
        manager.set_config(path)
        runner = manager.load_test_suite_to_channel("debug", 0)
        entered = threading.Event()
        release = threading.Event()
        case = next(iter(manager.test_suite_list[0]))
        def active():
            entered.set()
            release.wait(1)
            case.Status = TestCaseStatus.PASS
        case.run_test = active
        runner.start_run()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(RuntimeError):
                manager.set_config(path)
            with self.assertRaises(RuntimeError):
                manager.load_test_suite_to_channel("debug", 0)
        finally:
            release.set()
            runner.wait(1)
        bad = self.workbook(global_values=["001", "station", "site", "007", "project", "1", 0, 2, "{}"], filename="bad.xlsx")
        with self.assertRaises(ValueError):
            manager.set_config(bad)
        self.assertIs(manager.runner_list[0], runner)
        manager.close()

    def test_daily_log_rollover_updates_directory_and_lock(self):
        current = [datetime.date(2026, 1, 1)]
        class Day(datetime.date):
            @classmethod
            def today(cls):
                return current[0]
        with patch("davincitest.testbase.logger.datetime.date", Day):
            handler = DailyLogHandler(str(Path(self.tmp.name) / "daily.log"))
            record = logging.LogRecord("rollover", logging.INFO, __file__, 1, "first", (), None)
            handler.emit(record)
            current[0] = datetime.date(2026, 1, 2)
            record.msg = "second"
            handler.emit(record)
            self.assertIn("2026-01-02", handler.lockFilename)
            handler.close()
        self.assertIn("first", (Path(self.tmp.name) / "2026-01-01" / "daily.log").read_text())
        self.assertIn("second", (Path(self.tmp.name) / "2026-01-02" / "daily.log").read_text())

    def test_new_device_runs_use_current_day_and_previous_logs_remain_exportable(self):
        current = [datetime.date(2026, 1, 1)]
        class Day(datetime.date):
            @classmethod
            def today(cls):
                return current[0]
        with patch("davincitest.testbase.logger.datetime.date", Day):
            loggers = DutLogger("test")
            loggers.logger("day1").info("first day")
            current[0] = datetime.date(2026, 1, 2)
            loggers.logger("day2").info("second day")
            self.assertIn("2026-01-01", str(loggers.logger_dict["day1"]["path"]))
            self.assertIn("2026-01-02", str(loggers.logger_dict["day2"]["path"]))
            target = Path(self.tmp.name) / "previous.log"
            self.assertTrue(loggers.redirect("day1", "unused", target))
            self.assertIn("first day", target.read_text())
            loggers.close_handlers("day1")
            loggers.close_handlers("day2")

    def test_callback_exception_does_not_break_suite(self):
        case = self.case()
        suite, manager = self.manager([case])
        runner = SeqTestSuiteRunner(suite)
        def broken_callback():
            raise ValueError("callback problem")
        runner.set_running_to_idle_callback(broken_callback)
        runner.run()
        self.assertTrue(manager.get_result(0))
        self.assertEqual(runner.state, RunnerState.IDLE)

    def test_late_cleanup_cannot_overwrite_timeout_failure(self):
        release = threading.Event()
        class LateCleanup(PassingCase):
            def run_test(self):
                release.wait(2)
            def post_test(self):
                self.ResultManager.update_result(0, self.TestItemName, True, 9, "late pass")
                self.Status = TestCaseStatus.PASS
        case = self.case(LateCleanup, TimeOut=0.01)
        suite, manager = self.manager([case])
        runner = SeqTestSuiteRunner(suite)
        try:
            with patch.object(TestCaseRunner, "CLEANUP_TIMEOUT", 0.01):
                runner.start_run()
                self.assertTrue(runner.wait(1))
                self.assertTrue(runner.cleanup_pending)
        finally:
            release.set()
            runner.case_runner.case_thread.join(1)
        self.assertFalse(runner.cleanup_pending)
        self.assertEqual(case.Status, TestCaseStatus.FAIL)
        self.assertFalse(manager.get_result(0))

    def test_run_and_cleanup_errors_both_survive_in_report_detail(self):
        class BothFail(TestCase):
            def run_test(self):
                raise ValueError("measurement failed")
            def post_test(self):
                raise RuntimeError("cleanup failed")
        case = self.case(BothFail)
        _, manager = self.manager([case])
        TestCaseRunner().run(case)
        detail = manager._test_result_table_list[0]["item"].detail
        self.assertIn("measurement failed", detail)
        self.assertIn("cleanup failed", detail)

    def test_cli_pass_outputs_json_and_exports_reports(self):
        path = self.workbook([self.good_row()])
        report_dir = Path(self.tmp.name) / "reports"
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["--config", str(path), "--sn", "A", "--sn", "B", "--report-dir", str(report_dir)])
        self.assertEqual(exit_code, 0)
        self.assertIn('"value": "PASS"', output.getvalue())
        self.assertEqual(len(list(report_dir.rglob("*.csv"))), 2)

    def test_cli_failure_has_exit_code_one(self):
        row = self.good_row()
        row[1] = f"{FailingCase.__module__}.FailingCase"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.workbook([row]))]), 1)

    def test_cli_invalid_sheet_or_missing_sn_has_exit_code_two(self):
        path = self.workbook([self.good_row()])
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(["--config", str(path), "--mode", "missing"]), 2)
            self.assertEqual(main(["--config", str(path), "--report-dir", self.tmp.name]), 2)
        self.assertIn("missing", error.getvalue())
        self.assertIn("--sn", error.getvalue())


if __name__ == "__main__":
    unittest.main()
