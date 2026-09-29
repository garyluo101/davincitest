import json
import os
import shutil
import time
import zipfile
from typing import List
import datetime
import copy
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from davincitest.testbase.dtams import DtasHeader, DtasRow, dtas_csv
from davincitest.testbase.testsuite import SeqTestSuite
from davincitest.testbase.logger import Logger, DutLogger


class TestResultRow:
    def __init__(self, test_item_name: str, test_description: str) -> None:
        if not isinstance(test_item_name, str) or not test_item_name.strip():
            raise ValueError("param test_item_name must be valid str")

        self._test_item_name = test_item_name
        self.test_description = test_description
        self._result = None
        self._status = "PAUSE"
        self.detail = ""
        self.result_value = 0
        self.usl = 0
        self.lsl = 0
        self.unit = ""

    def set_result(self, result: bool):
        if not isinstance(result, bool):
            raise ValueError("result must be bool")
        self._result = result
        if result:
            self._status = "PASS"
        else:
            self._status = "FAIL"

    def is_success(self):
        return self._result is True

    def get_result(self):
        return self._result

    def get_name(self):
        return self._test_item_name

    def get_status(self):
        return self._status

    def __str__(self):
        return (f"TestItemName : {self._test_item_name}, "
                f"TestDescription : {self.test_description}, "
                f"ResultValue : {str(self.result_value)}, "
                f"Unit : {self.unit}, "
                f"LSL : {self.lsl}, "
                f"USL : {self.usl}, "
                f"Status : {self.get_status()}, "
                f"Detail : {self.detail}"
                f"Result : {self.is_success()}")

class TestResultTable:

    def __init__(self, index_: int, test_item_list: List[str], test_description_list: List[str]):
        self._sn: str = ""
        self.index = index_
        self._test_item_list = test_item_list
        self._test_result_list = [TestResultRow(test_item_list[i], test_description_list[i]) \
            for i in range(len(test_item_list))]

    def set_usls(self, usl_list: list[str]):
        if usl_list is not None:
            for i, usl in enumerate(usl_list):
                if i>= len(self._test_result_list):
                    break
                self._test_result_list[i].usl = usl

    def set_lsls(self, lsl_list: list[str]):
        if lsl_list is not None:
            for i, lsl in enumerate(lsl_list):
                if i>= len(self._test_result_list):
                    break
                self._test_result_list[i].lsl = lsl

    def set_units(self, unit_list: list[str]):
        if unit_list is not None:
            for i, unit in enumerate(unit_list):
                if i>= len(self._test_result_list):
                    break
                self._test_result_list[i].unit = unit

    def set_sn(self, sn: str):
        self._sn = sn

    def get_sn(self):
        return self._sn

    def get_test_result(self):
        return bool(self._test_result_list) and all(item.is_success() for item in self._test_result_list)

    def get_test_result_left(self, test_item_name: str):
        result = True
        if test_item_name in self._test_item_list:
            index = self._test_item_list.index(test_item_name)
            for i in range(index):
                result &= self._test_result_list[i].is_success()
            return result
        return False

    def to_dic(self):
        test_item_dic = {"id": self.index}
        sn = self.get_sn()
        if sn == "":
            test_item_dic['sn'] = {
                "value": '',
                "color": "BLACK",
                "backgrount": "WHITE"
            }
        else:
            test_item_dic['sn'] = {
                "value": sn,
                "color": "BLACK",
                "backgrount": "GREEN"
            }

        for item in self._test_result_list:
            result = item.get_result()
            if result is None:
                color = "BLACK"
                background = "WHITE"
            elif result:
                color = "BLACK"
                background = "GREEN"
            else:
                color = "BLACK"
                background = "Salmon"
            test_item_dic[item.get_name()] = {
                "value": item.get_status(),
                "color": color,
                "backgrount": background
            }

        return test_item_dic

    def __getitem__(self, name: str) -> TestResultRow:
        item = next((x for x in self._test_result_list if x.get_name() == name), None)
        if item is None:
            raise KeyError(f"Unknown TestItemName: {name}")
        return item

    def __iter__(self):
        for item in self._test_result_list:
            yield item

    def __str__(self):
        test_items_dic = {"id": self.index + 1}
        for item in self._test_result_list:
            test_items_dic[item.get_name()] = item.get_status()
        return json.dumps(test_items_dic, ensure_ascii=False)




@dataclass
class TestResultConfig:
    StationID: str = ""
    StationName: str = ""
    TaktTime: float = 0
    TestSite: str = ""
    OperatorID: str = ""
    ProjectName: str = ""
    TestSWRev: str = ""
    TestDateTime: str = ""
    PCName: str = ""

    def set_test_datetime(self):
        self.TestDateTime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def set_testswrev(self, version):
        self.TestSWRev = version

    def __str__(self):
        return (f"StationID : {self.StationID}, "
                f"StationName : {self.StationName}, "
                f"TaktTime : {self.TaktTime}, "
                f"TestSite : {self.TestSite}, "
                f"OperatorID : {self.OperatorID}, "
                f"ProjectName : {self.ProjectName}, "
                f"TestSWRev : {self.TestSWRev}, "
                f"TestDateTime : {self.TestDateTime}, "
                f"PCName : {self.PCName}")

class TestResultManager:

    def __init__(
        self,
        channel_index: int,
        channel_capacity: int,
        test_suite: SeqTestSuite,
        result_config: TestResultConfig,
        global_config,
        test_mode
    ):
        self._channel_index = channel_index
        self._channel_capacity = channel_capacity
        self._test_suite: SeqTestSuite = test_suite
        self._result_config: TestResultConfig = copy.deepcopy(result_config)
        if not isinstance(channel_capacity, int) or isinstance(channel_capacity, bool) or channel_capacity < 1:
            raise ValueError("channel_capacity must be a positive integer")
        self._lock = threading.RLock()
        self._test_result_table_list = []
        self._test_item_list = []
        self._test_description_list = []
        self._usl_list = []
        self._lsl_list = []
        self._unit_list = []
        self.sn_index_dict = {}
        self.global_config = global_config
        self.test_mode = test_mode

        for testcase in test_suite:
            self._test_item_list.append(testcase.TestItemName)
            self._test_description_list.append(testcase.TestDescription)
            self._usl_list.append(testcase.USL)
            self._lsl_list.append(testcase.LSL)
            self._unit_list.append(testcase.Unit)

        if len(self._test_item_list) != len(set(self._test_item_list)):
            raise ValueError("TestItemName must be unique")

        self._logger_manager = DutLogger(self._result_config.ProjectName)
        self.logger_list = []
        self._logger_names = []
        self.log = Logger().logger()

    def _check_index(self, index):
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("index must be int")
        if not 0 <= index < len(self._test_result_table_list):
            raise IndexError(f"Index {index} out of range")
        return index

    def init_test_result_table(self, count: int):
        if not isinstance(count, int) or isinstance(count, bool) or count != self._channel_capacity:
            raise ValueError("count must match channel_capacity")
        with self._lock:
            self.close_loggers()
            self.sn_index_dict.clear()
            self._test_result_table_list = [
                TestResultTable(i, self._test_item_list, self._test_description_list) for i in range(count)
            ]
            for table in self._test_result_table_list:
                table.set_usls(self._usl_list)
                table.set_lsls(self._lsl_list)
                table.set_units(self._unit_list)
            token = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f") + "_" + uuid.uuid4().hex[:8]
            for index in range(count):
                name = f"{self._logger_name(index)}_{token}"
                self._logger_names.append(name)
                self.logger_list.append(self._logger_manager.logger(name))

    def close_loggers(self):
        with self._lock:
            for name in self._logger_names:
                self._logger_manager.del_logger_handler(name)
            self._logger_names.clear()
            self.logger_list.clear()

    def begin_run(self):
        with self._lock:
            serials = [table.get_sn() for table in self._test_result_table_list]
            self.init_test_result_table(self._channel_capacity)
            for index, sn in enumerate(serials):
                self.set_sn(index, sn)
            self._result_config.set_test_datetime()

    def reset_case_result(self, test_item_name):
        with self._lock:
            for table in self._test_result_table_list:
                row = table[test_item_name]
                row._result = None
                row._status = "PAUSE"
                row.detail = ""
                row.result_value = 0

    def get_case_results(self, test_item_name):
        with self._lock:
            return [table[test_item_name].get_result() for table in self._test_result_table_list]

    def set_sn(self, index: int, sn: str):
        with self._lock:
            self._check_index(index)
            if not isinstance(sn, str):
                raise ValueError("SN must be str")
            if sn and sn in self.sn_index_dict and index != self.sn_index_dict[sn]:
                raise ValueError(f"SN {sn} already exists")
            previous = self._test_result_table_list[index].get_sn()
            if previous:
                self.sn_index_dict.pop(previous, None)
            self._test_result_table_list[index].set_sn(sn)
            if sn:
                self.sn_index_dict[sn] = index

    def get_sn(self, index: int) -> str:
        with self._lock:
            return self._test_result_table_list[self._check_index(index)].get_sn()

    def get_index(self, sn: str) -> int:
        with self._lock:
            if sn not in self.sn_index_dict:
                raise ValueError(f"SN {sn} not found")
            return self.sn_index_dict[sn]

    def update_result(self, index: int, test_item_name: str, result: bool, result_value: float, detail: str):
        if not isinstance(test_item_name, str):
            raise ValueError("TestItemName must be str")
        if not isinstance(result, bool):
            raise ValueError("Result must be bool")
        if not isinstance(result_value, (float, int)):
            raise ValueError("ResultValue must be numeric")
        if not isinstance(detail, str):
            raise ValueError("Detail must be str")
        with self._lock:
            table = self._test_result_table_list[self._check_index(index)]
            item = table[test_item_name]
            item.set_result(result)
            item.result_value = result_value
            item.detail = detail

    def get_result(self, index: int):
        with self._lock:
            return self._test_result_table_list[self._check_index(index)].get_test_result()

    def get_test_result_left(self, index: int, test_item_name: str):
        with self._lock:
            return self._test_result_table_list[self._check_index(index)].get_test_result_left(test_item_name)

    def get_result_value(self, index: int, test_item_name: str):
        with self._lock:
            return self._test_result_table_list[self._check_index(index)][test_item_name].result_value

    def get_logger(self, index: int):
        with self._lock:
            self._check_index(index)
            if len(self.logger_list) != self._channel_capacity:
                raise RuntimeError("Device loggers are closed; call begin_run() before testing")
            return self.logger_list[index]

    def get_channel_index(self):
        return self._channel_index

    def get_channel_capacity(self):
        return self._channel_capacity

    def get_case_time(self, test_item_name: str):
        if test_item_name not in self._test_item_list:
            return 0

        index = self._test_item_list.index(test_item_name)
        time_list = self._test_suite.get_case_time()
        if index >= len(time_list):
            return 0
        return time_list[index]

    def get_result_info_hor(self):
        with self._lock:
            return [item.to_dic() for item in self._test_result_table_list]

    @staticmethod
    def _safe_filename(value):
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))
        name = name.replace("..", "_").strip(" .")
        return name[:100] or "unnamed"

    def create_dtams_report(self, dtams_dir: str, test_data_save_dir=None) -> List[bool]:
        with self._lock:
            active = getattr(self._test_suite, "_active_case_thread", None)
            if getattr(self._test_suite, "_running", False) or (active is not None and active.is_alive()):
                raise RuntimeError("Wait for the test suite and cleanup to finish before exporting reports")
            if len(self._logger_names) != len(self._test_result_table_list):
                raise RuntimeError("Device loggers are closed; export reports before closing them")
            return self._create_dtams_report(dtams_dir, test_data_save_dir or [])

    def _create_dtams_report(self, dtams_dir, test_data_save_dir):
        station = self._safe_filename(f"{self._result_config.StationID}_{self._result_config.StationName}")
        report_dir = Path(dtams_dir).expanduser().resolve() / station / datetime.date.today().isoformat()
        report_dir.mkdir(parents=True, exist_ok=True)
        if not self._result_config.TestDateTime:
            self._result_config.set_test_datetime()
        time_list = self._test_suite.get_case_time()
        result = []
        for index, table in enumerate(self._test_result_table_list):
            if not table.get_sn():
                result.append(False)
                continue
            header = DtasHeader()
            for key in ("StationID", "StationName", "TestSite", "OperatorID", "ProjectName",
                        "TestSWRev", "TestDateTime", "PCName"):
                setattr(header, key, getattr(self._result_config, key))
            header.SerialNumber = table.get_sn()
            header.TestStatus = "PASS" if table.get_test_result() else "FAIL"
            header.TaktTime = sum(time_list)
            rows = []
            for row_index, item in enumerate(table):
                row = DtasRow()
                row.TestItemName = item.get_name()
                row.TestDescription = item.test_description
                row.Unit, row.USL, row.LSL = item.unit, item.usl, item.lsl
                row.ResultValue, row.Status = item.result_value, item.get_status()
                row.Detail = item.detail
                if row_index < len(time_list):
                    row.Detail += f" (cost:{round(time_list[row_index], 2)}/s)"
                rows.append(row)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{self._safe_filename(table.get_sn())}_{timestamp}_{self._channel_index}_{index}_{header.TestStatus}"
            with tempfile.TemporaryDirectory(prefix=".report-", dir=report_dir) as temp_dir:
                temp = Path(temp_dir)
                csv_path, log_path, zip_path = (temp / f"{filename}{suffix}" for suffix in (".csv", ".log", ".zip"))
                dtas_csv(str(csv_path), header.StationName, header, rows)
                logger_name = self._logger_names[index]
                if not self._logger_manager.redirect(logger_name, log_path.name, log_path):
                    raise FileNotFoundError(f"Device log not found: {logger_name}")
                if index < len(test_data_save_dir) and test_data_save_dir[index]:
                    backup = Path(test_data_save_dir[index]).expanduser().resolve()
                    if not backup.is_dir():
                        raise FileNotFoundError(f"Test data directory not found: {backup}")
                    shutil.copy2(csv_path, backup / csv_path.name)
                    shutil.copy2(log_path, backup / log_path.name)
                    self._make_zip(str(backup), str(zip_path))
                for path in (csv_path, log_path, zip_path):
                    if path.exists():
                        path.replace(report_dir / path.name)
            result.append(True)
        return result

    @staticmethod
    def _make_zip(folder_path: str, zip_path: str):

        zip_target = Path(zip_path).resolve()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(folder_path):
                for file in files:
                    if Path(root, file).resolve() != zip_target:
                        zipf.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), folder_path))

        return

    def _logger_name(self, index: int):
        return f"{self._channel_index}_{index}"
