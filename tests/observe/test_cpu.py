from service_control_ioc.observe.cpu import CpuMeter


def test_first_sample_is_baseline() -> None:
    meter = CpuMeter(90, 10)
    assert meter.sample(0.0, 0) is None
    assert not meter.spinning()


def test_full_core_is_100_percent() -> None:
    meter = CpuMeter(90, 10)
    meter.sample(0.0, 0)
    assert meter.sample(1.0, 1_000_000) == 100.0


def test_spinning_after_window() -> None:
    meter = CpuMeter(90, 10)
    for second in range(10):
        meter.sample(float(second), second * 1_000_000)
    assert not meter.spinning()  # 9 s of continuous load
    assert meter.sample(10.0, 10_000_000) == 100.0
    assert meter.spinning()  # 10 s


def test_low_sample_resets() -> None:
    meter = CpuMeter(90, 10)
    for second in range(11):
        meter.sample(float(second), second * 1_000_000)
    assert meter.spinning()
    assert meter.sample(11.0, 10_000_000 + 100_000) == 10.0
    assert not meter.spinning()
    meter.sample(12.0, 11_100_000)
    assert not meter.spinning()


def test_single_low_sample_is_not_spinning() -> None:
    meter = CpuMeter(90, 10)
    meter.sample(0.0, 0)
    assert meter.sample(1.0, 100_000) == 10.0
    assert not meter.spinning()


def test_none_usage_resets() -> None:
    meter = CpuMeter(90, 10)
    for second in range(11):
        meter.sample(float(second), second * 1_000_000)
    assert meter.spinning()
    assert meter.sample(11.0, None) is None
    assert not meter.spinning()
    # The baseline is gone: the next sample is a new baseline.
    assert meter.sample(12.0, 12_000_000) is None
