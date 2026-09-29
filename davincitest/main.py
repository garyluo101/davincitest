import argparse
import json
import sys
from pathlib import Path

from davincitest.manage import TestManager


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run an Excel-configured production test suite")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().with_name("config1.xlsx"))
    parser.add_argument("--mode", default="debug", help="Workbook sheet containing test cases")
    parser.add_argument("--channel", type=int, default=0, help="Zero-based channel index")
    parser.add_argument("--sn", action="append", default=[], help="Device serial number; repeat for each device")
    parser.add_argument("--report-dir", type=Path, help="Export DTAMS CSV and device logs after testing")
    args = parser.parse_args(argv)
    manager = TestManager()
    runner = None
    try:
        manager.set_config(args.config)
        if args.sn and len(args.sn) != manager.device_count:
            raise ValueError(f"Expected {manager.device_count} serial numbers, got {len(args.sn)}")
        if args.report_dir and not args.sn:
            raise ValueError("--report-dir requires --sn for every device")
        runner = manager.load_test_suite_to_channel(args.mode, args.channel)
        result_manager = manager.result_manager_list[args.channel]
        for index, sn in enumerate(args.sn):
            result_manager.set_sn(index, sn)
        runner.start_run()
        runner.wait()
        if runner.cleanup_pending:
            raise RuntimeError("Test worker/cleanup is still active after timeout; device recovery is required")
        print(json.dumps(manager.get_result_info_channel_total(args.channel), ensure_ascii=False, indent=2))
        if args.report_dir:
            result_manager.create_dtams_report(str(args.report_dir))
        return 0 if all(result_manager.get_result(i) for i in range(manager.device_count)) else 1
    except KeyboardInterrupt:
        if runner is not None:
            runner.stop_run()
        return 130
    except Exception as exc:
        print(f"Test framework error: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            manager.close()
        except RuntimeError as exc:
            print(f"Test framework cleanup pending: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
