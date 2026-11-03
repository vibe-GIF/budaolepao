import math
import random
import time

import numpy as np
import pytest

from server.models import GPSPoint, SensorFrame, TraceRequest
from rules.config import (
    DEFAULT_CONFIG,
    AccuracyStabilityConfig,
    MockDetectionConfig,
    NoiseSpectrumConfig,
    SensorConsistencyConfig,
    SpeedPhysiologicalConfig,
)
from rules.engine import RuleEngine
from rules.risk_scorer import RiskScorer


def make_point(lon, lat, accuracy=8.0, ts=None, **kw):
    return GPSPoint(
        lon=lon, lat=lat, accuracy=accuracy,
        timestamp=ts or time.time(), **kw,
    )


def make_trace(points, sensors=None, account="test", fp=None):
    return TraceRequest(
        trace_id="test-001",
        gps_points=points,
        sensors=sensors,
        account_id=account,
        device_fingerprint=fp or {},
    )


@pytest.fixture
def engine():
    return RuleEngine(config=DEFAULT_CONFIG)


@pytest.fixture
def scorer():
    return RiskScorer(config=DEFAULT_CONFIG)


# -----------------------------------------------------------
#  Mock Detection
# -----------------------------------------------------------

def test_mock_detection_clean(engine):
    cfg = MockDetectionConfig()
    rule = engine._rules["mock_detection"]
    points = [make_point(106.57, 29.50) for _ in range(5)]
    trace = make_trace(points)
    result = rule.evaluate(trace, cfg)
    assert result.passed
    assert result.score == 0.0


# -----------------------------------------------------------
#  Accuracy Stability
# -----------------------------------------------------------

def test_accuracy_stability_suspicious(engine):
    cfg = AccuracyStabilityConfig()
    rule = engine._rules["accuracy_stability"]
    points = [make_point(106.57, 29.50, accuracy=8.0) for _ in range(20)]
    trace = make_trace(points)
    result = rule.evaluate(trace, cfg)
    assert not result.passed
    assert result.score > 0


def test_accuracy_stability_normal(engine):
    cfg = AccuracyStabilityConfig()
    rule = engine._rules["accuracy_stability"]
    points = [
        make_point(106.57, 29.50, accuracy=random.uniform(3, 15))
        for _ in range(20)
    ]
    trace = make_trace(points)
    result = rule.evaluate(trace, cfg)
    assert result.passed


# -----------------------------------------------------------
#  Speed Physiological
# -----------------------------------------------------------

def test_speed_physiological_impossible(engine):
    cfg = SpeedPhysiologicalConfig()
    rule = engine._rules["speed_physiological"]
    t0 = time.time()
    points = [
        make_point(106.57, 29.50, ts=t0),
        make_point(106.58, 29.51, ts=t0 + 1),
    ]
    trace = make_trace(points)
    result = rule.evaluate(trace, cfg)
    assert not result.passed
    assert result.score > 0


def test_speed_physiological_normal(engine):
    cfg = SpeedPhysiologicalConfig()
    rule = engine._rules["speed_physiological"]
    lat, lon = 29.50, 106.57
    points = []
    t0 = time.time()
    ts = t0
    for i in range(30):
        lat += 0.00004 + 0.00002 * math.sin(i * 0.3)
        lon += 0.00004 + 0.00002 * math.cos(i * 0.4)
        ts += random.uniform(1.8, 2.2)
        points.append(make_point(lon, lat, ts=ts))
    trace = make_trace(points)
    result = rule.evaluate(trace, cfg)
    assert result.passed


# -----------------------------------------------------------
#  Noise Spectrum
# -----------------------------------------------------------

def test_noise_spectrum_white_noise(engine):
    cfg = NoiseSpectrumConfig()
    rule = engine._rules["noise_spectrum"]
    # A short random sample can cross the correlation threshold by chance.
    # Keep this regression fixture reproducible without changing global RNG state.
    rng = random.Random(0)
    t0 = 1_700_000_000.0
    lat, lon = 29.50, 106.57
    points = []
    for i in range(60):
        # 白噪声：每步独立随机，lag-1 自相关接近 0
        lat += 0.00001 + 0.00002 * (rng.random() - 0.5)
        lon += 0.00001 + 0.00002 * (rng.random() - 0.5)
        points.append(make_point(lon, lat, ts=t0 + i * 0.4))
    trace = make_trace(points)
    result = rule.evaluate(trace, cfg)
    assert not result.passed


def test_noise_spectrum_colored(engine):
    cfg = NoiseSpectrumConfig()
    rule = engine._rules["noise_spectrum"]
    t0 = time.time()
    lat, lon = 29.50, 106.57
    points = [make_point(lon, lat, ts=t0)]
    d_lat, d_lon = 0.0, 0.0
    phi = 0.8
    for i in range(200):
        d_lat = phi * d_lat + 0.00001 * random.gauss(0, 1)
        d_lon = phi * d_lon + 0.00001 * random.gauss(0, 1)
        lat += 0.00002 + d_lat
        lon += 0.00002 + d_lon
        points.append(make_point(lon, lat, ts=t0 + i * 0.4))
    trace = make_trace(points)
    result = rule.evaluate(trace, cfg)
    assert result.passed


# -----------------------------------------------------------
#  Sensor Consistency
# -----------------------------------------------------------

def test_sensor_consistency_missing(engine):
    cfg = SensorConsistencyConfig()
    rule = engine._rules["sensor_consistency"]
    points = [make_point(106.57, 29.50) for _ in range(10)]
    trace = make_trace(points, sensors=None)
    result = rule.evaluate(trace, cfg)
    assert not result.passed


def test_sensor_consistency_correlated(engine):
    cfg = SensorConsistencyConfig()
    rule = engine._rules["sensor_consistency"]
    t0 = time.time()
    points = []
    sensors = []
    lat, lon = 29.50, 106.57
    for i in range(20):
        lat += 0.0001
        lon += 0.0001
        ts = t0 + i * 2
        points.append(make_point(lon, lat, ts=ts))
        sensors.append(SensorFrame(
            timestamp=ts,
            step_rate=float(2.0 + i * 0.1),
            accel_x=0.5, accel_y=0.3, accel_z=9.8,
        ))
    trace = make_trace(points, sensors=sensors)
    result = rule.evaluate(trace, cfg)
    assert result.passed


# -----------------------------------------------------------
#  End-to-end: clean trace
# -----------------------------------------------------------

def test_e2e_clean_trace(engine, scorer):
    t0 = time.time()
    lat, lon = 29.50, 106.57
    points = []
    sensors = []
    for i in range(100):
        lat += 0.00005 + random.uniform(-0.00001, 0.00001)
        lon += 0.00005 + random.uniform(-0.00001, 0.00001)
        ts = t0 + i * random.uniform(0.3, 0.6)
        accuracy = random.uniform(3, 15)
        points.append(make_point(lon, lat, accuracy=accuracy, ts=ts))
        sensors.append(SensorFrame(
            timestamp=ts,
            step_count=int(1.5 + i * 0.05),
            accel_x=random.uniform(-0.5, 0.5),
            accel_y=random.uniform(-0.3, 0.3),
            accel_z=random.uniform(9.6, 10.0),
        ))
    trace = make_trace(
        points, sensors=sensors, account="user1",
        fp={"build": "samsung_sm-s908b", "sensor_count": 12, "device_id": "dev1"},
    )
    results = engine.evaluate(trace)
    risk = scorer.score(results)
    verdict = scorer.verdict(risk)
    assert verdict in ("normal", "low_risk")
    assert risk < 0.45


# -----------------------------------------------------------
#  End-to-end: suspicious trace (low variance, no sensors)
# -----------------------------------------------------------

def test_e2e_suspicious_trace(engine, scorer):
    t0 = time.time()
    lat, lon = 29.50, 106.57
    points = []
    for i in range(100):
        lat += 0.00005
        lon += 0.00005
        ts = t0 + i * 0.4
        points.append(make_point(lon, lat, accuracy=5.0, ts=ts))
    trace = make_trace(
        points, sensors=None, account="user2",
        fp={"build": "sdk_phone_arm64", "sensor_count": 3, "device_id": "dev2"},
    )
    results = engine.evaluate(trace)
    risk = scorer.score(results)
    verdict = scorer.verdict(risk)
    assert verdict in ("high_risk", "medium_risk", "low_risk")
    assert risk > 0.3


# -----------------------------------------------------------
#  Step-Distance consistency (WeRun vs GPS)
# -----------------------------------------------------------

def _moving_trace(n=50, step=0.00005):
    lat, lon = 30.469, 114.407
    t0 = time.time()
    pts = []
    for i in range(n):
        lat += step
        lon += step
        pts.append(make_point(lon, lat, ts=t0 + i * 1.0))
    return pts


def test_step_distance_spoofed(engine):
    from rules.step_distance import StepDistanceRule
    from rules.config import StepDistanceConfig
    rule = StepDistanceRule()
    pts = _moving_trace()  # ~ 50 * 7.8m ≈ 390m... make longer
    pts = _moving_trace(n=200)  # ~1.5km
    trace = make_trace(pts)
    trace.total_steps = 5  # GPS ran but almost no steps
    result = rule.evaluate(trace, StepDistanceConfig())
    assert not result.passed
    assert result.score == 1.0


def test_step_distance_consistent(engine):
    from rules.step_distance import StepDistanceRule
    from rules.config import StepDistanceConfig
    rule = StepDistanceRule()
    pts = _moving_trace(n=200)  # ~1.5km
    trace = make_trace(pts)
    trace.total_steps = 2200  # ~1.5km at 0.7m stride
    result = rule.evaluate(trace, StepDistanceConfig())
    assert result.passed


def test_step_distance_no_steps_not_applicable(engine):
    from rules.step_distance import StepDistanceRule
    from rules.config import StepDistanceConfig
    rule = StepDistanceRule()
    trace = make_trace(_moving_trace())
    result = rule.evaluate(trace, StepDistanceConfig())
    assert result.applicable is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
