# davincitest

通过 Excel 配置用例的生产测试框架，支持多通道、每通道多设备、顺序执行、重试、超时、失败停止及 DTAMS CSV/日志/原始数据归档。

## 安装与运行

需要 Python 3.10 或更高版本；本次验证环境为 macOS ARM64、Python 3.10.11。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m davincitest
```

也可用 `.venv/bin/python -m pip install -r requirements.txt` 安装依赖，再从项目根目录运行。安装包提供 `davincitest` 命令。

默认读取包内 `config1.xlsx` 的 `debug` 页，不依赖启动目录。示例应输出 `add_test=PASS`（1+5=6）、`minus_test=FAIL`（1-5=-4，不满足用例的 >=5 条件）。减法用例会按配置执行三次，间隔三秒，整个示例约六秒完成。

```bash
.venv/bin/python -m davincitest --config davincitest/config1.xlsx --mode debug \
  --channel 0 --sn DEMO001 --report-dir reports
.venv/bin/python -m unittest discover -v
```

退出码：`0` 表示所有设备通过，`1` 表示设备测试失败或存在未执行项，`2` 表示配置/框架/导出错误，`130` 表示用户中断。示例默认返回 `1`，这是预期测试结果。

`config.xlsx` 是原有空文件，不能作为配置；`config1.xlsx` 的 `prod` 页尚未配置用例，运行 `--mode prod` 会明确报错。生产工站应提供实际用例配置。

## 配置

`global` 页包含一行设置，必需列为 `StationID`、`StationName`、`TestSite`、`OperatorID`、`ProjectName`、`TestSWRev`、`ChannelCount`、`DeviceCount`。两个数量必须为正整数。可选 `GlobalConfig` 为 JSON 对象，空白表示没有全局配置；非法 JSON 会阻止加载。`TestSWRev` 按配置保留。

每个测试模式使用独立工作表。必需列为 `TestItemName`、`TestCase`、`TestData`，其余列可省略或留空以使用默认值。

| 列 | 含义与默认值 |
| --- | --- |
| TestItemName | 启用用例的唯一名称，非空字符串 |
| TestCase | 实现 `run_test()` 的类全名，例如 `davincitest.testcases.AddTest` |
| TestData | JSON 对象；空白使用 `{}` |
| TestDescription、Unit、Detail | 描述、单位、明细；默认空字符串 |
| LSL、USL | 上下限元数据，默认 0；由具体用例实现判定 |
| TimeOut | 单次尝试的超时秒数，包含准备、执行、清理；默认 60，必须 >0 |
| RetryTimes | 总尝试次数，默认 1，必须为 >=1 的整数 |
| RetryGap | 重试间隔秒数，默认 3，必须 >=0 |
| FailStopFlag | 失败耗尽重试后是否停止套件，0/1，默认 0 |
| Skip | 所有模式下跳过该配置行，0/1，默认 0；不进入结果表 |
| FailSkipFlag | 前面任一用例最终失败时跳过本项，0/1，默认 0 |

未知列、重复名称、缺少列、空套件、无效类和无效数值会给出错误；加载失败时保留原通道。未执行项保持 `PAUSE`，不会被统计为通过。

## 接入用例和应用

```python
from davincitest.manage import TestManager

manager = TestManager()
manager.set_config("davincitest/config1.xlsx")
runner = manager.load_test_suite_to_channel("debug", 0, share_data_mgr=None)
results = manager.result_manager_list[0]
results.set_sn(0, "DEMO001")
runner.start_run()
runner.wait()  # 等待实际完成；后台框架异常会在此抛出
print(manager.get_result_info_channel_total(0))
results.create_dtams_report("reports")
manager.close()
```

`TestManager` 保留单例接口。每个通道有独立套件、执行器和结果管理器，可分别启动。每次重新运行会重置结果与计时、创建新设备日志，保留 SN。运行或清理期间不能替换该通道或重载配置。

用例继承 `TestCase`，生命周期为 `pre_test()` → `run_test()` → `post_test()`。准备失败时跳过执行，仍尝试清理。单设备或统一判定可设置 `Status`、`ResultValue`、`Detail`；逐设备判定使用 `ResultManager.update_result(index, TestItemName, passed, value, detail)`。没有明确结果会判失败，任一设备失败会驱动用例重试。普通异常清空结果值，`DAVINCIException` 保留 `ResultValue`，异常与清理错误会写入结果明细。

设备通信应设置自身 I/O 超时，并在长循环中检查 `self.cancel_event.is_set()`。框架保留原有 `kthread` 终止机制；阻塞的底层 I/O 不保证立即退出。超时或停止后最多额外等待五秒，同一工作线程负责清理；若仍存活则停止后续用例，`runner.cleanup_pending` 为真，并阻止重启和报告导出。应等待阻塞解除、确认设备恢复后再运行。

`runner.stop_run(timeout=...)` 会请求停止并等待，返回是否在指定时间内结束套件；`runner.wait(timeout=...)` 返回是否结束套件。工作线程的遗留清理由 `cleanup_pending` 判断。

## 日志与报告

日志默认位于当前目录下 `logs/YYYY-MM-DD/`，可通过 `DAVINCITEST_LOG_DIR` 指定目录。框架日志按日与大小轮转；设备日志按运行隔离，保持 UTF-8 编码。

报告位于 `reports/工站ID_工站名/日期/`，CSV 保留原有 GBK 编码要求，包含工站字段、测量值、状态、明细和耗时。每个有 SN 的设备生成 CSV 与独立日志；缺少 SN 的设备返回 `False`。传入 `create_dtams_report(path, [设备0数据目录, ...])` 可同时备份 CSV/日志并压缩该设备原始数据。重复导出使用独立文件名，导出后仍可再次运行。

完整项目梳理和验证记录见 [docs/PROJECT_REVIEW.md](docs/PROJECT_REVIEW.md)。
