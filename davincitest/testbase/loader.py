import importlib
import json
import math
import socket
import traceback
from pathlib import Path

import pandas as pd

from davincitest.testbase.config import Config
from davincitest.testbase.logger import Logger
from davincitest.testbase.testcase import TestCase
from davincitest.testbase.testresult import TestResultConfig
from davincitest.testbase.testsuite import SeqTestSuite


class TestCaseLoader:
    common_config_sheet = "global"

    def __init__(self):
        self.config_file_path = ""
        self.result_config = TestResultConfig()
        self.ChannelCount = 1
        self.DeviceCount = 1
        self.global_config = None
        self._reset_errors()
        self.log = Logger().logger()
        self.config = Config()

    def _reset_errors(self):
        self._module_errs = {"key_error": {}, "case_error": {}, "load_error": {}}

    def get_last_errors(self):
        return {kind: dict(errors) for kind, errors in self._module_errs.items()}

    def set_config(self, file_path):
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")
        if path.stat().st_size == 0:
            raise ValueError(f"Config file is empty: {path}")
        self.config_file_path = str(path)

    def _read_sheet(self, sheet_name):
        if not self.config_file_path:
            raise ValueError("Call set_config() before loading a test suite")
        try:
            # Empty cells remain empty instead of becoming NaN (including text fields).
            return pd.read_excel(self.config_file_path, sheet_name=sheet_name, keep_default_na=False, dtype=object)
        except Exception as exc:
            raise ValueError(f"Cannot read config sheet {sheet_name!r}: {exc}") from exc

    @staticmethod
    def _number(value, name, minimum=0, integer=False):
        try:
            number = float(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not math.isfinite(number) or number < minimum or (integer and not number.is_integer()):
            raise ValueError(f"{name} must be {'an integer' if integer else 'a finite number'} >= {minimum}")
        return int(number) if integer else number

    def _normalize_case(self, values):
        defaults = TestCase().__dict__
        case = {key: value for key, value in values.items() if value != ""}
        for key in self.config.setting_key_list:
            if key != "TestCase":
                case.setdefault(key, defaults.get(key, ""))
        for key in ("TestItemName", "TestCase"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
            case[key] = case[key].strip()
        data = case["TestData"]
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("TestData must be a JSON object")
        case["TestData"] = data
        for key in ("TestDescription", "Unit", "Detail"):
            case[key] = str(case[key])
        case["TimeOut"] = self._number(case["TimeOut"], "TimeOut")
        if case["TimeOut"] == 0:
            raise ValueError("TimeOut must be greater than 0")
        case["RetryTimes"] = self._number(case["RetryTimes"], "RetryTimes", minimum=1, integer=True)
        case["RetryGap"] = self._number(case["RetryGap"], "RetryGap")
        for key in ("Skip", "FailStopFlag", "FailSkipFlag"):
            case[key] = self._number(case[key], key, integer=True)
            if case[key] not in (0, 1):
                raise ValueError(f"{key} must be 0 or 1")
        return case

    def create_test_case(self, index, case_dict, test_mode):
        if case_dict.get("Skip", 0) == 1:
            return None
        test_class = self.load_obj(case_dict["TestCase"])
        if test_class is None:
            return None
        try:
            return test_class(case_dict)
        except Exception:
            self._module_errs["load_error"][f"row {index + 2}: {case_dict['TestItemName']}"] = traceback.format_exc()
            return None

    def load_seq_test_suite(self, test_mode: str):
        self._reset_errors()
        cases = []
        try:
            df = self._read_sheet(test_mode)
        except ValueError as exc:
            self._module_errs["load_error"][test_mode] = str(exc)
            return SeqTestSuite([])
        for key in df.columns:
            if key not in self.config.setting_key_list:
                self._module_errs["key_error"][key] = f"Unknown config column: {key}"
        for key in ("TestItemName", "TestCase", "TestData"):
            if key not in df.columns:
                self._module_errs["key_error"][key] = f"Missing required column: {key}"
        if self._module_errs["key_error"]:
            return SeqTestSuite([])

        names = set()
        for index, row in df.iterrows():
            values = row.to_dict()
            if all(value == "" for value in values.values()):
                continue
            try:
                case_dict = self._normalize_case(values)
                if case_dict["Skip"] == 1:
                    continue
                name = case_dict["TestItemName"]
                if name in names:
                    raise ValueError(f"Duplicate TestItemName: {name}")
                names.add(name)
                case = self.create_test_case(index, case_dict, test_mode)
                if case is not None:
                    cases.append(case)
            except Exception as exc:
                self._module_errs["case_error"][f"row {index + 2}"] = str(exc)
        if not cases and not any(self._module_errs.values()):
            self._module_errs["case_error"][test_mode] = "No enabled test cases in this sheet"
        return SeqTestSuite(cases)

    def load_obj(self, testname):
        try:
            module_name, separator, class_name = testname.rpartition(".")
            if not separator or not module_name or not class_name:
                raise ValueError("TestCase must be a fully qualified module.Class name")
            testclass = getattr(importlib.import_module(module_name), class_name)
            if not self._is_testcase_class(testclass):
                raise TypeError(f"{testname} is not a TestCase subclass implementing run_test()")
            return testclass
        except Exception:
            self._module_errs["load_error"][str(testname)] = traceback.format_exc()
            return None

    @staticmethod
    def _is_testcase_class(obj):
        return isinstance(obj, type) and issubclass(obj, TestCase) and obj.run_test is not TestCase.run_test

    def load_common_settings(self):
        df = self._read_sheet(self.common_config_sheet)
        required = ("StationID", "StationName", "TestSite", "OperatorID", "ProjectName", "TestSWRev", "ChannelCount", "DeviceCount")
        missing = [key for key in required if key not in df.columns]
        if missing:
            raise ValueError(f"global sheet missing columns: {', '.join(missing)}")
        if len(df) != 1:
            raise ValueError("global sheet must contain exactly one settings row")
        values = df.iloc[0]
        channels = self._number(values["ChannelCount"], "ChannelCount", minimum=1, integer=True)
        devices = self._number(values["DeviceCount"], "DeviceCount", minimum=1, integer=True)
        config = TestResultConfig()
        config.PCName = socket.gethostname()
        for key in required[:6]:
            setattr(config, key, str(values[key]))
        global_config = None
        if "GlobalConfig" in df.columns and values["GlobalConfig"] != "":
            try:
                global_config = json.loads(values["GlobalConfig"])
                if not isinstance(global_config, dict):
                    raise ValueError("must be a JSON object")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"GlobalConfig Error: {exc}") from exc
        self.ChannelCount, self.DeviceCount = channels, devices
        self.result_config, self.global_config = config, global_config
        return channels, devices, config, global_config


if __name__ == "__main__":
    loader = TestCaseLoader()
    loader.set_config(Path(__file__).resolve().parents[1] / "config1.xlsx")
    loader.load_common_settings()
    for index, item in enumerate(loader.load_seq_test_suite("debug")):
        print(index, item.TestItemName)
    if any(loader.get_last_errors().values()):
        raise ValueError(loader.get_last_errors())
