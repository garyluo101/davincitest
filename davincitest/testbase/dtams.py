import csv
from typing import List

from pathlib import Path

from davincitest.testbase.logger import Logger

class DtasHeader:

    def __init__(self) -> None:
        self.StationID = ""
        self.StationName = ""
        self.SerialNumber = ""
        self.TaktTime = ""
        self.TestSite = ""
        self.OperatorID = ""
        self.ProjectName = ""
        self.TestSWRev = ""
        self.TestDateTime = ""
        self.PCName = ""
        self.TestItem = ""
        self.TestStatus = ""

class DtasRow:

    def __init__(self) -> None:
        self.TestDescription = ""
        self.TestItemName = ""
        self.Unit = ""
        self.LSL = ""
        self.USL = ""
        self.ResultValue = ""
        self.Status = ""
        self.Detail = ""

def dtas_csv(csv_path: str, ETName: str, header: DtasHeader, rows: List[DtasRow]):
    path = Path(csv_path)
    Logger().logger().info(path.parent.absolute())
    path.parent.absolute().mkdir(parents=True, exist_ok=True)

    with open(csv_path, 'w', newline='', encoding='gbk') as csvfile:
        spamwriter = csv.writer(csvfile, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        spamwriter.writerow([ETName])
        spamwriter.writerow(list(header.__dict__.keys()))
        spamwriter.writerow(list(header.__dict__.values()))
        spamwriter.writerow([])
        spamwriter.writerow(list(DtasRow().__dict__.keys()))

        for row in rows:
            spamwriter.writerow(row.__dict__.values())
    return
